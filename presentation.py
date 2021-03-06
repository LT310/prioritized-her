import gym
import gym.spaces

from agent import Agent
from agent import AgentUtils

"""Shorter script for live presentation of the given model without learning.
Exit with Esc button"""


def main():
    env = gym.make("FetchPush-v1")
    # env = gym.make("FetchReach-v1")
    agent = Agent(env)

    model_id = input('Model ID:\n')
    agent.reset()
    AgentUtils.load(agent, model_id)

    while True:
        _run_presentation(agent, env)


def _run_presentation(agent, env):
    done = False
    state = env.reset()
    state = agent.normalizer.normalize(state)

    while not done:
        env.render()
        action = agent.get_action_greedy(state)
        state, reward, done, info = env.step(action)
        state = agent.normalizer.normalize(state)


if __name__ == "__main__":
    main()
