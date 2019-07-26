import argparse
import time

from d4pg.actors import Actor
from d4pg.learners import Learner
from d4pg.replay import ReplayBuffer_remote

# Plot results
from d4pg.utils import VisdomLinePlotter

import gym

def make_cassie_env(*args, **kwargs):
    def _thunk():
        return CassieEnv(*args, **kwargs)
    return _thunk

def gym_factory(path, **kwargs):
    from functools import partial

    """
    This is (mostly) equivalent to gym.make(), but it returns an *uninstantiated* 
    environment constructor.

    Since environments containing cpointers (e.g. Mujoco envs) can't be serialized, 
    this allows us to pass their constructors to Ray remote functions instead 
    (since the gym registry isn't shared across ray subprocesses we can't simply 
    pass gym.make() either)

    Note: env.unwrapped.spec is never set, if that matters for some reason.
    """
    spec = gym.envs.registry.spec(path)
    _kwargs = spec._kwargs.copy()
    _kwargs.update(kwargs)
    
    if callable(spec._entry_point):
        cls = spec._entry_point(**_kwargs)
    else:
        cls = gym.envs.registration.load(spec._entry_point)

    return partial(cls, **_kwargs)

parser = argparse.ArgumentParser()

# args common for actors and learners
parser.add_argument("--env_name", default="Cassie-mimic-v0")                    # environment name
parser.add_argument("--hidden_size", default=256)

# learner specific args
parser.add_argument("--replay_size", default=1e7, type=int)                     # replay buffer size    
parser.add_argument("--max_timesteps", default=1e7, type=float)                 # Max time steps to run environment for
parser.add_argument("--training_episodes", default=10000, type=float)           # Max episodes to learn from
parser.add_argument("--batch_size", default=500, type=int)                      # Batch size for both actor and critic
parser.add_argument("--discount", default=0.99, type=float)                     # exploration/exploitation discount factor
parser.add_argument("--tau", default=0.005, type=float)                         # target update rate (tau)
parser.add_argument("--eval_update_freq", default=10, type=int)                 # how often to update learner
parser.add_argument("--evaluate_freq", default=50, type=int)                    # how often to evaluate learner

# actor specific args
parser.add_argument("--num_actors", default=4, type=int)                       # Number of actors
parser.add_argument("--policy_name", default="TD3")                             # Policy name
parser.add_argument("--start_timesteps", default=1e4, type=int)                 # How many time steps purely random policy is run for
parser.add_argument("--initial_load_freq", default=10, type=int)                # initial amount of time between loading global model
parser.add_argument("--act_noise", default=0.3, type=float)                     # Std of Gaussian exploration noise (used to be 0.1)
parser.add_argument('--param_noise', type=bool, default=True)                   # param noise
parser.add_argument('--noise_scale', type=float, default=0.3)                   # noise scale for param noise
parser.add_argument("--taper_load_freq", type=bool, default=True)               # initial amount of time between loading global model
parser.add_argument("--viz_actors", type=bool, default=True)                    # Visualize actors in visdom or not

# evaluator args
parser.add_argument("--num_trials", default=10, type=int)                       # Number of evaluators
parser.add_argument("--num_evaluators", default=4, type=int)                   # Number of evaluators
parser.add_argument("--viz_port", default=8097)                                 # visdom server port

args = parser.parse_args()

import ray
ray.init(num_gpus=0, include_webui=True, temp_dir="./ray_tmp")

if __name__ == "__main__":
    #torch.set_num_threads(4)

    # Experiment Name
    experiment_name = "{}_{}_{}".format(args.policy_name, args.env_name, args.num_actors)
    print("DISTRIBUTED Policy: {}\nEnvironment: {}\n# of Actors:{}".format(args.policy_name, args.env_name, args.num_actors))

    # Environment
    if(args.env_name in ["Cassie-v0", "Cassie-mimic-v0", "Cassie-mimic-walking-v0"]):
        # set up cassie environment
        import gym_cassie
        env = gym.make("Cassie-mimic-v0")
        max_episode_steps = 400
    else:
        env_fn = gym_factory(args.env_name)
        #max_episode_steps = env_fn()._max_episode_steps
        max_episode_steps = 1000

    # Visdom Monitoring
    obs_dim = env_fn().observation_space.shape[0]
    action_dim = env_fn().action_space.shape[0]

    # create remote visdom logger
    # plotter_id = VisdomLinePlotter.remote(env_name=experiment_name, port=args.viz_port)

    # Create remote learner (learner will create the evaluators) and replay buffer
    memory_id = ReplayBuffer_remote.remote(args.replay_size, experiment_name, args.viz_port)
    learner_id = Learner.remote(env_fn, memory_id, args.training_episodes, obs_dim, action_dim, batch_size=args.batch_size, discount=args.discount, eval_update_freq=args.eval_update_freq, evaluate_freq=args.evaluate_freq, num_of_evaluators=args.num_evaluators)

    # Create remote actors
    actors_ids = [Actor.remote(env_fn, learner_id, memory_id, action_dim, args.start_timesteps // args.num_actors, args.initial_load_freq, args.taper_load_freq, args.act_noise, args.noise_scale, args.param_noise, i) for i in range(args.num_actors)]

    # # start learner loop process (non-blocking)
    # learner_id.update_and_evaluate.remote()

    start = time.time()

    # start collection loop for each actor
    ray.wait([actors_ids[i].collect_experience.remote() for i in range(args.num_actors)], num_returns=args.num_actors)

    # get results from learner
    results, evaluation_freq = ray.get(learner_id.get_results.remote())

    end = time.time()

    # also dump ray timeline
    ray.global_state.chrome_tracing_dump(filename="./ray_timeline.json")