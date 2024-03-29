import gym
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import Normal
import random
from datetime import datetime
import time
from tensorboardX import SummaryWriter
import numpy as np

import pdb

from sac.networks import Actor, QNet, VNet


env_name = 'Pendulum-v0'
n_episodes = 10000
hidden_size = 256
replay_buffer_size = 1e6
train_batch_size = 256
gamma = 0.99
target_smoothing_coeff = 0.005
lr = 3e-4
logstd_min = -20
logstd_max = 2
reward_scaling = 1
entropy_coeff = -1e-10

evaluate_freq = 25
seed = 0
load_path = None
save_freq = 100
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class ExpReplay:
    def __init__(self):
        self.states = []
        self.actions = []
        self.next_states = []
        self.rewards = []
        self.dones = []
        self.pointer = 0
        self.max_len = replay_buffer_size

    def add(self, exp_tuple):
        if len(self.states) < self.max_len:
            self.states.append(0)
            self.actions.append(0)
            self.rewards.append(0)
            self.next_states.append(0)
            self.dones.append(0)

        state, action, reward, next_state, done = exp_tuple
        self.states[self.pointer] = state
        self.actions[self.pointer] = action
        self.rewards[self.pointer] = reward
        self.next_states[self.pointer] = next_state
        self.dones[self.pointer] = done
        self.pointer = int((self.pointer + 1) % self.max_len)

    def sample(self, batch_size):
        st_b, ac_b, rew_b, nst_b, dn_b = \
            zip(*random.sample(list(zip(self.states,self.actions,self.rewards, self.next_states, self.dones)),
                               batch_size))
        return st_b, ac_b, rew_b, nst_b, dn_b

    def __len__(self):
        return len(self.states)


class Actor(nn.Module):
    def __init__(self, input_size, output_size, action_scale):
        super(Actor, self).__init__()

        self.fc1 = nn.Linear(input_size, hidden_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size)
        self.fc_mean = nn.Linear(hidden_size, output_size)
        self.fc_logstd = nn.Linear(hidden_size, output_size)

        self.output_size = output_size
        self.action_scale = action_scale

    def forward(self, x):

        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x_m = self.fc_mean(x)
        x_ls = torch.clamp(self.fc_logstd(x), min=logstd_min, max=logstd_max)

        return x_m, x_ls

    def get_action(self, state):

        state_th = torch.tensor(state).float().to(device)
        action_mean_th, action_logstd_th = self.forward(state_th)
        action_th_sampled = torch.tanh(Normal(action_mean_th, torch.exp(action_logstd_th)).sample())*\
                            torch.tensor(self.action_scale).to(device)
        action = action_th_sampled.cpu().detach().numpy()
        return action


class QNet(nn.Module):
    def __init__(self, state_size, action_size):
        super(QNet, self).__init__()

        self.fc1 = nn.Linear(state_size, hidden_size)
        self.fc2 = nn.Linear(hidden_size + action_size, hidden_size)
        self.fc3 = nn.Linear(hidden_size, 1)

    def forward(self, s, a):
        x = F.relu(self.fc1(s))
        x = F.relu(self.fc2(torch.cat((x, a), dim=-1)))
        x = self.fc3(x)
        return x



def optimize_model(policy, q1_net, q2_net, v_net, v_target_net, memory, actor_optimizer, q_net_optimizer, v_net_optimizer):
    if len(memory) < train_batch_size:
        return 0, 0, 0  # dummy losses for consistency in presenting results

    st_b, ac_b, rew_b, nst_b, dn_b = memory.sample(train_batch_size)
    states_th = torch.tensor(st_b).float().to(device)
    actions_th = torch.tensor(ac_b).to(device)
    rewards_th = torch.tensor(rew_b).unsqueeze(1).to(device)
    next_states_th = torch.tensor(nst_b).float().to(device)
    dones_th = torch.tensor(dn_b).float().unsqueeze(1).to(device)

    Q1_vals = q1_net(states_th, actions_th)
    Q2_vals = q2_net(states_th, actions_th)
    V_vals = v_net(states_th)
    V_next_state_vals = v_target_net(next_states_th)
    pi_action_means, pi_action_logstd = policy(states_th)
    pi_action_stds = torch.exp(pi_action_logstd)

    z = Normal(torch.zeros_like(pi_action_means), torch.ones_like(pi_action_stds)).sample()
    newly_sampled_actions = pi_action_means + z*pi_action_stds
    newly_sampled_action_log_probs = Normal(pi_action_means, pi_action_stds).log_prob(newly_sampled_actions)
    newly_sampled_Q1_vals = q1_net(states_th, newly_sampled_actions)
    newly_sampled_Q2_vals = q2_net(states_th, newly_sampled_actions)
    newly_sampled_Q_minvals = torch.min(newly_sampled_Q1_vals, newly_sampled_Q2_vals)

    J_v = torch.mean((V_vals - (newly_sampled_Q_minvals.detach() - entropy_coeff*newly_sampled_action_log_probs.detach()))**2)
    v_net_optimizer.zero_grad()
    J_v.backward()
    v_net_optimizer.step()

    J_q1 = torch.mean((Q1_vals - (rewards_th + gamma*V_next_state_vals*(1-dones_th)))**2)
    J_q2 = torch.mean((Q2_vals - (rewards_th + gamma * V_next_state_vals * (1 - dones_th))) ** 2)
    J_q = J_q1 + J_q2
    q_net_optimizer.zero_grad()
    J_q.backward()
    q_net_optimizer.step()

    J_pi = torch.mean(entropy_coeff*newly_sampled_action_log_probs - newly_sampled_Q_minvals)
    actor_optimizer.zero_grad()
    J_pi.backward()
    actor_optimizer.step()
    return J_v, J_q, J_pi


def evaluate_episode(env, policy):
    done = False
    state = env.reset()
    rew_ep = 0
    sum_std = 0
    steps = 0
    while not done:
        env.render()
        action_mean_th, action_logstd_th = policy.forward(torch.tensor(state).float().to(device))
        steps += 1
        sum_std += torch.exp(action_logstd_th).detach().cpu().numpy()
        action = torch.tanh(action_mean_th).cpu().detach().numpy()*env.action_space.high
        next_state, reward, done, _ = env.step(action)
        rew_ep += reward
        state = next_state

    print("Evaluation Reward: {}, Average Std: {}".format(rew_ep, sum_std/steps))
    return rew_ep

if __name__ == '__main__':

    env = gym.make(env_name)
    dir_name = '/tmp/rl_implementations/sac/{}-{}'.format(env_name, datetime.today().strftime("%Y-%d-%b-%H-%M-%S"))
    writer = SummaryWriter(dir_name)

    torch.manual_seed(seed)
    random.seed(seed)
    env.seed(seed)

    time_start = time.time()

    policy = Actor(env.observation_space.shape[0], env.action_space.shape[0],env.action_space.high).to(device)
    q1_net = QNet(env.observation_space.shape[0], env.action_space.shape[0]).to(device)
    q2_net = QNet(env.observation_space.shape[0], env.action_space.shape[0]).to(device)
    v_net = VNet(env.observation_space.shape[0]).to(device)
    v_target_net = VNet(env.observation_space.shape[0]).to(device)
    v_target_net.eval()
    v_target_net.load_state_dict(v_net.state_dict())

    actor_optimizer = optim.Adam(policy.parameters(), lr=lr)
    q_net_optimizer = optim.Adam([*q1_net.parameters(), *q2_net.parameters()], lr=lr)
    v_net_optimizer = optim.Adam(v_net.parameters(), lr=lr)
    memory = ExpReplay()
    steps = 0

    for ep in range(n_episodes):
        state = env.reset()
        done = False
        rew_ep = 0

        while not done:
            steps += 1
            action = policy.get_action(state)
            next_state, reward, done, info = env.step(action)
            rew_ep += reward

            exp_tuple = (state, action, reward_scaling*reward, next_state, done)
            memory.add(exp_tuple)

            loss = optimize_model(policy, q1_net, q2_net, v_net, v_target_net, memory,
                                  actor_optimizer, q_net_optimizer, v_net_optimizer)

            for t_par, par in zip(v_target_net.parameters(), v_net.parameters()):
                t_par.data = par.data*target_smoothing_coeff + t_par.data*(1-target_smoothing_coeff)

            state = next_state

        writer.add_scalar('train/V-loss', loss[0], ep)
        writer.add_scalar('train/Q-loss', loss[1], ep)
        writer.add_scalar('train/Pi-loss', loss[2], ep)
        writer.add_scalar('train/Total loss', sum(loss), ep)
        writer.add_scalar('train/steps', steps, ep)

        if ep % evaluate_freq == 0:
            rew_eval = evaluate_episode(env, policy)
            writer.add_scalar('eval/rewards', rew_eval, ep)