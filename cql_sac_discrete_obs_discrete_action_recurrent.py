import argparse
import os
import random
import time
from distutils.util import strtobool
import pickle

import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F
import torch.optim as optim

import wandb
from common.models import (
    RecurrentDiscreteActorDiscreteObs,
    RecurrentDiscreteCriticDiscreteObs,
)
from common.replay_buffer import EpisodicReplayBuffer
from common.utils import make_env, save, set_seed


def parse_args():
    # fmt: off
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp-name", type=str, default=os.path.basename(__file__).rstrip(".py"),
        help="the name of this experiment")
    parser.add_argument("--seed", type=int, default=1,
        help="seed of the experiment")
    parser.add_argument("--cuda", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, cuda will be enabled by default")
    parser.add_argument("--wandb-project", type=str, default="cql-sac-discrete-obs-discrete-action-recurrent",
        help="wandb project name")
    parser.add_argument("--wandb-group", type=str, default=None,
        help="wandb group name to use for run")
    parser.add_argument("--wandb-dir", type=str, default="./",
        help="the wandb directory")
    parser.add_argument("--capture-video", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="whether to capture videos of the agent performances (check out `videos` folder)")

    # Algorithm specific arguments
    parser.add_argument("--env-id", type=str, default="POMDP-heavenhell_1-episodic-v0",
        help="the id of the environment")
    parser.add_argument("--total-timesteps", type=int, default=200500,
        help="total timesteps of the experiments")
    parser.add_argument("--maximum-episode-length", type=int, default=50,
        help="maximum length for episodes for gym POMDP environment")
    parser.add_argument("--buffer-size", type=int, default=int(1e5),
        help="the replay memory buffer size")
    parser.add_argument("--gamma", type=float, default=0.99,
        help="the discount factor gamma")
    parser.add_argument("--tau", type=float, default=0.005,
        help="target smoothing coefficient (default: 0.005)")
    parser.add_argument("--batch-size", type=int, default=256,
        help="the batch size of sample from the reply memory")
    parser.add_argument("--learning-starts", type=int, default=5e3,
        help="timestep to start learning")
    parser.add_argument("--policy-lr", type=float, default=3e-5,
        help="the learning rate of the policy network optimizer")
    parser.add_argument("--q-lr", type=float, default=3e-4,
        help="the learning rate of the Q network network optimizer")
    parser.add_argument("--policy-frequency", type=int, default=2,
        help="the frequency of training policy (delayed)")
    parser.add_argument("--target-network-frequency", type=int, default=1, # Denis Yarats' implementation delays this by 2.
        help="the frequency of updates for the target networks")
    parser.add_argument("--alpha", type=float, default=0.2,
            help="Entropy regularization coefficient.")
    parser.add_argument("--autotune", type=lambda x:bool(strtobool(x)), default=True, nargs="?", const=True,
        help="automatic tuning of the entropy coefficient")

    # CQL specific arguments
    parser.add_argument("--cql-alpha", type=float, default=1.0,
            help="CQL regularizer scaling coefficient.")
    parser.add_argument("--cql-autotune", type=lambda x:bool(strtobool(x)), default=True, nargs="?", const=True,
        help="automatic tuning of the CQL regularizer coefficient")
    parser.add_argument("--cql-tau", type=float, default=10.0,
            help="Threshold used for automatic tuning of CQL regularizer coefficient")

    # Offline training specific arguments
    parser.add_argument("--dataset-path", type=str, default="/home/chulabhaya/phd/research/data/heavenhell_1/3-23-23_heavenhell_1_sac_expert_policy_expert_data.pkl",
        help="path to dataset for training")
    parser.add_argument("--num-evals", type=int, default=10,
        help="number of evaluation episodes to generate per evaluation during training")
    parser.add_argument("--eval-freq", type=int, default=1000,
        help="timestep frequency at which to run evaluation")

    # Checkpointing specific arguments
    parser.add_argument("--save", type=lambda x:bool(strtobool(x)), default=True, nargs="?", const=True,
        help="checkpoint saving during training")
    parser.add_argument("--save-checkpoint-dir", type=str, default="./trained_models/",
        help="path to directory to save checkpoints in")
    parser.add_argument("--checkpoint-interval", type=int, default=5000,
        help="how often to save checkpoints during training (in timesteps)")
    parser.add_argument("--resume", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="whether to resume training from a checkpoint")
    parser.add_argument("--resume-checkpoint-path", type=str, default="gymnasium_seed_4_global_step_120000.pth",
        help="path to checkpoint to resume training from")
    parser.add_argument("--run-id", type=str, default=None,
        help="wandb unique run id for resuming")

    args = parser.parse_args()
    # fmt: on
    return args


def eval_policy(
    actor,
    env_name,
    maximum_episode_length,
    seed,
    seed_offset,
    global_step,
    capture_video,
    run_name,
    num_evals,
    data_log,
):
    # Put actor model in evaluation mode
    actor.eval()

    with torch.no_grad():
        # Initialization
        run_name_full = run_name + "__eval__" + str(global_step)
        env = make_env(
            env_name,
            seed + seed_offset,
            capture_video,
            run_name_full,
            max_episode_len=maximum_episode_length,
        )
        # Track averages
        avg_episodic_return = 0
        avg_episodic_length = 0
        # Start evaluation
        obs, info = env.reset(seed=seed + seed_offset)
        for _ in range(num_evals):
            in_hidden = None
            terminated, truncated = False, False
            while not (truncated or terminated):
                # Get action
                seq_lengths = torch.LongTensor([1])
                action, _, _, out_hidden = actor.get_action(
                    torch.tensor(obs).to(device).view(1, -1), seq_lengths, in_hidden
                )
                action = action.view(-1).detach().cpu().numpy()[0]
                in_hidden = out_hidden

                # Take step in environment
                next_obs, reward, terminated, truncated, info = env.step(action)

                # Update next obs
                obs = next_obs
            avg_episodic_return += info["episode"]["r"][0]
            avg_episodic_length += info["episode"]["l"][0]
            obs, info = env.reset()
        # Update averages
        avg_episodic_return /= num_evals
        avg_episodic_length /= num_evals
        print(
            f"global_step={global_step}, episodic_return={avg_episodic_return}, episodic_length={avg_episodic_length}",
            flush=True,
        )
        data_log["misc/episodic_return"] = avg_episodic_return
        data_log["misc/episodic_length"] = avg_episodic_length

    # Put actor model back in training mode
    actor.train()


if __name__ == "__main__":
    args = parse_args()
    run_name = f"{args.exp_name}"
    wandb_id = wandb.util.generate_id()
    run_id = f"{run_name}_{wandb_id}"

    # If a unique wandb run id is given, then resume from that, otherwise
    # generate new run for resuming
    if args.resume and args.run_id is not None:
        wandb.init(
            id=args.run_id,
            dir=args.wandb_dir,
            project=args.wandb_project,
            resume="must",
            mode="offline",
        )
    else:
        wandb.init(
            id=run_id,
            dir=args.wandb_dir,
            project=args.wandb_project,
            config=vars(args),
            name=run_name,
            save_code=True,
            settings=wandb.Settings(code_dir="."),
            group=args.wandb_group,
            mode="offline",
        )

    # Set training device
    device = torch.device("cuda" if torch.cuda.is_available() and args.cuda else "cpu")
    print("Running on the following device: " + device.type, flush=True)

    # Set seeding
    set_seed(args.seed, device)

    # Load checkpoint if resuming
    if args.resume:
        print("Resuming from checkpoint: " + args.resume_checkpoint_path, flush=True)
        checkpoint = torch.load(args.resume_checkpoint_path)

    # Set RNG state for seeds if resuming
    if args.resume:
        random.setstate(checkpoint["rng_states"]["random_rng_state"])
        np.random.set_state(checkpoint["rng_states"]["numpy_rng_state"])
        torch.set_rng_state(checkpoint["rng_states"]["torch_rng_state"])
        if device.type == "cuda":
            torch.cuda.set_rng_state(checkpoint["rng_states"]["torch_cuda_rng_state"])
            torch.cuda.set_rng_state_all(
                checkpoint["rng_states"]["torch_cuda_rng_state_all"]
            )

    # Env setup
    env = make_env(
        args.env_id,
        args.seed,
        args.capture_video,
        run_name,
        max_episode_len=args.maximum_episode_length,
    )
    assert isinstance(
        env.action_space, gym.spaces.Discrete
    ), "only discrete action space is supported"

    # Initialize models and optimizers
    actor = RecurrentDiscreteActorDiscreteObs(env).to(device)
    qf1 = RecurrentDiscreteCriticDiscreteObs(env).to(device)
    qf2 = RecurrentDiscreteCriticDiscreteObs(env).to(device)
    qf1_target = RecurrentDiscreteCriticDiscreteObs(env).to(device)
    qf2_target = RecurrentDiscreteCriticDiscreteObs(env).to(device)
    qf1_target.load_state_dict(qf1.state_dict())
    qf2_target.load_state_dict(qf2.state_dict())
    q_optimizer = optim.Adam(
        list(qf1.parameters()) + list(qf2.parameters()), lr=args.q_lr
    )
    actor_optimizer = optim.Adam(list(actor.parameters()), lr=args.policy_lr)

    # If resuming training, load models and optimizers
    if args.resume:
        actor.load_state_dict(checkpoint["model_state_dict"]["actor_state_dict"])
        qf1.load_state_dict(checkpoint["model_state_dict"]["qf1_state_dict"])
        qf2.load_state_dict(checkpoint["model_state_dict"]["qf2_state_dict"])
        qf1_target.load_state_dict(
            checkpoint["model_state_dict"]["qf1_target_state_dict"]
        )
        qf2_target.load_state_dict(
            checkpoint["model_state_dict"]["qf2_target_state_dict"]
        )
        q_optimizer.load_state_dict(checkpoint["optimizer_state_dict"]["q_optimizer"])
        actor_optimizer.load_state_dict(
            checkpoint["optimizer_state_dict"]["actor_optimizer"]
        )

    # Automatic entropy tuning
    if args.autotune:
        target_entropy = -0.3 * torch.log(1 / torch.tensor(env.action_space.n))
        if args.resume:
            log_alpha = checkpoint["model_state_dict"]["log_alpha"]
        else:
            log_alpha = torch.zeros(1, requires_grad=True, device=device)
        a_optimizer = optim.Adam([log_alpha], lr=args.q_lr, eps=1e-4)
        # If resuming, load optimizer
        if args.resume:
            a_optimizer.load_state_dict(
                checkpoint["optimizer_state_dict"]["a_optimizer"]
            )

        alpha = log_alpha.exp().item()
    else:
        alpha = args.alpha

    # Automatic CQL regularizer coefficient tuning
    if args.cql_autotune:
        if args.resume:
            cql_log_alpha = checkpoint["model_state_dict"]["cql_log_alpha"]
        else:
            cql_log_alpha = torch.zeros(1, requires_grad=True, device=device)
        cql_a_optimizer = optim.Adam([cql_log_alpha], lr=args.q_lr, eps=1e-4)
        # If resuming, load optimizer
        if args.resume:
            cql_a_optimizer.load_state_dict(
                checkpoint["optimizer_state_dict"]["cql_a_optimizer"]
            )
    else:
        cql_alpha = torch.tensor(args.cql_alpha)

    # Load dataset
    dataset = pickle.load(open(args.dataset_path, "rb"))

    # Initialize replay buffer
    env.observation_space.dtype = np.float32
    rb = EpisodicReplayBuffer(
        args.buffer_size,
        device,
    )
    rb.load_buffer(dataset)

    # If resuming training, then load previous replay buffer
    if args.resume:
        rb_data = checkpoint["replay_buffer"]
        rb.load_buffer(rb_data)

    # Start time tracking for run
    start_time = time.time()

    # Start the game
    start_global_step = 0
    # If resuming, update starting step
    if args.resume:
        start_global_step = checkpoint["global_step"] + 1

    in_hidden = None
    obs, info = env.reset(seed=args.seed)
    # Set RNG state for env
    if args.resume:
        env.np_random.bit_generator.state = checkpoint["rng_states"]["env_rng_state"]
        env.action_space.np_random.bit_generator.state = checkpoint["rng_states"][
            "env_action_space_rng_state"
        ]
        env.observation_space.np_random.bit_generator.state = checkpoint["rng_states"][
            "env_obs_space_rng_state"
        ]
    for global_step in range(start_global_step, args.total_timesteps):
        # Store values for data logging for each global step
        data_log = {}

        # sample data from replay buffer
        (
            observations,
            actions,
            next_observations,
            rewards,
            terminateds,
            seq_lengths,
        ) = rb.sample(args.batch_size)
        # ---------- update critic ---------- #
        # no grad because target networks are updated separately (pg. 6 of
        # updated SAC paper)
        with torch.no_grad():
            _, next_state_action_probs, next_state_log_pis, _ = actor.get_action(
                next_observations, seq_lengths
            )
            # two Q-value estimates for reducing overestimation bias (pg. 8 of updated SAC paper)
            qf1_next_target = qf1_target(next_observations, seq_lengths)
            qf2_next_target = qf2_target(next_observations, seq_lengths)
            min_qf_next_target = torch.min(qf1_next_target, qf2_next_target)
            # calculate eq. 3 in updated SAC paper
            qf_next_target = next_state_action_probs * (
                min_qf_next_target - alpha * next_state_log_pis
            )
            # calculate eq. 2 in updated SAC paper
            next_q_value = rewards + (
                (1 - terminateds) * args.gamma * qf_next_target.sum(dim=2).unsqueeze(-1)
            )

        # calculate eq. 5 in updated SAC paper
        qf1_values = qf1(observations, seq_lengths)
        qf2_values = qf2(observations, seq_lengths)
        qf1_a_values = qf1_values.gather(2, actions)
        qf2_a_values = qf2_values.gather(2, actions)
        q_loss_mask = torch.unsqueeze(
            torch.arange(torch.max(seq_lengths))[:, None] < seq_lengths[None, :], 2
        ).to(device)
        q_loss_mask_nonzero_elements = torch.sum(q_loss_mask).to(device)
        qf1_loss = (
            torch.sum(
                F.mse_loss(qf1_a_values, next_q_value, reduction="none") * q_loss_mask
            )
            / q_loss_mask_nonzero_elements
        )
        qf2_loss = (
            torch.sum(
                F.mse_loss(qf2_a_values, next_q_value, reduction="none") * q_loss_mask
            )
            / q_loss_mask_nonzero_elements
        )
        qf_loss = 0.5 * (qf1_loss + qf2_loss)  # scaling added from CQL

        # calculate CQL regularization loss
        qf1_policy_action_value = (
            torch.sum(torch.logsumexp(qf1_values, dim=2, keepdim=True) * q_loss_mask)
            / q_loss_mask_nonzero_elements
        )
        qf1_data_action_value = (
            torch.sum(qf1_a_values * q_loss_mask) / q_loss_mask_nonzero_elements
        )
        cql_qf1_diff = qf1_policy_action_value - qf1_data_action_value
        qf2_policy_action_value = (
            torch.sum(torch.logsumexp(qf2_values, dim=2, keepdim=True) * q_loss_mask)
            / q_loss_mask_nonzero_elements
        )
        qf2_data_action_value = (
            torch.sum(qf2_a_values * q_loss_mask) / q_loss_mask_nonzero_elements
        )
        cql_qf2_diff = qf2_policy_action_value - qf2_data_action_value
        if args.cql_autotune:
            cql_alpha = torch.clamp(torch.exp(cql_log_alpha), min=0.0, max=1000000.0)
            cql_qf1_loss = cql_alpha * (cql_qf1_diff - args.cql_tau)
            cql_qf2_loss = cql_alpha * (cql_qf2_diff - args.cql_tau)
            cql_alpha_loss = -(cql_qf1_loss + cql_qf2_loss)

            # ---------- update cql_alpha ---------- #
            cql_a_optimizer.zero_grad()
            cql_alpha_loss.backward(retain_graph=True)
            cql_a_optimizer.step()
        else:
            cql_qf1_loss = cql_alpha * cql_qf1_diff
            cql_qf2_loss = cql_alpha * cql_qf2_diff

        # calculate final q-function loss which is a combination of Bellman error and CQL regularization
        cql_qf_loss = cql_qf1_loss + cql_qf2_loss
        qf_loss = cql_qf_loss + qf_loss

        # calculate eq. 6 in updated SAC paper
        q_optimizer.zero_grad()
        qf_loss.backward()
        q_optimizer.step()

        # ---------- update actor ---------- #
        if global_step % args.policy_frequency == 0:  # TD 3 Delayed update support
            for _ in range(
                args.policy_frequency
            ):  # compensate for the delay by doing 'actor_update_interval' instead of 1
                _, state_action_probs, state_action_log_pis, _ = actor.get_action(
                    observations, seq_lengths
                )

                # no grad because q-networks are updated separately
                with torch.no_grad():
                    qf1_pi = qf1(observations, seq_lengths)
                    qf2_pi = qf2(observations, seq_lengths)
                    min_qf_pi = torch.min(qf1_pi, qf2_pi)

                # calculate eq. 7 in updated SAC paper
                actor_loss_mask = torch.repeat_interleave(
                    q_loss_mask, env.action_space.n, 2
                )
                actor_loss_mask_nonzero_elements = torch.sum(actor_loss_mask)
                actor_loss = state_action_probs * (
                    (alpha * state_action_log_pis) - min_qf_pi
                )
                actor_loss = (
                    torch.sum(actor_loss * actor_loss_mask)
                    / actor_loss_mask_nonzero_elements
                )

                # calculate eq. 9 in updated SAC paper
                actor_optimizer.zero_grad()
                actor_loss.backward()
                actor_optimizer.step()

                # ---------- update alpha ---------- #
                if args.autotune:
                    # no grad because actor network is updated separately
                    with torch.no_grad():
                        (
                            _,
                            state_action_probs,
                            state_action_log_pis,
                            _,
                        ) = actor.get_action(observations, seq_lengths)
                    # calculate eq. 18 in updated SAC paper
                    alpha_loss = state_action_probs * (
                        -log_alpha * (state_action_log_pis + target_entropy)
                    )
                    alpha_loss = (
                        torch.sum(alpha_loss * actor_loss_mask)
                        / actor_loss_mask_nonzero_elements
                    )

                    # calculate gradient of eq. 18
                    a_optimizer.zero_grad()
                    alpha_loss.backward()
                    a_optimizer.step()
                    alpha = log_alpha.exp().item()

        # update the target networks
        if global_step % args.target_network_frequency == 0:
            for param, target_param in zip(qf1.parameters(), qf1_target.parameters()):
                target_param.data.copy_(
                    args.tau * param.data + (1 - args.tau) * target_param.data
                )  # "update target network weights" line in page 8, algorithm 1,
                # in updated SAC paper
            for param, target_param in zip(qf2.parameters(), qf2_target.parameters()):
                target_param.data.copy_(
                    args.tau * param.data + (1 - args.tau) * target_param.data
                )

        if global_step % 100 == 0:
            data_log["losses/qf1_values"] = (
                torch.sum(qf1_a_values * q_loss_mask) / q_loss_mask_nonzero_elements
            ).item()
            data_log["losses/qf2_values"] = (
                torch.sum(qf2_a_values * q_loss_mask) / q_loss_mask_nonzero_elements
            ).item()
            data_log["losses/qf1_loss"] = qf1_loss.item()
            data_log["losses/qf2_loss"] = qf2_loss.item()
            data_log["losses/cql_qf1_loss"] = cql_qf1_loss.item()
            data_log["losses/cql_qf2_loss"] = cql_qf2_loss.item()
            data_log["losses/cql_qf1_diff"] = cql_qf1_diff.item()
            data_log["losses/cql_qf2_diff"] = cql_qf2_diff.item()
            data_log["losses/qf_loss"] = qf_loss.item()
            data_log["losses/cql_qf_loss"] = cql_qf_loss.item()
            data_log["losses/actor_loss"] = actor_loss.item()
            data_log["losses/alpha"] = alpha
            data_log["losses/cql_alpha"] = cql_alpha.item()
            data_log["misc/steps_per_second"] = int(
                global_step / (time.time() - start_time)
            )
            print("SPS:", int(global_step / (time.time() - start_time)), flush=True)
            if args.autotune:
                data_log["losses/alpha_loss"] = alpha_loss.item()
            if args.cql_autotune:
                data_log["losses/cql_alpha_loss"] = cql_alpha_loss.item()

        # Evaluate trained policy
        if (global_step + 1) % args.eval_freq == 0 or global_step == 0:
            eval_policy(
                actor,
                args.env_id,
                args.maximum_episode_length,
                args.seed,
                10000,
                global_step,
                args.capture_video,
                run_name,
                args.num_evals,
                data_log,
            )

        data_log["misc/global_step"] = global_step
        wandb.log(data_log, step=global_step)

        # Save checkpoints during training
        if args.save:
            if global_step % args.checkpoint_interval == 0:
                # Save models
                models = {
                    "actor_state_dict": actor.state_dict(),
                    "qf1_state_dict": qf1.state_dict(),
                    "qf2_state_dict": qf2.state_dict(),
                    "qf1_target_state_dict": qf1_target.state_dict(),
                    "qf2_target_state_dict": qf2_target.state_dict(),
                }
                # Save optimizers
                optimizers = {
                    "q_optimizer": q_optimizer.state_dict(),
                    "actor_optimizer": actor_optimizer.state_dict(),
                }
                if args.autotune:
                    optimizers["a_optimizer"] = a_optimizer.state_dict()
                    models["log_alpha"] = log_alpha
                if args.cql_autotune:
                    optimizers["cql_a_optimizer"] = cql_a_optimizer.state_dict()
                    models["cql_log_alpha"] = cql_log_alpha
                # Save replay buffer
                rb_data = rb.save_buffer()
                # Save random states, important for reproducibility
                rng_states = {
                    "random_rng_state": random.getstate(),
                    "numpy_rng_state": np.random.get_state(),
                    "torch_rng_state": torch.get_rng_state(),
                    "env_rng_state": env.np_random.bit_generator.state,
                    "env_action_space_rng_state": env.action_space.np_random.bit_generator.state,
                    "env_obs_space_rng_state": env.observation_space.np_random.bit_generator.state,
                }
                if device.type == "cuda":
                    rng_states["torch_cuda_rng_state"] = torch.cuda.get_rng_state()
                    rng_states[
                        "torch_cuda_rng_state_all"
                    ] = torch.cuda.get_rng_state_all()

                save(
                    run_id,
                    args.save_checkpoint_dir,
                    global_step,
                    models,
                    optimizers,
                    rb_data,
                    rng_states,
                )

    env.close()
