from abc import ABC, abstractmethod
from functools import partial

import numpy as np

import jax
from haiku import PRNGSequence
from rljax.buffer import PrioritizedReplayBuffer, ReplayBuffer, RolloutBuffer
from rljax.utils import soft_update


class Algorithm(ABC):
    """
    Base class for algorithms.
    """

    def __init__(
        self,
        num_steps,
        state_space,
        action_space,
        seed,
        gamma,
    ):
        self.rng = PRNGSequence(seed)
        self.env_step = 0
        self.learning_step = 0
        self.num_steps = num_steps
        self.state_space = state_space
        self.action_space = action_space
        self.gamma = gamma

    @abstractmethod
    def is_update(self):
        pass

    @abstractmethod
    def select_action(self, state):
        pass

    @abstractmethod
    def step(self, env, state, t):
        pass

    @abstractmethod
    def update(self, writer):
        pass

    @abstractmethod
    def __str__(self):
        pass


class OnPolicyActorCritic(Algorithm):
    """
    Base class for on-policy Actor-Critic algorithms.
    """

    def __init__(
        self,
        num_steps,
        state_space,
        action_space,
        seed,
        gamma,
        buffer_size,
    ):
        super(OnPolicyActorCritic, self).__init__(
            num_steps=num_steps,
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            gamma=gamma,
        )

        self.discount = gamma
        self.buffer_size = buffer_size
        self.buffer = RolloutBuffer(
            buffer_size=buffer_size,
            state_space=state_space,
            action_space=action_space,
        )

    def select_action(self, state):
        action = self._select_action(self.params_actor, state[None, ...])
        return np.array(action[0])

    def explore(self, state):
        action, log_pi = self._explore(self.params_actor, next(self.rng), state[None, ...])
        return np.array(action[0]), np.array(log_pi[0])

    @abstractmethod
    def _select_action(self, params_actor, state):
        pass

    @abstractmethod
    def _explore(self, params_actor, rng, state):
        pass

    def is_update(self):
        return self.env_step % self.buffer_size == 0

    def step(self, env, state, t):
        t += 1
        self.env_step += 1

        action, log_pi = self.explore(state)
        next_state, reward, done, _ = env.step(action)
        mask = False if t == env._max_episode_steps else done
        self.buffer.append(state, action, reward, mask, log_pi, next_state)

        if done:
            t = 0
            next_state = env.reset()

        return next_state, t


class OffPolicyAlgorithm(Algorithm):
    """
    Base class for off-policy algorithms.
    """

    def __init__(
        self,
        num_steps,
        state_space,
        action_space,
        seed,
        gamma,
        nstep,
        buffer_size,
        use_per,
        batch_size,
        start_steps,
        update_interval,
    ):
        super(OffPolicyAlgorithm, self).__init__(
            num_steps=num_steps,
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            gamma=gamma,
        )
        if use_per:
            self.buffer = PrioritizedReplayBuffer(
                buffer_size=buffer_size,
                state_space=state_space,
                action_space=action_space,
                gamma=gamma,
                nstep=nstep,
                beta_steps=(num_steps - start_steps) / update_interval,
            )
        else:
            self.buffer = ReplayBuffer(
                buffer_size=buffer_size,
                state_space=state_space,
                action_space=action_space,
                gamma=gamma,
                nstep=nstep,
            )

        self.discount = gamma ** nstep
        self.use_per = use_per
        self.batch_size = batch_size
        self.start_steps = start_steps
        self.update_interval = update_interval

    def is_update(self):
        return self.env_step % self.update_interval == 0 and self.env_step >= self.start_steps

    @abstractmethod
    def explore(self, state):
        pass

    def step(self, env, state, t):
        t += 1
        self.env_step += 1

        if self.env_step <= self.start_steps:
            action = env.action_space.sample()
        else:
            action = self.explore(state)

        next_state, reward, done, _ = env.step(action)
        mask = False if t == env._max_episode_steps else done
        self.buffer.append(state, action, reward, mask, next_state, done)

        if done:
            t = 0
            next_state = env.reset()

        return next_state, t


class OffPolicyActorCritic(OffPolicyAlgorithm):
    """
    Base class for off-policy Actor-Critic algorithms.
    """

    def __init__(
        self,
        num_steps,
        state_space,
        action_space,
        seed,
        gamma,
        nstep,
        buffer_size,
        use_per,
        batch_size,
        start_steps,
        update_interval,
        tau,
    ):
        super(OffPolicyActorCritic, self).__init__(
            num_steps=num_steps,
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            gamma=gamma,
            nstep=nstep,
            buffer_size=buffer_size,
            use_per=use_per,
            batch_size=batch_size,
            start_steps=start_steps,
            update_interval=update_interval,
        )
        self._update_target = jax.jit(partial(soft_update, tau=tau))

    def select_action(self, state):
        action = self._select_action(self.params_actor, state[None, ...])
        return np.array(action[0])

    def explore(self, state):
        action = self._explore(self.params_actor, next(self.rng), state[None, ...])
        return np.array(action[0])

    @abstractmethod
    def _select_action(self, params_actor, state):
        pass

    @abstractmethod
    def _explore(self, params_actor, rng, state):
        pass


class QLearning(OffPolicyAlgorithm):
    """
    Base class for discrete Q-learning algorithms.
    """

    def __init__(
        self,
        num_steps,
        state_space,
        action_space,
        seed,
        gamma,
        nstep,
        buffer_size,
        use_per,
        batch_size,
        start_steps,
        update_interval,
        update_interval_target,
        eps,
        eps_eval,
    ):
        super(QLearning, self).__init__(
            num_steps=num_steps,
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            gamma=gamma,
            nstep=nstep,
            buffer_size=buffer_size,
            use_per=use_per,
            batch_size=batch_size,
            start_steps=start_steps,
            update_interval=update_interval,
        )

        self.update_interval_target = update_interval_target
        self.eps = eps
        self.eps_eval = eps_eval
        self._update_target = jax.jit(partial(soft_update, tau=1.0))

    def select_action(self, state):
        if np.random.rand() < self.eps_eval:
            action = self.action_space.sample()
        else:
            action = self._select_action(self.params, state[None, ...])
            action = np.array(action[0])
        return action

    def explore(self, state):
        if np.random.rand() < self.eps:
            action = self.action_space.sample()
        else:
            action = self._select_action(self.params, state[None, ...])
            action = np.array(action[0])
        return action

    @abstractmethod
    def _select_action(self, params, state):
        pass