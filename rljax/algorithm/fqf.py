import os
from functools import partial
from typing import Tuple

import haiku as hk
import jax
import jax.numpy as jnp
import numpy as np
from jax.experimental import optix

from rljax.algorithm.base_class import QLearning
from rljax.network import CumProbNetwork, DiscreteImplicitQuantileFunction, make_quantile_nerwork
from rljax.util import get_quantile_at_action, load_params, optimize, quantile_loss, save_params


class FQF(QLearning):
    name = "FQF"

    def __init__(
        self,
        num_agent_steps,
        state_space,
        action_space,
        seed,
        max_grad_norm=None,
        gamma=0.99,
        nstep=1,
        buffer_size=10 ** 6,
        use_per=False,
        batch_size=32,
        start_steps=50000,
        update_interval=4,
        update_interval_target=8000,
        eps=0.01,
        eps_eval=0.001,
        eps_decay_steps=250000,
        fn=None,
        lr=5e-5,
        lr_cum_p=2.5e-9,
        units=(512,),
        num_quantiles=32,
        num_cosines=64,
        loss_type="huber",
        dueling_net=False,
        double_q=False,
    ):
        assert loss_type in ["l2", "huber"]
        super(FQF, self).__init__(
            num_agent_steps=num_agent_steps,
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            max_grad_norm=max_grad_norm,
            gamma=gamma,
            nstep=nstep,
            buffer_size=buffer_size,
            batch_size=batch_size,
            use_per=use_per,
            start_steps=start_steps,
            update_interval=update_interval,
            update_interval_target=update_interval_target,
            eps=eps,
            eps_eval=eps_eval,
            eps_decay_steps=eps_decay_steps,
        )
        if fn is None:

            def fn(s, cum_p):
                return DiscreteImplicitQuantileFunction(
                    action_space=action_space,
                    hidden_units=units,
                    dueling_net=dueling_net,
                )(s, cum_p)

        self.net, self.params, fake_feature = make_quantile_nerwork(self.rng, state_space, action_space, fn, num_quantiles)
        self.params_target = self.params
        opt_init, self.opt = optix.adam(lr, eps=0.01 / batch_size)
        self.opt_state = opt_init(self.params)

        # Fraction proposal network.
        self.cum_p_net = hk.without_apply_rng(hk.transform(lambda s: CumProbNetwork(num_quantiles=num_quantiles)(s)))
        self.params_cum_p = self.cum_p_net.init(next(self.rng), fake_feature)
        opt_init, self.opt_cum_p = optix.rmsprop(lr_cum_p, decay=0.95, eps=1e-5, centered=True)
        self.opt_state_cum_p = opt_init(self.params_cum_p)

        # Other parameters.
        self.num_quantiles = num_quantiles
        self.num_cosines = num_cosines
        self.loss_type = loss_type
        self.double_q = double_q

    def forward(self, state):
        return self._forward(self.params_cum_p, self.params, state)

    @partial(jax.jit, static_argnums=0)
    def _forward(
        self,
        params_cum_p: hk.Params,
        params: hk.Params,
        state: np.ndarray,
    ) -> jnp.ndarray:
        feature = self.net["feature"].apply(params["feature"], state)
        cum_p, cum_p_prime = self.cum_p_net.apply(params_cum_p, feature)
        quantile_s = self.net["quantile"].apply(params["quantile"], feature, cum_p_prime)
        q_s = ((cum_p[:, 1:, None] - cum_p[:, :-1, None]) * quantile_s).sum(axis=1)
        return jnp.argmax(q_s, axis=1)

    def update(self, writer=None):
        self.learning_step += 1
        weight, batch = self.buffer.sample(self.batch_size)
        state, action, reward, done, next_state = batch

        # Update fraction proposal network.
        self.opt_state_cum_p, self.params_cum_p, loss_cum_p, _ = optimize(
            self._loss_cum_p,
            self.opt_cum_p,
            self.opt_state_cum_p,
            self.params_cum_p,
            self.max_grad_norm,
            params=self.params,
            state=state,
            action=action,
        )

        # Update quantile network.
        self.opt_state, self.params, loss, abs_td = optimize(
            self._loss,
            self.opt,
            self.opt_state,
            self.params,
            self.max_grad_norm,
            params_target=self.params_target,
            params_cum_p=self.params_cum_p,
            state=state,
            action=action,
            reward=reward,
            done=done,
            next_state=next_state,
            weight=weight,
            **self.kwargs_update,
        )

        # Update priority.
        if self.use_per:
            self.buffer.update_priority(abs_td)

        # Update target network.
        if self.agent_step % self.update_interval_target == 0:
            self.params_target = self._update_target(self.params_target, self.params)

        if writer and self.learning_step % 1000 == 0:
            writer.add_scalar("loss/q", loss, self.learning_step)
            writer.add_scalar("loss/cum_p", loss_cum_p, self.learning_step)

    @partial(jax.jit, static_argnums=0)
    def _loss(
        self,
        params: hk.Params,
        params_target: hk.Params,
        params_cum_p: hk.Params,
        state: np.ndarray,
        action: np.ndarray,
        reward: np.ndarray,
        done: np.ndarray,
        next_state: np.ndarray,
        weight: np.ndarray,
    ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        # Calculate features.
        feature = self.net["feature"].apply(params["feature"], state)
        next_feature = self.net["feature"].apply(params_target["feature"], next_state)

        # Calculate proposed fractions at current states.
        cum_p_prime = jax.lax.stop_gradient(self.cum_p_net.apply(params_cum_p, feature)[1])

        # Calculate greedy actions using fractions proposed at next states.
        if self.double_q:
            # With online network.
            next_action = self._forward(params_cum_p, params, next_state)[..., None]
        else:
            # With target network.
            next_cum_p, next_cum_p_prime = self.cum_p_net.apply(params_cum_p, next_feature)
            next_quantile_s = self.net["quantile"].apply(params_target["quantile"], next_feature, next_cum_p_prime)
            next_q_s = ((next_cum_p[:, 1:, None] - next_cum_p[:, :-1, None]) * next_quantile_s).sum(axis=1)
            next_action = jnp.argmax(next_q_s, axis=1)[..., None]

        # Calculate max quantile values with target network. Note that target quantiles share the same proposed fractions
        # with current quantiles. (i.e. next_cum_p_prime = cum_p_prime)
        next_quantile_s = self.net["quantile"].apply(params_target["quantile"], next_feature, cum_p_prime)
        next_quantile = get_quantile_at_action(next_quantile_s, next_action)

        # Calculate target quantile values and reshape to (batch_size, 1, N).
        target_quantile = jnp.expand_dims(reward, 2) + (1.0 - jnp.expand_dims(done, 2)) * self.discount * next_quantile
        target_quantile = jax.lax.stop_gradient(target_quantile).reshape(-1, 1, self.num_quantiles)

        # Calculate current quantile values, whose shape is (batch_size, N, 1).
        curr_quantile = get_quantile_at_action(self.net["quantile"].apply(params["quantile"], feature, cum_p_prime), action)
        td = target_quantile - curr_quantile
        loss = quantile_loss(td, cum_p_prime, weight, self.loss_type)
        abs_td = jnp.abs(td).sum(axis=1).mean(axis=1, keepdims=True)
        return loss, jax.lax.stop_gradient(abs_td)

    @partial(jax.jit, static_argnums=0)
    def _loss_cum_p(self, params_cum_p, params, state, action):
        # Calculate feature.
        feature = jax.lax.stop_gradient(self.net["feature"].apply(params["feature"], state))
        # Calculate cumulative probabilities.
        cum_p, cum_p_prime = self.cum_p_net.apply(params_cum_p, feature)
        # Caluculate quantile values.
        quantile = get_quantile_at_action(self.net["quantile"].apply(params["quantile"], feature, cum_p[:, 1:-1]), action)
        quantile_prime = get_quantile_at_action(self.net["quantile"].apply(params["quantile"], feature, cum_p_prime), action)

        # NOTE: Proposition 1 in the paper requires F^{-1} is non-decreasing. I relax this requirements and
        # calculate gradients of taus even when F^{-1} is not non-decreasing.
        val1 = quantile - quantile_prime[:, :-1]
        sign1 = quantile > jnp.concatenate([quantile_prime[:, :1], quantile[:, :-1]], axis=1)
        val2 = quantile - quantile_prime[:, 1:]
        sign2 = quantile < jnp.concatenate([quantile[:, 1:], quantile_prime[:, -1:]], axis=1)
        grad = jnp.where(sign1, val1, -val1) + jnp.where(sign2, val2, -val2)
        grad = jax.lax.stop_gradient(grad.reshape(-1, self.num_quantiles - 1))
        return (cum_p[:, 1:-1] * grad).sum(axis=1).mean(), None

    def save_params(self, save_dir):
        super().save_params(save_dir)
        save_params(self.params_cum_p, os.path.join(save_dir, "params_cum_p.npz"))

    def load_params(self, save_dir):
        super().load_params(save_dir)
        self.params_cum_p = load_params(os.path.join(save_dir, "params_cum_p.npz"))
