import json
import sys
import os
import matplotlib.pyplot as plt
from shutil import copyfile
from torch.optim import Adam
from torch.nn.functional import mse_loss
import copy
from memory import *
from her import generate_her_transitions
from models import ActorNet, CriticNet
from tools import *
from normalizer import Normalizer


class Agent:
    def __init__(self, env):
        """Args:
            env(gym.Core.env): environment
        """

        with open('./configuration.json') as config_file:
            self.config = json.load(config_file)['agent']

        self.env = env
        self.state = None
        self.epsilon = None
        self.epsilon_decay = None
        self.state_size = None
        self.actions_size = None
        self.actor = None
        self.actor_target = None
        self.critic = None
        self.critic_target = None
        self.actor_optim = None
        self.critic_optim = None
        self.gamma = None
        self.memory = None
        self.batch_size = None
        self.action_space = None
        self.normalizer = None

    def __str__(self):
        return 'RL_Agent Object'

    def reset(self):
        self.action_space = self.env.action_space
        obs_space = self.env.observation_space.spaces
        obs_len = obs_space['observation'].shape[0]
        goal_len = obs_space['desired_goal'].shape[0]
        self.state_size = obs_len + goal_len
        self.actions_size = self.action_space.shape[0]
        max_action = float(self.env.action_space.high[0])

        self.actor = ActorNet(self.state_size, *self.config['net_sizes'],
                              self.actions_size, max_action)
        self.critic = CriticNet(self.state_size, *self.config['net_sizes'],
                                self.actions_size)
        self.actor_target = ActorNet(self.state_size, *self.config['net_sizes'],
                                     self.actions_size, max_action)
        self.critic_target = CriticNet(self.state_size,
                                       *self.config['net_sizes'],
                                       self.actions_size)
        self.actor_optim = Adam(self.actor.parameters(),
                                lr=self.config['learning_rate'])
        self.critic_optim = Adam(self.critic.parameters(),
                                 lr=self.config['learning_rate'])

        self.update(self.critic_target, self.critic, 1)
        self.update(self.actor_target, self.actor, 1)

        self.epsilon = self.config['epsilon']
        self.epsilon_decay = self.config['epsilon_decay']
        self.gamma = self.config['gamma']

        if self.config['PER']:
            self.memory = self.memory = PrioritizedMemory(
                self.config['memory_size'],
                self.config["memory_alpha"],
                self.config["memory_epsilon"],
                self.config["memory_beta"],
                self.config["memory_beta_increment"])
        else:
            self.memory = ReplayBuffer(self.config['memory_size'])

        self.batch_size = self.config['batch_size']
        self.normalizer = Normalizer(obs_len, goal_len)
        # warm up the normalizer
        self.normalizer.observe(self.env.reset())

    def run(self, train):
        total_reward = 0
        done = False
        self.state = self.env.reset()
        self.normalizer.observe(self.state)
        self.state = self.normalizer.normalize(self.state)
        ep_transitions = []

        # start episode
        while not done:
            if self.config['render']:
                self.env.render()

            # act and observe
            action = self._get_action_epsilon_greedy(self.state)
            obs, reward, done, info = self.env.step(action)
            total_reward += reward

            # normalize the state
            self.normalizer.observe(obs)
            obs = self.normalizer.normalize(obs)

            # save the transition for later HER processing
            transition = [self.state, reward, action, obs, not done]
            ep_transitions.append(transition)

            # save to memory
            self.append_sample_to_memory(*copy.deepcopy((
                flatten_state_dict_for_model(self.state),
                reward, action, flatten_state_dict_for_model(obs), not done)))

            self.state = obs

        if random.random() < self.config["her-probability"]:
            her_trs = generate_her_transitions(ep_transitions,
                                               self.env.compute_reward,
                                               self.config['her-type'],
                                               self.config['her-k_value'])
            for t in her_trs:
                self.append_sample_to_memory(*t)

        if len(self.memory) > self.batch_size * 5 and train:
            for i in range(40):
                self._train()
            self.soft_update_networks()

        if self.epsilon > self.config['epsilon_min']:
            self.epsilon *= self.epsilon_decay

        return total_reward

    def _train(self):
        batch, indexes, importance_sampling_weights = None, None, None
        if self.config['PER']:
            batch, indexes, importance_sampling_weights = \
                self.sample_from_per_memory(self.batch_size)
            importance_sampling_weights = torch.Tensor(
                importance_sampling_weights)
        else:
            batch = self.memory.get_random_batch(self.batch_size)

        state_batch = torch.Tensor(batch[0])
        reward_batch = torch.Tensor(batch[1])
        action_batch = torch.Tensor(batch[2])
        next_state_batch = torch.Tensor(batch[3])
        # unused - see additional info in the Readme
        # mask_batch = torch.Tensor(batch[4] * 1)

        next_q_values = self.critic_target(next_state_batch,
                                           self.actor_target(next_state_batch))
        expected_q_values = reward_batch + (self.gamma * next_q_values)
        expected_q_values = expected_q_values.clamp_(-50., 0.).detach()

        self.critic_optim.zero_grad()
        q_values = self.critic(state_batch, action_batch)

        if self.config['PER']:
            errors = torch.abs(q_values - expected_q_values)
            critic_loss = (importance_sampling_weights * errors ** 2).sum()
            for i in range(self.batch_size):
                index = indexes[i]
                self.memory.update(index, errors[i].detach().numpy())
        else:
            critic_loss = mse_loss(q_values, expected_q_values)
        critic_loss.backward()

        self.critic_optim.step()

        self.actor_optim.zero_grad()
        policy_loss = self.critic(state_batch, self.actor(state_batch))
        action_reg = (self.actor.action_preact ** 2).mean()
        policy_loss = -policy_loss.mean() + action_reg
        policy_loss.backward()

        self.actor_optim.step()

    def get_action_greedy(self, state):
        """Hey, actor - act!... plus detach().numpy() ..."""
        return self.actor(
            flatten_state_dict_for_model(state)).detach().numpy()

    def _get_action_epsilon_greedy(self, state):
        """Returns an action for given state by using the actor network.
        With epsilon probability, it returns a fully random action.
        In both cases, there is a OU noise added as well.
        Parameters can be specified in the configuration file.
        """

        if random.random() > self.epsilon:
            action = self.get_action_greedy(state) + \
                     np.random.normal(scale=0.2, size=self.actions_size)
        else:
            action = self.env.action_space.sample()
        return np.clip(action, -1., 1.)

    def append_sample_to_memory(self, state, reward, action,
                                next_state, done):
        """Adds given transition to the memory. In case of using Prioritized
        Experience Replay, it calculates the TD error."""
        if not self.config['PER']:
            self.memory.append((state, reward, action, next_state, done))
        else:
            q = self.critic(torch.Tensor(state).unsqueeze(0),
                            torch.Tensor(action).unsqueeze(0))

            target_val = self.critic_target(
                torch.Tensor(next_state).unsqueeze(0),
                self.actor_target(torch.Tensor(next_state).unsqueeze(0)))

            target = reward + (self.gamma * target_val * (done * 1)).detach()
            error = abs(q - target).detach().numpy()
            self.memory.add((state, reward, action, next_state,
                             done), error)

    def soft_update_networks(self):
        self.update(self.critic_target, self.critic,
                    self.config['network_update_amount'])
        self.update(self.actor_target, self.actor,
                    self.config['network_update_amount'])

    def update(self, target, src, amount):
        for target_param, param in zip(target.parameters(),
                                       src.parameters()):
            target_param.data.copy_(
                target_param.data * (1.0 - amount) + param.data * amount)

    def sample_from_per_memory(self, batch_size):
        transition_batch, indexes, importance_sampling_weights = \
            self.memory.sample(batch_size)

        x, r, u, y, d = [], [], [], [], []
        for i in transition_batch:
            X, R, U, Y, D = i
            x.append(np.array(X, copy=False))
            y.append(np.array(Y, copy=False))
            u.append(np.array(U, copy=False))
            r.append(np.array(R, copy=False))
            d.append(np.array(D, copy=False))

        return ((np.array(x), np.array(r).reshape(-1, 1), np.array(u),
                 np.array(y), np.array(d).reshape(-1, 1)), indexes,
                importance_sampling_weights)


class AgentUtils:
    """Class providing save and load methods for Agent objects"""

    @staticmethod
    def load(agent, model_id):
        """Loads network configuration and model

        Loads from file into the Agent's
        network fields.

        Args:
            agent(Agent): an Agent object, to whom we want to load
            model_id(str): id of model which we want to load

        """

        model_path = 'saved_models/model_{}/'.format(model_id)
        conf_path = model_path + 'configuration.json'
        model_critic_path = model_path + 'critic_network.pt'
        model_actor_path = model_path + 'actor_network.pt'

        # loading configuration file
        try:
            with open(conf_path) as config_file:
                agent.config = json.load(config_file)['agent']
            agent.reset()
        except FileNotFoundError as exc:
            print("Loading model failed. No model with given index, or no" +
                  " configuration file. Error: \n")
            print(exc)
            sys.exit()

        # load network model
        agent.critic.load_state_dict(torch.load(model_critic_path))
        agent.critic_target.load_state_dict(torch.load(model_critic_path))
        agent.actor.load_state_dict(torch.load(model_actor_path))
        agent.actor_target.load_state_dict(torch.load(model_actor_path))

        # load normalizer values
        agent.normalizer.mean = torch.load(model_path + 'norm_mean.pt')
        agent.normalizer.var = torch.load(model_path + 'norm_var.pt')

    @staticmethod
    def save(model, rewards=None, success_rates=None):
        """Save model, configuration file and training rewards

        Saving to files in the saved_models/{old_id} directory.

        Args:
            model(torch.nn.Net): neural network torch model (q_network)
            rewards(list): list of total rewards for each episode, default None
            success_rates(list): list of floats - success_rates for each
            evaluation performed on agent during learning
        """

        path = 'saved_models/model_{}/'

        # create new directory with incremented id
        new_id = 0
        while True:
            if not os.path.exists(path.format(new_id)):
                os.makedirs(path.format(new_id))
                break
            new_id += 1

        # model
        torch.save(model.critic.state_dict(),
                   (path + 'critic_network.pt').format(new_id))
        torch.save(model.actor.state_dict(),
                   (path + 'actor_network.pt').format(new_id))

        # normalizer values
        torch.save(model.normalizer.mean,
                   (path + "norm_mean.pt").format(new_id))
        torch.save(model.normalizer.var,
                   (path + "norm_var.pt").format(new_id))

        # config
        config_path = 'configuration.json'
        copyfile(config_path, (path + config_path).format(new_id))

        if not rewards:
            return

        # rewards log
        with open((path + "rewards.log").format(new_id), "a") as logfile:
            for reward in rewards:
                logfile.write("{}\n".format(reward))

        # success_rates log
        with open((path + "success_rates.log").format(new_id), "a") as logfile:
            for success_rate in success_rates:
                logfile.write("{}\n".format(success_rate))

        # rewards chart
        rewards = []
        for line in open((path + 'rewards.log').format(new_id), 'r'):
            values = [float(s) for s in line.split()]
            rewards.append(values)
        avg_rewards = []
        for i in range(len(rewards) // (10 or 1)):
            avg_rewards.append(np.mean(rewards[10 * i: 10 * (i + 1)]))
        plt.plot(avg_rewards)
        plt.savefig((path + 'learning_plot.png').format(new_id))
        plt.close()

        # rewards chart
        rates = []
        for line in open((path + 'success_rates.log').format(new_id), 'r'):
            values = [float(s) for s in line.split()]
            rates.append(values)
        plt.plot(rates)
        plt.savefig((path + 'success_rates_plot.png').format(new_id))
        plt.close()

        return new_id
