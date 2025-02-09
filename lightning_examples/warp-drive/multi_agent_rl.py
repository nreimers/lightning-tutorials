# %% [markdown]
# **⚠️ PLEASE NOTE:**
# This notebook runs on a GPU runtime. If running on Colab, choose Runtime > Change runtime type from the menu, then select `GPU` in the 'Hardware accelerator' dropdown menu.

# %% [markdown]
# ## Introduction

# %% [markdown]
# This tutorial provides a demonstration of a multi-agent Reinforcement Learning (RL) training loop with [WarpDrive](https://github.com/salesforce/warp-drive). WarpDrive is a flexible, lightweight, and easy-to-use RL framework that implements end-to-end deep multi-agent RL on a single GPU (Graphics Processing Unit). Using the extreme parallelization capability of GPUs, it enables [orders-of-magnitude faster RL](https://arxiv.org/abs/2108.13976) compared to common implementations that blend CPU simulations and GPU models. WarpDrive is extremely efficient as it runs simulations across multiple agents and multiple environment replicas in parallel and completely eliminates the back-and-forth data copying between the CPU and the GPU.
#
# We have integrated WarpDrive with the [Pytorch Lightning](https://www.pytorchlightning.ai/) framework, which greatly reduces the trainer boilerplate code, and improves training flexibility.
#
# Below, we demonstrate how to use WarpDrive and PytorchLightning together to train a game of [Tag](https://github.com/salesforce/warp-drive/blob/master/example_envs/tag_continuous/tag_continuous.py) where multiple *tagger* agents are trying to run after and tag multiple other *runner* agents. As such, the Warpdrive framework comprises several utility functions that help easily implement any (OpenAI-)*gym-style* RL environment, and furthermore, provides quality-of-life tools to train it end-to-end using just a few lines of code. You may familiarize yourself with WarpDrive with the help of these [tutorials](https://github.com/salesforce/warp-drive/tree/master/tutorials).
#
# We invite everyone to **contribute to WarpDrive**, including adding new multi-agent environments, proposing new features and reporting issues on our open source [repository](https://github.com/salesforce/warp-drive).


# %%
import logging

import matplotlib.pyplot as plt
import mpl_toolkits.mplot3d.art3d as art3d
import numpy as np
import torch
from example_envs.tag_continuous.tag_continuous import TagContinuous

# from IPython.display import HTML
from matplotlib import animation
from matplotlib.patches import Polygon
from pytorch_lightning import Trainer
from warp_drive.env_wrapper import EnvWrapper
from warp_drive.training.pytorch_lightning_trainer import CudaCallback, PerfStatsCallback, WarpDriveModule

# %%
_NUM_AVAILABLE_GPUS = torch.cuda.device_count()
assert _NUM_AVAILABLE_GPUS > 0, "This notebook needs a GPU to run!"

# %%
# Set logger level e.g., DEBUG, INFO, WARNING, ERROR.
logging.getLogger().setLevel(logging.ERROR)

# %% [markdown]
# ## Specify a set of run configurations for your experiments
#
# The run configuration is a dictionary comprising the environment parameters, the trainer and the policy network settings, as well as configurations for saving.
#
# For our experiment, we consider an environment wherein $5$ taggers and $100$ runners play the game of [Tag](https://github.com/salesforce/warp-drive/blob/master/example_envs/tag_continuous/tag_continuous.py) on a $20 \times 20$ plane. The game lasts $200$ timesteps. Each agent chooses it's own acceleration and turn actions at every timestep, and we use mechanics to determine how the agents move over the grid. When a tagger gets close to a runner, the runner is tagged, and is eliminated from the game. For the configuration below, the runners and taggers have the same unit skill levels, or top speeds.
#
# We train the agents using $50$ environments or simulations running in parallel. With WarpDrive, each simulation runs on sepate GPU blocks.
#
# There are two separate policy networks used for the tagger and runner agents. Each network is a fully-connected model with two layers each of $256$ dimensions. We use the Advantage Actor Critic (A2C) algorithm for training. WarpDrive also currently provides the option to use the Proximal Policy Optimization (PPO) algorithm instead.

# %%
run_config = dict(
    name="tag_continuous",
    # Environment settings.
    env=dict(
        # number of taggers in the environment
        num_taggers=5,
        # number of runners in the environment
        num_runners=100,
        # length of the (square) grid on which the game is played
        grid_length=20.0,
        # episode length in timesteps
        episode_length=200,
        # maximum acceleration
        max_acceleration=0.1,
        # minimum acceleration
        min_acceleration=-0.1,
        # 3*pi/4 radians
        max_turn=2.35,
        # -3*pi/4 radians
        min_turn=-2.35,
        # number of discretized accelerate actions
        num_acceleration_levels=10,
        # number of discretized turn actions
        num_turn_levels=10,
        # skill level for the tagger
        skill_level_tagger=1.0,
        # skill level for the runner
        skill_level_runner=1.0,
        # each agent only sees full or partial information
        use_full_observation=False,
        # flag to indicate if a runner stays in the game after getting tagged
        runner_exits_game_after_tagged=True,
        # number of other agents each agent can see
        num_other_agents_observed=10,
        # positive reward for the tagger upon tagging a runner
        tag_reward_for_tagger=10.0,
        # negative reward for the runner upon getting tagged
        tag_penalty_for_runner=-10.0,
        # reward at the end of the game for a runner that isn't tagged
        end_of_game_reward_for_runner=1.0,
        # margin between a tagger and runner to consider the runner as 'tagged'.
        tagging_distance=0.02,
    ),
    # Trainer settings.
    trainer=dict(
        # number of environment replicas (number of GPU blocks used)
        num_envs=50,
        # total batch size used for training per iteration (across all the environments)
        train_batch_size=10000,
        # total number of episodes to run the training for (can be arbitrarily high!)
        num_episodes=50000,
    ),
    # Policy network settings.
    policy=dict(
        runner=dict(
            # flag indicating whether the model needs to be trained
            to_train=True,
            # algorithm used to train the policy
            algorithm="A2C",
            # discount rate
            gamma=0.98,
            # learning rate
            lr=0.005,
            # policy model settings
            model=dict(type="fully_connected", fc_dims=[256, 256], model_ckpt_filepath=""),
        ),
        tagger=dict(
            to_train=True,
            algorithm="A2C",
            gamma=0.98,
            lr=0.002,
            model=dict(type="fully_connected", fc_dims=[256, 256], model_ckpt_filepath=""),
        ),
    ),
    # Checkpoint saving setting.
    saving=dict(
        # how often (in iterations) to print the metrics
        metrics_log_freq=10,
        # how often (in iterations) to save the model parameters
        model_params_save_freq=5000,
        # base folder used for saving
        basedir="/tmp",
        # experiment name
        name="continuous_tag",
        # experiment tag
        tag="example",
    ),
)

# %% [markdown]
# ## Instantiate the WarpDrive Module
#
# In order to instantiate the WarpDrive module, we first use an environment wrapper to specify that the environment needs to be run on the GPU (via the `use_cuda` flag). Also, agents in the environment can share policy models; so we specify a dictionary to map each policy network model to the list of agent ids using that model.

# %%
# Create a wrapped environment object via the EnvWrapper.
# Ensure that use_cuda is set to True (in order to run on the GPU).
env_wrapper = EnvWrapper(
    TagContinuous(**run_config["env"]),
    num_envs=run_config["trainer"]["num_envs"],
    use_cuda=True,
)

# Agents can share policy models: this dictionary maps policy model names to agent ids.
policy_tag_to_agent_id_map = {
    "tagger": list(env_wrapper.env.taggers),
    "runner": list(env_wrapper.env.runners),
}

wd_module = WarpDriveModule(
    env_wrapper=env_wrapper,
    config=run_config,
    policy_tag_to_agent_id_map=policy_tag_to_agent_id_map,
    verbose=True,
)


# %% [markdown]
# ## Visualizing an episode roll-out before training
#
# We have created a helper function (see below) to visualize an episode rollout. Internally, this function uses the WarpDrive module's `fetch_episode_states` API to fetch the data arrays on the GPU for the duration of an entire episode. Specifically, we fetch the state arrays pertaining to agents' x and y locations on the plane and indicators on which agents are still active in the game. Note that this function may be invoked at any time during training, and it will use the state of the policy models at that time to sample actions and generate the visualization.

# %%
def generate_tag_env_rollout_animation(
    warp_drive_module,
    fps=25,
    tagger_color="#C843C3",
    runner_color="#245EB6",
    runner_not_in_game_color="#666666",
    fig_width=6,
    fig_height=6,
):
    assert warp_drive_module is not None
    episode_states = warp_drive_module.fetch_episode_states(["loc_x", "loc_y", "still_in_the_game"])
    assert isinstance(episode_states, dict)
    env = warp_drive_module.cuda_envs.env

    fig, ax = plt.subplots(1, 1, figsize=(fig_width, fig_height))  # , constrained_layout=True
    ax.remove()
    ax = fig.add_subplot(1, 1, 1, projection="3d")

    # Bounds
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_zlim(-0.01, 0.01)

    # Surface
    corner_points = [(0, 0), (0, 1), (1, 1), (1, 0)]

    poly = Polygon(corner_points, color=(0.1, 0.2, 0.5, 0.15))
    ax.add_patch(poly)
    art3d.pathpatch_2d_to_3d(poly, z=0, zdir="z")

    # "Hide" side panes
    ax.xaxis.set_pane_color((1.0, 1.0, 1.0, 0.0))
    ax.yaxis.set_pane_color((1.0, 1.0, 1.0, 0.0))
    ax.zaxis.set_pane_color((1.0, 1.0, 1.0, 0.0))

    # Hide grid lines
    ax.grid(False)

    # Hide axes ticks
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_zticks([])

    # Hide axes
    ax.set_axis_off()

    # Set camera
    ax.elev = 40
    ax.azim = -55
    ax.dist = 10

    # Try to reduce whitespace
    fig.subplots_adjust(left=0, right=1, bottom=-0.2, top=1)

    # Plot init data
    lines = [None for _ in range(env.num_agents)]

    for idx in range(len(lines)):
        if idx in env.taggers:
            lines[idx] = ax.plot3D(
                episode_states["loc_x"][:1, idx] / env.grid_length,
                episode_states["loc_y"][:1, idx] / env.grid_length,
                0,
                color=tagger_color,
                marker="o",
                markersize=10,
            )[0]
        else:  # runners
            lines[idx] = ax.plot3D(
                episode_states["loc_x"][:1, idx] / env.grid_length,
                episode_states["loc_y"][:1, idx] / env.grid_length,
                [0],
                color=runner_color,
                marker="o",
                markersize=5,
            )[0]

    init_num_runners = env.num_agents - env.num_taggers

    def _get_label(timestep, n_runners_alive, init_n_runners):
        line1 = "Continuous Tag\n"
        line2 = "Time Step:".ljust(14) + f"{timestep:4.0f}\n"
        frac_runners_alive = n_runners_alive / init_n_runners
        pct_runners_alive = f"{n_runners_alive:4} ({frac_runners_alive * 100:.0f}%)"
        line3 = "Runners Left:".ljust(14) + pct_runners_alive
        return line1 + line2 + line3

    label = ax.text(
        0,
        0,
        0.02,
        _get_label(0, init_num_runners, init_num_runners).lower(),
    )

    label.set_fontsize(14)
    label.set_fontweight("normal")
    label.set_color("#666666")

    def animate(i):
        for idx, line in enumerate(lines):
            line.set_data_3d(
                episode_states["loc_x"][i : i + 1, idx] / env.grid_length,
                episode_states["loc_y"][i : i + 1, idx] / env.grid_length,
                np.zeros(1),
            )

            still_in_game = episode_states["still_in_the_game"][i, idx]

            if still_in_game:
                pass
            else:
                line.set_color(runner_not_in_game_color)
                line.set_marker("")

        n_runners_alive = episode_states["still_in_the_game"][i].sum() - env.num_taggers
        label.set_text(_get_label(i, n_runners_alive, init_num_runners).lower())

    ani = animation.FuncAnimation(fig, animate, np.arange(0, env.episode_length + 1), interval=1000.0 / fps)
    plt.close()

    return ani


# %% [markdown]
# The animation below shows a sample realization of the game episode before training, i.e., with randomly chosen agent actions. The $5$ taggers are marked in pink, while the $100$ blue agents are the runners. Both the taggers and runners move around randomly and about half the runners remain at the end of the episode.

# %%

# anim = generate_tag_env_rollout_animation(wd_module)
# HTML(anim.to_html5_video())

# %% [markdown]
# ## Create the Lightning Trainer
#
# Next, we create the trainer for training the WarpDrive model. We add the `performance stats` callbacks to the trainer to view the throughput performance of WarpDrive.

# %%
log_freq = run_config["saving"]["metrics_log_freq"]

# Define callbacks.
cuda_callback = CudaCallback(module=wd_module)
perf_stats_callback = PerfStatsCallback(
    batch_size=wd_module.training_batch_size,
    num_iters=wd_module.num_iters,
    log_freq=log_freq,
)

# Instantiate the PytorchLightning trainer with the callbacks.
# Also, set the number of gpus to 1, since this notebook uses just a single GPU.
num_gpus = 1
num_episodes = run_config["trainer"]["num_episodes"]
episode_length = run_config["env"]["episode_length"]
training_batch_size = run_config["trainer"]["train_batch_size"]
num_epochs = num_episodes * episode_length / training_batch_size

trainer = Trainer(
    accelerator="gpu",
    devices=num_gpus,
    callbacks=[cuda_callback, perf_stats_callback],
    max_epochs=num_epochs,
)

# %%
# Start tensorboard.
# %load_ext tensorboard
# %tensorboard --logdir lightning_logs/

# %% [markdown]
# ## Train the WarpDrive Module
#
# Finally, we invoke training.
#
# Note: please scroll up to the tensorboard cell to visualize the curves during training.

# %%
trainer.fit(wd_module)

# %% [markdown]
# ## Visualize an episode-rollout after training

# %%

# anim = generate_tag_env_rollout_animation(wd_module)
# HTML(anim.to_html5_video())

# %% [markdown]
# Note: In the configuration above, we have set the trainer to only train on $50000$ rollout episodes, but you can increase the `num_episodes` configuration parameter to train further. As more training happens, the runners learn to escape the taggers, and the taggers learn to chase after the runner. Sometimes, the taggers also collaborate to team-tag runners. A good number of episodes to train on (for the configuration we have used) is $2$M or higher.

# %%
# Finally, close the WarpDrive module to clear up the CUDA memory heap
wd_module.graceful_close()
