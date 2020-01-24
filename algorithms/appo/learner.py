import glob
import os
import random
import threading
import time
from collections import OrderedDict, deque
from os.path import join
from queue import Empty, Queue
from threading import Thread

import numpy as np
import torch
from torch.multiprocessing import Process, Queue as TorchQueue, Event as MultiprocessingEvent

from algorithms.appo.appo_utils import TaskType, list_of_dicts_to_dict_of_lists, iterate_recursively, \
    memory_stats, cuda_envvars
from algorithms.appo.model import ActorCritic
from algorithms.appo.population_based_training import PbtTask
from algorithms.utils.action_distributions import get_action_distribution
from algorithms.utils.algo_utils import calculate_gae
from algorithms.utils.multi_env import safe_get
from utils.decay import LinearDecay
from utils.timing import Timing
from utils.utils import log, AttrDict, experiment_dir, ensure_dir_exists


class LearnerWorker:
    def __init__(
        self, worker_idx, policy_id, cfg, obs_space, action_space, report_queue, policy_worker_queues,
    ):
        log.info('Initializing GPU learner %d for policy %d', worker_idx, policy_id)

        self.worker_idx = worker_idx
        self.policy_id = policy_id

        self.cfg = cfg

        # PBT-related stuff
        self.should_save_model = True  # set to true if we need to save the model to disk on the next training iteration
        self.load_policy_id = None  # non-None when we need to replace our parameters with another policy's parameters
        self.new_cfg = None  # non-None when we need to update the learning hyperparameters

        self.terminate = False

        self.obs_space = obs_space
        self.action_space = action_space

        self.device = None
        self.actor_critic = None
        self.optimizer = None

        self.task_queue = TorchQueue()
        self.report_queue = report_queue
        self.model_saved_event = MultiprocessingEvent()
        self.model_saved_event.clear()

        # queues corresponding to policy workers using the same policy
        # we send weight updates via these queues
        self.policy_worker_queues = policy_worker_queues

        self.rollout_tensors = dict()
        self.traj_buffer_ready = dict()

        self.experience_buffer_queue = Queue()

        self.with_training = True  # this only exists for debugging purposes
        self.train_in_background = True
        self.training_thread = Thread(target=self._train_loop) if self.train_in_background else None
        self.train_thread_initialized = threading.Event()
        self.processing_experience_batch = threading.Event()

        self.train_step = self.env_steps = 0

        self.summary_rate_decay = LinearDecay([(0, 50), (1000000, 1000), (10000000, 5000)])
        self.last_summary_written = -1e9
        self.save_rate_decay = LinearDecay([(0, self.cfg.initial_save_rate), (1000000, 5000)], staircase=100)
        self.last_saved = 0

        self.discarded_experience_over_time = deque([], maxlen=30)
        self.discarded_experience_timer = time.time()
        self.num_discarded_rollouts = 0

        self.kl_coeff = self.cfg.initial_kl_coeff

        self.process = Process(target=self._run, daemon=True)

    def start_process(self):
        self.process.start()

    def _init(self):
        log.info('Waiting for GPU learner to initialize...')
        self.train_thread_initialized.wait()
        log.info('GPU learner %d initialized', self.worker_idx)

    def _terminate(self):
        self.terminate = True

    def _broadcast_weights(self, discarding_rate):
        state_dict = self.actor_critic.state_dict()
        policy_version = self.train_step
        weight_update = (policy_version, state_dict, discarding_rate)
        for q in self.policy_worker_queues:
            q.put((TaskType.UPDATE_WEIGHTS, weight_update))

    def _calculate_gae(self, buffer):
        rewards = np.asarray(buffer.rewards)  # [E, T]
        dones = np.asarray(buffer.dones)  # [E, T]
        values_arr = np.array(buffer.values).squeeze()  # [E, T]

        # calculating fake values for the last step in the rollout
        # this will make sure that advantage of the very last action is always zero
        values = []
        for i in range(len(values_arr)):
            last_value, last_reward = values_arr[i][-1], rewards[i, -1]
            next_value = (last_value - last_reward) / self.cfg.gamma
            values.append(list(values_arr[i]))
            values[i].append(float(next_value))  # [T] -> [T+1]

        # calculating returns and GAE
        rewards = rewards.transpose((1, 0))  # [E, T] -> [T, E]
        dones = dones.transpose((1, 0))  # [E, T] -> [T, E]
        values = np.asarray(values).transpose((1, 0))  # [E, T+1] -> [T+1, E]

        advantages, returns = calculate_gae(rewards, dones, values, self.cfg.gamma, self.cfg.gae_lambda)

        # transpose tensors back to [E, T] before creating a single experience buffer
        buffer.advantages = advantages.transpose((1, 0))  # [T, E] -> [E, T]
        buffer.returns = returns.transpose((1, 0))  # [T, E] -> [E, T]
        buffer.returns = buffer.returns[:, :, np.newaxis]  # [E, T] -> [E, T, 1]

        return buffer

    def _mark_rollout_buffer_free(self, rollout):
        r = rollout
        traj_buffer_ready = self.traj_buffer_ready[(r['worker_idx'], r['split_idx'])]
        traj_buffer_ready[r['env_idx'], r['agent_idx'], r['traj_buffer_idx']] = 1

    def _prepare_train_buffer(self, rollouts, timing):
        trajectories = [AttrDict(r['t']) for r in rollouts]

        with timing.add_time('buffers'):
            buffer = AttrDict()

            # by the end of this loop the buffer is a dictionary containing lists of numpy arrays
            for i, t in enumerate(trajectories):
                for key, x in t.items():
                    if key not in buffer:
                        buffer[key] = []
                    buffer[key].append(x)

            # convert lists of dict observations to a single dictionary of lists
            for key, x in buffer.items():
                if isinstance(x[0], (dict, OrderedDict)):
                    buffer[key] = list_of_dicts_to_dict_of_lists(x)

        if not self.cfg.with_vtrace:
            with timing.add_time('calc_gae'):
                buffer = self._calculate_gae(buffer)

            # normalize advantages if needed
            if self.cfg.normalize_advantage:
                adv_mean = buffer.advantages.mean()
                adv_std = buffer.advantages.std()
                # adv_max, adv_min = buffer.advantages.max(), buffer.advantages.min()
                # adv_max_abs = max(adv_max, abs(adv_min))
                # log.info(
                #     'Adv mean %.3f std %.3f, min %.3f, max %.3f, max abs %.3f',
                #     adv_mean, adv_std, adv_min, adv_max, adv_max_abs,
                # )
                buffer.advantages = (buffer.advantages - adv_mean) / max(1e-2, adv_std)

        with timing.add_time('tensors'):
            for d, key, value in iterate_recursively(buffer):
                d[key] = torch.cat(value, dim=0)

            # will squeeze actions only in simple categorical case
            tensors_to_squeeze = ['actions', 'log_prob_actions', 'policy_version', 'values']
            for tensor_name in tensors_to_squeeze:
                buffer[tensor_name].squeeze_()

        with timing.add_time('buff_ready'):
            for r in rollouts:
                self._mark_rollout_buffer_free(r)

        return buffer

    def _process_macro_batch(self, rollouts, timing):
        assert self.cfg.macro_batch % self.cfg.rollout == 0
        assert self.cfg.rollout % self.cfg.recurrence == 0
        assert self.cfg.macro_batch % self.cfg.recurrence == 0

        samples = env_steps = 0
        for rollout in rollouts:
            samples += rollout['length']
            env_steps += rollout['env_steps']

        with timing.add_time('prepare'):
            buffer = self._prepare_train_buffer(rollouts, timing)
            self.experience_buffer_queue.put((buffer, samples, env_steps))

    def _process_rollouts(self, rollouts, timing):
        # log.info('Pending rollouts: %d (%d samples)', len(self.rollouts), len(self.rollouts) * self.cfg.rollout)
        rollouts_in_macro_batch = self.cfg.macro_batch // self.cfg.rollout
        work_done = False

        if len(rollouts) < rollouts_in_macro_batch:
            return rollouts, work_done

        discard_rollouts = 0
        policy_version = self.train_step
        for r in rollouts:
            rollout_min_version = r['t']['policy_version'].min().item()
            if policy_version - rollout_min_version >= self.cfg.max_policy_lag:
                discard_rollouts += 1
                self._mark_rollout_buffer_free(r)
            else:
                break

        if discard_rollouts > 0:
            log.warning(
                'Discarding %d old rollouts (learner %d is not fast enough to process experience)',
                self.policy_id, discard_rollouts,
            )
            rollouts = rollouts[discard_rollouts:]
            self.num_discarded_rollouts += discard_rollouts

        if len(rollouts) >= rollouts_in_macro_batch:
            # process newest rollouts
            rollouts_to_process = rollouts[:rollouts_in_macro_batch]
            rollouts = rollouts[rollouts_in_macro_batch:]

            self._process_macro_batch(rollouts_to_process, timing)
            # log.info('Unprocessed rollouts: %d (%d samples)', len(rollouts), len(rollouts) * self.cfg.rollout)

            work_done = True

        return rollouts, work_done

    def _get_minibatches(self, experience_size):
        """Generating minibatches for training."""
        assert self.cfg.rollout % self.cfg.recurrence == 0
        assert experience_size % self.cfg.batch_size == 0

        if self.cfg.macro_batch == self.cfg.batch_size:
            return [None]  # single minibatch is actually the entire buffer, we don't need indices

        # indices that will start the mini-trajectories from the same episode (for bptt)
        indices = np.arange(0, experience_size, self.cfg.recurrence)
        indices = np.random.permutation(indices)

        # complete indices of mini trajectories, e.g. with recurrence==4: [4, 16] -> [4, 5, 6, 7, 16, 17, 18, 19]
        indices = [np.arange(i, i + self.cfg.recurrence) for i in indices]
        indices = np.concatenate(indices)

        assert len(indices) == experience_size

        num_minibatches = experience_size // self.cfg.batch_size
        minibatches = np.split(indices, num_minibatches)
        return minibatches

    @staticmethod
    def _get_minibatch(buffer, indices):
        if indices is None:
            # handle the case of a single batch, where the entire buffer is a minibatch
            return buffer

        mb = AttrDict()

        for item, x in buffer.items():
            if isinstance(x, (dict, OrderedDict)):
                mb[item] = AttrDict()
                for key, x_elem in x.items():
                    mb[item][key] = x_elem[indices]
            else:
                mb[item] = x[indices]

        return mb

    def _should_save_summaries(self):
        summaries_every = self.summary_rate_decay.at(self.train_step)
        if self.train_step - self.last_summary_written < summaries_every:
            return False

        if random.random() < 0.1:
            # this is to make sure summaries are saved at random moments in time, to guarantee we have no bias
            return False

        return True

    def _after_optimizer_step(self):
        """A hook to be called after each optimizer step."""
        self.train_step += 1
        self._maybe_save()

    def _maybe_save(self):
        save_every = self.save_rate_decay.at(self.train_step)
        if self.train_step - self.last_saved >= save_every or self.should_save_model:
            self._save()
            self.model_saved_event.set()
            self.should_save_model = False
            self.last_saved = self.train_step

    @staticmethod
    def checkpoint_dir(cfg, policy_id):
        checkpoint_dir = join(experiment_dir(cfg=cfg), f'checkpoint_p{policy_id}')
        return ensure_dir_exists(checkpoint_dir)

    @staticmethod
    def get_checkpoints(checkpoints_dir):
        checkpoints = glob.glob(join(checkpoints_dir, 'checkpoint_*'))
        return sorted(checkpoints)

    def _get_checkpoint_dict(self):
        checkpoint = {
            'train_step': self.train_step,
            'env_steps': self.env_steps,
            'kl_coeff': self.kl_coeff,
            'model': self.actor_critic.state_dict(),
            'optimizer': self.optimizer.state_dict(),
        }
        return checkpoint

    def _save(self):
        checkpoint = self._get_checkpoint_dict()
        assert checkpoint is not None

        checkpoint_dir = self.checkpoint_dir(self.cfg, self.policy_id)
        tmp_filepath = join(checkpoint_dir, 'checkpoint_tmp')
        filepath = join(checkpoint_dir, f'checkpoint_{self.train_step:09d}_{self.env_steps}.pth')
        log.info('Saving %s...', tmp_filepath)
        torch.save(checkpoint, tmp_filepath)
        log.info('Renaming %s to %s', tmp_filepath, filepath)
        os.rename(tmp_filepath, filepath)

        while len(self.get_checkpoints(checkpoint_dir)) > self.cfg.keep_checkpoints:
            oldest_checkpoint = self.get_checkpoints(checkpoint_dir)[0]
            if os.path.isfile(oldest_checkpoint):
                log.debug('Removing %s', oldest_checkpoint)
                os.remove(oldest_checkpoint)

    @staticmethod
    def _policy_loss(ratio, adv, clip_ratio_low, clip_ratio_high):
        clipped_ratio = torch.clamp(ratio, clip_ratio_low, clip_ratio_high)
        loss_unclipped = ratio * adv
        loss_clipped = clipped_ratio * adv
        loss = torch.min(loss_unclipped, loss_clipped)
        loss = -loss.mean()

        return loss

    def _value_loss(self, new_values, old_values, target, clip_value):
        value_clipped = old_values + torch.clamp(new_values - old_values, -clip_value, clip_value)
        value_original_loss = (new_values - target).pow(2)
        value_clipped_loss = (value_clipped - target).pow(2)
        value_loss = torch.max(value_original_loss, value_clipped_loss)
        value_loss = value_loss.mean()
        value_loss *= self.cfg.value_loss_coeff

        return value_loss

    def _copy_train_data_to_gpu(self, buffer):
        for d, k, v in iterate_recursively(buffer):
            d[k] = v.to(self.device).float()
        return buffer

    def _train(self, cpu_buffer, experience_size, timing):
        with torch.no_grad():
            with timing.add_time('tensors_gpu_float'):
                buffer = self._copy_train_data_to_gpu(cpu_buffer)

            rho_hat = c_hat = 1.0  # V-trace parameters
            # noinspection PyArgumentList
            rho_hat = torch.Tensor([rho_hat])
            # noinspection PyArgumentList
            c_hat = torch.Tensor([c_hat])

        clip_ratio_high = self.cfg.ppo_clip_ratio
        # this still works with e.g. clip_ratio = 2, while PPO's 1-r would give negative ratio
        clip_ratio_low = 1.0 / clip_ratio_high

        clip_value = self.cfg.ppo_clip_value
        gamma = self.cfg.gamma

        kl_old_mean = 0.0
        rnn_dist = 0.0

        num_sgd_steps = 0

        stats = None

        for epoch in range(self.cfg.ppo_epochs):
            minibatches = self._get_minibatches(experience_size)

            for batch_num in range(len(minibatches)):
                indices = minibatches[batch_num]

                # current minibatch consisting of short trajectory segments with length == recurrence
                mb = self._get_minibatch(buffer, indices)

                # calculate policy head outside of recurrent loop
                head_outputs = self.actor_critic.forward_head(mb.obs)

                # initial rnn states
                timestep = np.arange(0, self.cfg.batch_size, self.cfg.recurrence)
                rnn_states = mb.rnn_states[timestep]

                # calculate RNN outputs for each timestep in a loop
                with timing.add_time('bptt'):
                    core_outputs = []
                    for i in range(self.cfg.recurrence):
                        # indices of head outputs corresponding to the current timestep
                        timestep = np.arange(i, self.cfg.batch_size, self.cfg.recurrence)
                        step_head_outputs = head_outputs[timestep]

                        core_output, rnn_states = self.actor_critic.forward_core(step_head_outputs, rnn_states)
                        core_outputs.append(core_output)

                        # zero-out RNN states on the episode boundary
                        dones = mb.dones[timestep].unsqueeze(dim=1)
                        rnn_states = (1.0 - dones) * rnn_states

                # transform core outputs from [T, Batch, D] to [Batch, T, D] and then to [Batch x T, D]
                # which is the same shape as the minibatch
                core_outputs = torch.stack(core_outputs)
                num_timesteps, num_trajectories = core_outputs.shape[:2]
                assert num_timesteps == self.cfg.recurrence
                assert num_timesteps * num_trajectories == self.cfg.batch_size
                core_outputs = core_outputs.transpose(0, 1).reshape(-1, *core_outputs.shape[2:])
                assert core_outputs.shape[0] == head_outputs.shape[0]

                # calculate policy tail outside of recurrent loop
                result = self.actor_critic.forward_tail(core_outputs, with_action_distribution=True)

                action_distribution = result.action_distribution
                log_prob_actions = action_distribution.log_prob(mb.actions)
                ratio = torch.exp(log_prob_actions - mb.log_prob_actions)  # pi / pi_old

                values = result.values.squeeze()

                with torch.no_grad():  # these computations are not the part of the computation graph
                    ratios_cpu = ratio.cpu()
                    values_cpu = values.cpu()
                    rewards_cpu = mb.rewards.cpu()  # we only need this on CPU, potential minor optimization
                    dones_cpu = mb.dones.cpu()

                    vtrace_rho = torch.min(rho_hat, ratios_cpu)
                    vtrace_c = torch.min(c_hat, ratios_cpu)

                    vs = torch.zeros((num_trajectories * self.cfg.recurrence))
                    adv = torch.zeros((num_trajectories * self.cfg.recurrence))

                    last_timestep = np.arange(self.cfg.recurrence - 1, self.cfg.batch_size, self.cfg.recurrence)
                    next_values = (values_cpu[last_timestep] - rewards_cpu[last_timestep]) / self.cfg.gamma
                    next_vs = next_values

                    with timing.add_time('vtrace'):
                        for i in reversed(range(self.cfg.recurrence)):
                            timestep = np.arange(i, self.cfg.batch_size, self.cfg.recurrence)

                            rewards = rewards_cpu[timestep]
                            dones = dones_cpu[timestep]
                            not_done = 1.0 - dones
                            not_done_times_gamma = not_done * gamma

                            curr_values = values_cpu[timestep]
                            curr_vtrace_rho = vtrace_rho[timestep]
                            curr_vtrace_c = vtrace_c[timestep]

                            delta_s = curr_vtrace_rho * (rewards + not_done_times_gamma * next_values - curr_values)
                            adv[timestep] = curr_vtrace_rho * (rewards + not_done_times_gamma * next_vs - curr_values)
                            next_vs = curr_values + delta_s + not_done_times_gamma * curr_vtrace_c * (next_vs - next_values)
                            vs[timestep] = next_vs

                            next_values = curr_values

                    adv_mean = adv.mean()
                    adv_std = adv.std()
                    adv = (adv - adv_mean) / max(1e-2, adv_std)  # normalize advantage
                    adv = adv.to(self.device)

                with timing.add_time('losses'):
                    policy_loss = self._policy_loss(ratio, adv, clip_ratio_low, clip_ratio_high)

                    targets = vs.to(self.device)
                    old_values = mb.values
                    value_loss = self._value_loss(values, old_values, targets, clip_value)

                    # entropy loss
                    kl_prior = action_distribution.kl_prior()
                    kl_prior = kl_prior.mean()
                    prior_loss = self.cfg.prior_loss_coeff * kl_prior

                    old_action_distribution = get_action_distribution(self.actor_critic.action_space, mb.action_logits)

                    # small KL penalty for being different from the behavior policy
                    kl_old = action_distribution.kl_divergence(old_action_distribution)
                    kl_old_mean = kl_old.mean()
                    kl_penalty = self.kl_coeff * kl_old_mean

                    loss = policy_loss + value_loss + prior_loss + kl_penalty

                with timing.add_time('update'):
                    # update the weights
                    self.optimizer.zero_grad()
                    loss.backward()

                    if self.cfg.max_grad_norm > 0.0:
                        with timing.add_time('clip'):
                            torch.nn.utils.clip_grad_norm_(self.actor_critic.parameters(), self.cfg.max_grad_norm)

                    self.optimizer.step()
                    num_sgd_steps += 1

                with torch.no_grad():
                    self._after_optimizer_step()

                    # collect and report summaries
                    with_summaries = self._should_save_summaries()
                    if with_summaries:
                        self.last_summary_written = self.train_step
                        stats = AttrDict()

                        grad_norm = sum(
                            p.grad.data.norm(2).item() ** 2
                            for p in self.actor_critic.parameters()
                            if p.grad is not None
                        ) ** 0.5
                        stats.grad_norm = grad_norm
                        stats.loss = loss
                        stats.value = result.values.mean()
                        stats.entropy = action_distribution.entropy().mean()
                        stats.kl_prior = kl_prior
                        stats.policy_loss = policy_loss
                        stats.value_loss = value_loss
                        stats.prior_loss = prior_loss
                        stats.kl_coeff = self.kl_coeff
                        stats.kl_penalty = kl_penalty
                        stats.adv_min = adv.min()
                        stats.adv_max = adv.max()
                        stats.max_abs_logprob = torch.abs(mb.action_logits).max()

                        if epoch == self.cfg.ppo_epochs - 1 and batch_num == len(minibatches) - 1:
                            # we collect these stats only for the last PPO batch, or every time if we're only doing
                            # one batch, IMPALA-style
                            ratio_mean = torch.abs(1.0 - ratio).mean().detach()
                            ratio_min = ratio.min().detach()
                            ratio_max = ratio.max().detach()
                            # log.debug('Learner %d ratio mean min max %.4f %.4f %.4f', self.policy_id, ratio_mean.cpu().item(), ratio_min.cpu().item(), ratio_max.cpu().item())

                            value_delta = torch.abs(values - old_values)
                            value_delta_avg, value_delta_max = value_delta.mean(), value_delta.max()

                            stats.kl_divergence = kl_old_mean
                            stats.kl_max = kl_old.max()
                            stats.value_delta = value_delta_avg
                            stats.value_delta_max = value_delta_max
                            stats.fraction_clipped = ((ratio < clip_ratio_low).float() + (ratio > clip_ratio_high).float()).mean()
                            stats.rnn_dist = rnn_dist
                            stats.ratio_mean = ratio_mean
                            stats.ratio_min = ratio_min
                            stats.ratio_max = ratio_max
                            stats.num_sgd_steps = num_sgd_steps

                        # this caused numerical issues on some versions of PyTorch with second moment getting to infinity
                        adam_max_second_moment = 0.0
                        for key, tensor_state in self.optimizer.state.items():
                            adam_max_second_moment = max(tensor_state['exp_avg_sq'].max().item(), adam_max_second_moment)
                        stats.adam_max_second_moment = adam_max_second_moment

                        curr_policy_version = self.train_step
                        version_diff = curr_policy_version - mb.policy_version
                        stats.version_diff_avg = version_diff.mean()
                        stats.version_diff_min = version_diff.min()
                        stats.version_diff_max = version_diff.max()

                        # we want this statistic for the last batch of the last epoch
                        for key, value in stats.items():
                            if isinstance(value, torch.Tensor):
                                stats[key] = value.detach().cpu()

        with torch.no_grad():
            # adjust KL-penalty coefficient if KL divergence at the end of training is high
            if kl_old_mean > self.cfg.target_kl:
                self.kl_coeff *= 1.5
            elif kl_old_mean < self.cfg.target_kl / 2:
                self.kl_coeff /= 1.5
            self.kl_coeff = max(self.kl_coeff, 1e-6)

        del buffer
        return stats

    def _update_pbt(self):
        """To be called from the training loop, same thread that updates the model!"""
        if self.load_policy_id is not None:
            assert self.cfg.with_pbt

            log.debug('Learner %d loads policy from %d', self.policy_id, self.load_policy_id)
            self.load_from_checkpoint(self.load_policy_id)
            self.load_policy_id = None

        if self.new_cfg is not None:
            for key, value in self.new_cfg.items():
                if self.cfg[key] != value:
                    log.debug('Learner %d replacing cfg parameter %r with new value %r', self.policy_id, key, value)
                    self.cfg[key] = value

            for param_group in self.optimizer.param_groups:
                param_group['lr'] = self.cfg.learning_rate
                param_group['betas'] = (self.cfg.adam_beta1, self.cfg.adam_beta2)
                log.debug('Updated optimizer lr to value %.7f, betas: %r', param_group['lr'], param_group['betas'])

            self.new_cfg = None

    def load_checkpoint(self, checkpoints):
        if len(checkpoints) <= 0:
            log.warning('No checkpoints found')
            return None
        else:
            latest_checkpoint = checkpoints[-1]
            log.warning('Loading state from checkpoint %s...', latest_checkpoint)
            checkpoint_dict = torch.load(latest_checkpoint, map_location=self.device)
            return checkpoint_dict

    def _load_state(self, checkpoint_dict, load_progress=True):
        if load_progress:
            self.train_step = checkpoint_dict['train_step']
            self.env_steps = checkpoint_dict['env_steps']
        self.kl_coeff = checkpoint_dict['kl_coeff']
        self.actor_critic.load_state_dict(checkpoint_dict['model'])
        self.optimizer.load_state_dict(checkpoint_dict['optimizer'])
        log.info('Loaded experiment state at training iteration %d, env step %d', self.train_step, self.env_steps)

    def init_model(self):
        self.actor_critic = ActorCritic(self.obs_space, self.action_space, self.cfg)
        self.actor_critic.to(self.device)
        self.actor_critic.share_memory()

    def load_from_checkpoint(self, policy_id):
        checkpoints = self.get_checkpoints(self.checkpoint_dir(self.cfg, policy_id))
        checkpoint_dict = self.load_checkpoint(checkpoints)
        if checkpoint_dict is None:
            log.debug('Did not load from checkpoint, starting from scratch!')
        else:
            log.debug('Loading model from checkpoint')

            # if we're replacing our policy with another policy (under PBT), let's not reload the env_steps
            load_progress = policy_id == self.policy_id
            self._load_state(checkpoint_dict, load_progress=load_progress)

    def initialize(self, timing):
        with timing.timeit('init'):
            # initialize the Torch modules
            if self.cfg.seed is not None:
                log.info('Setting fixed seed %d', self.cfg.seed)
                torch.manual_seed(self.cfg.seed)
                np.random.seed(self.cfg.seed)

            torch.backends.cudnn.benchmark = True

            # we should already see only one CUDA device, because of env vars
            assert torch.cuda.device_count() == 1
            self.device = torch.device('cuda', index=0)
            self.init_model()

            self.optimizer = torch.optim.Adam(
                self.actor_critic.parameters(),
                self.cfg.learning_rate,
                betas=(self.cfg.adam_beta1, self.cfg.adam_beta2),
                eps=self.cfg.adam_eps,
            )

            self.load_from_checkpoint(self.policy_id)

            self._broadcast_weights(self._discarding_rate())  # sync the very first version of the weights

        self.train_thread_initialized.set()

    def _process_training_data(self, data, timing, wait_stats=None):
        buffer, samples, env_steps = data
        self.env_steps += env_steps
        experience_size = buffer.rewards.shape[0]

        stats = dict(env_steps=self.env_steps, policy_id=self.policy_id)

        with timing.add_time('train'):
            discarding_rate = self._discarding_rate()

            if self.with_training:
                self._update_pbt()

                # log.debug('Training policy %d on %d samples', self.policy_id, samples)
                train_stats = self._train(buffer, experience_size, timing)

                if train_stats is not None:
                    stats['train'] = train_stats

                    if wait_stats is not None:
                        wait_avg, wait_min, wait_max = wait_stats
                        stats['train']['wait_avg'] = wait_avg
                        stats['train']['wait_min'] = wait_min
                        stats['train']['wait_max'] = wait_max

                    stats['train']['discarded_rollouts'] = self.num_discarded_rollouts
                    stats['train']['discarding_rate'] = discarding_rate

                    stats['stats'] = memory_stats('learner', self.device)

                self._broadcast_weights(discarding_rate)

        self.report_queue.put(stats)

    def _train_loop(self):
        timing = Timing()
        self.initialize(timing)

        wait_times = deque([], maxlen=self.cfg.num_workers)
        last_cache_cleanup = time.time()
        num_batches_processed = 0

        while not self.terminate:
            with timing.timeit('train_wait'):
                data = safe_get(self.experience_buffer_queue)

            self.processing_experience_batch.set()

            if self.terminate:
                break

            wait_stats = None
            wait_times.append(timing.train_wait)

            if len(wait_times) >= wait_times.maxlen:
                wait_times_arr = np.asarray(wait_times)
                wait_avg = np.mean(wait_times_arr)
                wait_min, wait_max = wait_times_arr.min(), wait_times_arr.max()
                # log.debug(
                #     'Training thread had to wait %.5f s for the new experience buffer (avg %.5f)',
                #     timing.train_wait, wait_avg,
                # )
                wait_stats = (wait_avg, wait_min, wait_max)

            self._process_training_data(data, timing, wait_stats)
            num_batches_processed += 1

            if time.time() - last_cache_cleanup > 30.0 or (not self.cfg.benchmark and num_batches_processed < 50):
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
                last_cache_cleanup = time.time()

        log.info('Train loop timing: %s', timing)
        del self.actor_critic
        del self.device

    def _experience_collection_rate_stats(self):
        now = time.time()
        if now - self.discarded_experience_timer > 1.0:
            self.discarded_experience_timer = now
            self.discarded_experience_over_time.append((now, self.num_discarded_rollouts))

    def _discarding_rate(self):
        if len(self.discarded_experience_over_time) <= 0:
            return 0

        first, last = self.discarded_experience_over_time[0], self.discarded_experience_over_time[-1]
        delta_rollouts = last[1] - first[1]
        delta_time = last[0] - first[0]
        discarding_rate = delta_rollouts / delta_time
        return discarding_rate

    def _init_rollout_tensors(self, data):
        data = AttrDict(data)
        assert self.policy_id == data.policy_id

        worker_idx, split_idx, traj_buffer_idx = data.worker_idx, data.split_idx, data.traj_buffer_idx
        for env_agent, rollout_data in data.tensors.items():
            env_idx, agent_idx = env_agent
            tensor_dict_key = (worker_idx, split_idx, env_idx, agent_idx, traj_buffer_idx)
            assert tensor_dict_key not in self.rollout_tensors

            self.rollout_tensors[tensor_dict_key] = rollout_data

        self.traj_buffer_ready[(worker_idx, split_idx)] = data.is_ready_tensor

    def _extract_rollouts(self, data):
        data = AttrDict(data)
        worker_idx, split_idx, traj_buffer_idx = data.worker_idx, data.split_idx, data.traj_buffer_idx

        rollouts = []
        for rollout_data in data.rollouts:
            env_idx, agent_idx = rollout_data['env_idx'], rollout_data['agent_idx']
            tensor_dict_key = (worker_idx, split_idx, env_idx, agent_idx, traj_buffer_idx)
            tensors = self.rollout_tensors[tensor_dict_key]

            rollout_data['t'] = tensors
            rollout_data['worker_idx'] = worker_idx
            rollout_data['split_idx'] = split_idx
            rollout_data['traj_buffer_idx'] = traj_buffer_idx
            rollouts.append(rollout_data)

        if not self.with_training:
            return []

        return rollouts

    def _process_pbt_task(self, pbt_task):
        task_type, data = pbt_task

        if task_type == PbtTask.SAVE_MODEL:
            policy_id = data
            assert policy_id == self.policy_id
            self.should_save_model = True
        elif task_type == PbtTask.LOAD_MODEL:
            policy_id, new_policy_id = data
            assert policy_id == self.policy_id
            assert new_policy_id is not None
            self.load_policy_id = new_policy_id
        elif task_type == PbtTask.UPDATE_CFG:
            policy_id, new_cfg = data
            assert policy_id == self.policy_id
            self.new_cfg = new_cfg

    def _run(self):
        cuda_envvars(self.policy_id)
        torch.multiprocessing.set_sharing_strategy('file_system')

        timing = Timing()

        rollouts = []

        if self.train_in_background:
            self.training_thread.start()
        else:
            self.initialize(timing)
            log.error(
                'train_in_background set to False on learner %d! This is slow, use only for testing!', self.policy_id,
            )

        while not self.terminate:
            while True:
                try:
                    task_type, data = self.task_queue.get_nowait()

                    if task_type == TaskType.INIT:
                        self._init()
                    elif task_type == TaskType.TERMINATE:
                        log.info('GPU learner timing: %s', timing)
                        self._terminate()
                        break
                    elif task_type == TaskType.INIT_TENSORS:
                        self._init_rollout_tensors(data)
                    elif task_type == TaskType.TRAIN:
                        with timing.add_time('extract'):
                            rollouts.extend(self._extract_rollouts(data))
                            # log.debug('Learner %d has %d rollouts', self.policy_id, len(rollouts))
                    elif task_type == TaskType.PBT:
                        self._process_pbt_task(data)
                except Empty:
                    break

            while self.experience_buffer_queue.qsize() > 1:
                self.processing_experience_batch.clear()
                self.processing_experience_batch.wait()

            rollouts, work_done = self._process_rollouts(rollouts, timing)

            if not self.train_in_background:
                while not self.experience_buffer_queue.empty():
                    training_data = self.experience_buffer_queue.get()
                    self.processing_experience_batch.set()
                    self._process_training_data(training_data, timing)

            self._experience_collection_rate_stats()

            if not work_done:
                # if we didn't do anything let's sleep to prevent wasting CPU time
                time.sleep(0.005)

        if self.train_in_background:
            self.experience_buffer_queue.put(None)
            self.training_thread.join()

    def init(self):
        self.task_queue.put((TaskType.INIT, None))
        self.task_queue.put((TaskType.EMPTY, None))

        # wait until we finished initializing
        while self.task_queue.qsize() > 0:
            time.sleep(0.01)

    def close(self):
        self.task_queue.put((TaskType.TERMINATE, None))

    def join(self):
        self.process.join(timeout=5)


# WITHOUT TRAINING:
# [2019-11-27 22:06:02,056] Gpu learner timing: init: 3.1058, work: 0.0001
# [2019-11-27 22:06:02,059] Gpu worker timing: init: 2.7746, deserialize: 4.6964, to_device: 3.8011, forward: 14.2683, serialize: 8.4691, postprocess: 9.8058, policy_step: 32.8482, weight_update: 0.0005, gpu_waiting: 2.0623
# [2019-11-27 22:06:02,065] Gpu worker timing: init: 5.4169, deserialize: 3.6640, to_device: 3.1592, forward: 13.2836, serialize: 6.3964, postprocess: 7.6095, policy_step: 27.9706, weight_update: 0.0005, gpu_waiting: 1.8249
# [2019-11-27 22:06:02,067] Env runner 0: timing waiting: 0.8708, reset: 27.0515, save_policy_outputs: 0.0006, env_step: 26.0700, finalize: 0.3460, overhead: 1.1313, format_output: 0.3095, one_step: 0.0272, work: 36.5773
# [2019-11-27 22:06:02,079] Env runner 1: timing waiting: 0.8468, reset: 26.8022, save_policy_outputs: 0.0008, env_step: 26.1251, finalize: 0.3565, overhead: 1.1361, format_output: 0.3224, one_step: 0.0269, work: 36.6139

# WITH TRAINING 1 epoch:
# [2019-11-27 22:24:20,590] Gpu worker timing: init: 2.9078, deserialize: 5.5495, to_device: 5.6693, forward: 15.7285, serialize: 10.0113, postprocess: 13.4533, policy_step: 40.7373, weight_update: 0.0007, gpu_waiting: 2.0482
# [2019-11-27 22:24:20,596] Gpu worker timing: init: 4.8333, deserialize: 4.6056, to_device: 5.0975, forward: 14.8585, serialize: 8.0576, postprocess: 11.3531, policy_step: 36.2226, weight_update: 0.0006, gpu_waiting: 1.9836
# [2019-11-27 22:24:20,606] Env runner 1: timing waiting: 0.9328, reset: 27.9299, save_policy_outputs: 0.0005, env_step: 31.6222, finalize: 0.4432, overhead: 1.3904, format_output: 0.3692, one_step: 0.0309, work: 44.7151
# [2019-11-27 22:24:20,622] Env runner 0: timing waiting: 1.0276, reset: 27.5389, save_policy_outputs: 0.0009, env_step: 31.5377, finalize: 0.4614, overhead: 1.4103, format_output: 0.3564, one_step: 0.0269, work: 44.6398
# [2019-11-27 22:24:23,072] Gpu learner timing: init: 3.3635, last_values: 0.4506, gae: 3.5159, numpy: 0.6232, finalize: 4.6129, buffer: 6.4776, update: 16.3922, train: 26.0528, work: 37.2159
# [2019-11-27 22:24:52,618] Collected 1012576, FPS: 22177.3

# Env runner 0: timing waiting: 2.5731, reset: 5.0527, save_policy_outputs: 0.0007, env_step: 28.7689, overhead: 1.1565, format_inputs: 0.3170, one_step: 0.0276, work: 39.3452
# [2019-12-06 19:01:42,042] Env runner 1: timing waiting: 2.5900, reset: 4.9147, save_policy_outputs: 0.0004, env_step: 28.8585, overhead: 1.1266, format_inputs: 0.3087, one_step: 0.0254, work: 39.3333
# [2019-12-06 19:01:42,227] Gpu worker timing: init: 2.8738, weight_update: 0.0006, deserialize: 7.6602, to_device: 5.3244, forward: 8.1527, serialize: 14.3651, postprocess: 17.5523, policy_step: 38.8745, gpu_waiting: 0.5276
# [2019-12-06 19:01:42,232] Gpu learner timing: init: 3.3448, last_values: 0.2737, gae: 3.0682, numpy: 0.5308, finalize: 3.8888, buffer: 5.2451, forw_head: 0.2639, forw_core: 0.8289, forw_tail: 0.5334, clip: 4.5709, update: 12.0888, train: 19.6720, work: 28.8663
# [2019-12-06 19:01:42,723] Collected 1007616, FPS: 23975.2

# Last version using Plasma:
# [2020-01-07 00:24:27,690] Env runner 0: timing wait_actor: 0.0001, waiting: 2.2242, reset: 13.0768, save_policy_outputs: 0.0004, env_step: 27.5735, overhead: 1.0524, format_inputs: 0.2934, enqueue_policy_requests: 4.6075, complete_rollouts: 3.2226, one_step: 0.0250, work: 37.9023
# [2020-01-07 00:24:27,697] Env runner 1: timing wait_actor: 0.0042, waiting: 2.2486, reset: 13.3085, save_policy_outputs: 0.0005, env_step: 27.5389, overhead: 1.0206, format_inputs: 0.2921, enqueue_policy_requests: 4.5829, complete_rollouts: 3.3319, one_step: 0.0240, work: 37.8813
# [2020-01-07 00:24:27,890] Gpu worker timing: init: 3.0419, wait_policy: 0.0002, gpu_waiting: 0.4060, weight_update: 0.0007, deserialize: 0.0923, to_device: 4.7866, forward: 6.8820, serialize: 13.8782, postprocess: 16.9365, policy_step: 28.8341, one_step: 0.0000, work: 39.9577
# [2020-01-07 00:24:27,906] GPU learner timing: buffers: 0.0461, tensors: 8.7751, prepare: 8.8510
# [2020-01-07 00:24:27,907] Train loop timing: init: 3.0417, train_wait: 0.0969, bptt: 2.6350, vtrace: 5.7421, losses: 0.7799, clip: 4.6204, update: 9.1475, train: 21.3880
# [2020-01-07 00:24:28,213] Collected {0: 1015808}, FPS: 25279.4
# [2020-01-07 00:24:28,214] Timing: experience: 40.1832

# Version using Pytorch tensors with shared memory:
# [2020-01-07 01:08:05,569] Env runner 0: timing wait_actor: 0.0003, waiting: 0.6292, reset: 12.4041, save_policy_outputs: 0.4311, env_step: 30.1347, overhead: 4.3134, enqueue_policy_requests: 0.0677, complete_rollouts: 0.0274, one_step: 0.0261, work: 35.3962, wait_buffers: 0.0164
# [2020-01-07 01:08:05,596] Env runner 1: timing wait_actor: 0.0003, waiting: 0.7102, reset: 12.7194, save_policy_outputs: 0.4400, env_step: 30.1091, overhead: 4.2822, enqueue_policy_requests: 0.0630, complete_rollouts: 0.0234, one_step: 0.0270, work: 35.3405, wait_buffers: 0.0162
# [2020-01-07 01:08:05,762] Gpu worker timing: init: 2.8383, wait_policy: 0.0000, gpu_waiting: 2.3759, loop: 4.3098, weight_update: 0.0006, updates: 0.0008, deserialize: 0.8207, to_device: 6.8636, forward: 15.0019, postprocess: 2.4855, handle_policy_step: 29.5612, one_step: 0.0000, work: 33.9772
# [2020-01-07 01:08:05,896] Train loop timing: init: 2.9927, train_wait: 0.0001, bptt: 2.6755, vtrace: 6.3307, losses: 0.7319, update: 4.6164, train: 22.0022
# [2020-01-07 01:08:10,888] Collected {0: 1015808}, FPS: 28900.6
# [2020-01-07 01:08:10,888] Timing: experience: 35.1483

# Version V53, Torch 1.3.1
# [2020-01-09 20:33:23,540] Env runner 0: timing wait_actor: 0.0002, waiting: 0.7097, reset: 5.2281, save_policy_outputs: 0.3789, env_step: 29.3372, overhead: 4.2642, enqueue_policy_requests: 0.0660, complete_rollouts: 0.0313, one_step: 0.0244, work: 34.5037, wait_buffers: 0.0213
# [2020-01-09 20:33:23,556] Env runner 1: timing wait_actor: 0.0009, waiting: 0.6965, reset: 5.3100, save_policy_outputs: 0.3989, env_step: 29.3533, overhead: 4.2378, enqueue_policy_requests: 0.0685, complete_rollouts: 0.0290, one_step: 0.0261, work: 34.5326, wait_buffers: 0.0165
# [2020-01-09 20:33:23,711] Gpu worker timing: init: 1.3378, wait_policy: 0.0016, gpu_waiting: 2.3035, loop: 4.5320, weight_update: 0.0006, updates: 0.0008, deserialize: 0.8223, to_device: 6.4952, forward: 14.8064, postprocess: 2.4568, handle_policy_step: 28.7065, one_step: 0.0000, work: 33.3578
# [2020-01-09 20:33:23,816] GPU learner timing: extract: 0.0137, buffers: 0.0437, tensors: 6.6962, buff_ready: 0.1400, prepare: 6.9068
# [2020-01-09 20:33:23,892] Train loop timing: init: 1.3945, train_wait: 0.0000, bptt: 2.2262, vtrace: 5.5308, losses: 0.6580, update: 3.6261, train: 19.8292
# [2020-01-09 20:33:28,787] Collected {0: 1015808}, FPS: 29476.0
# [2020-01-09 20:33:28,787] Timing: experience: 34.4622

# Version V60
# [2020-01-19 03:25:14,014] Env runner 0: timing wait_actor: 0.0001, waiting: 9.7151, reset: 41.1152, save_policy_outputs: 0.5734, env_step: 39.1791, overhead: 6.5181, enqueue_policy_requests: 0.1089, complete_rollouts: 0.2901, one_step: 0.0163, work: 47.2741, wait_buffers: 0.2795
# [2020-01-19 03:25:14,015] Env runner 1: timing wait_actor: 0.0001, waiting: 10.1184, reset: 41.6788, save_policy_outputs: 0.5846, env_step: 39.1234, overhead: 6.4405, enqueue_policy_requests: 0.1021, complete_rollouts: 0.0304, one_step: 0.0154, work: 46.8807, wait_buffers: 0.0202
# [2020-01-19 03:25:14,178] Updated weights on worker 0, policy_version 251 (0.00032)
# [2020-01-19 03:25:14,201] Gpu worker timing: init: 1.3160, wait_policy: 0.0009, gpu_waiting: 9.5548, loop: 9.7118, weight_update: 0.0003, updates: 0.0005, deserialize: 1.5404, to_device: 12.7886, forward: 12.9712, postprocess: 4.9893, handle_policy_step: 37.9686, one_step: 0.0000, work: 47.9418
# [2020-01-19 03:25:14,221] GPU learner timing: extract: 0.0392, buffers: 0.0745, tensors: 11.0697, buff_ready: 0.4808, prepare: 11.7095
# [2020-01-19 03:25:14,321] Train loop timing: init: 1.4332, train_wait: 0.0451, tensors_gpu_float: 4.3031, bptt: 5.0880, vtrace: 2.4773, losses: 1.9113, update: 7.6270, train: 36.8291
# [2020-01-19 03:25:14,465] Collected {0: 2015232}, FPS: 35779.2
# [2020-01-19 03:25:14,465] Timing: experience: 56.3241

# Version V61, cudnn benchmark=True
# [2020-01-19 18:19:31,416] Env runner 0: timing wait_actor: 0.0002, waiting: 8.8857, reset: 41.9806, save_policy_outputs: 0.5918, env_step: 38.3737, overhead: 6.3290, enqueue_policy_requests: 0.1026, complete_rollouts: 0.0286, one_step: 0.0141, work: 46.0301, wait_buffers: 0.0181
# [2020-01-19 18:19:31,420] Env runner 1: timing wait_actor: 0.0002, waiting: 9.0225, reset: 42.5019, save_policy_outputs: 0.5540, env_step: 38.1044, overhead: 6.2374, enqueue_policy_requests: 0.1140, complete_rollouts: 0.2770, one_step: 0.0169, work: 45.8830, wait_buffers: 0.2664
# [2020-01-19 18:19:31,472] Updated weights on worker 0, policy_version 245 (0.00051)
# [2020-01-19 18:19:31,587] Updated weights on worker 0, policy_version 246 (0.00053)
# [2020-01-19 18:19:31,610] Gpu worker timing: init: 1.3633, wait_policy: 0.0037, gpu_waiting: 9.4391, loop: 9.6261, weight_update: 0.0005, updates: 0.0007, deserialize: 1.4722, to_device: 12.5683, forward: 12.8369, postprocess: 4.9932, handle_policy_step: 36.1579, one_step: 0.0000, work: 45.9985
# [2020-01-19 18:19:31,624] GPU learner timing: extract: 0.0376, buffers: 0.0769, tensors: 11.2689, buff_ready: 0.4423, prepare: 11.8845
# [2020-01-19 18:19:31,630] Train loop timing: init: 1.4804, train_wait: 0.0481, tensors_gpu_float: 4.1565, bptt: 5.2692, vtrace: 2.2177, losses: 1.7225, update: 7.5387, train: 31.5856
# [2020-01-19 18:19:31,797] Collected {0: 1966080}, FPS: 36238.5
# [2020-01-19 18:19:31,797] Timing: experience: 54.2540
