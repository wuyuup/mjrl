
from mjrl.algos.npg_cg import NPG
from mjrl.utils.replay_buffer import TrajectoryReplayBuffer
from mjrl.utils.networks import QNetwork
from mjrl.utils.cg_solve import cg_solve
from mjrl.utils.evaluate_q_function import evaluate_n_step, evaluate_start_end, mse

import mjrl.samplers.core as trajectory_sampler
import mjrl.utils.process_samples as process_samples
import numpy as np
import time

class NPGOffPolicy(NPG):

    def __init__(self, env, policy, q_function, normalized_step_size,
                    num_policy_updates,
                    num_update_states,
                    num_update_actions,
                    fit_on_policy,
                    fit_off_policy,
                    summary_writer=None,
                    ):
        """
        params:
            env
            policy
            q_function
            normalized_step_size
            num_policy_updates
            num_update_states
            num_update_actions
            fit_on_policy
            fit_off_policy
            simple_value_func - whether to estimate V(s) simply or correctly:
                If true, we estimate V(s) = Q(s,a), a = argmax a' pi(a'|s)
                If false, we estimate V(s) = 1/num_value_states sum_i Q(s,a_i), a_i ~ pi(.|s)
            num_value_states
        """
        super().__init__(env, policy, q_function, normalized_step_size = normalized_step_size)
        self.num_policy_updates = num_policy_updates
        self.num_update_states = num_update_states
        self.num_update_actions = num_update_actions
        self.fit_on_policy = fit_on_policy
        self.fit_off_policy = fit_off_policy
        self.summary_writer = summary_writer

    def train_step(self, N,
                    env=None,
                    sample_mode='trajectories',
                    horizon=1e6,
                    gamma=0.995,
                    gae_lambda=0.97,
                    num_cpu='max',
                    env_kwargs=None,
                    iteration=None):

        if iteration is None:
            raise Exception('Must set include_iteration=True in train_step()')

        # get new samples
        env = self.env.env_id if env is None else env
        if sample_mode == 'trajectories':
            input_dict = dict(num_traj=N, env=env, policy=self.policy, horizon=horizon,
                                base_seed=self.seed, num_cpu=num_cpu)
            paths = trajectory_sampler.sample_paths(**input_dict)
        elif sample_mode == 'samples':
            input_dict = dict(num_samples=N, env=env, policy=self.policy, horizon=horizon,
                                base_seed=self.seed, num_cpu=num_cpu)
            paths = trajectory_sampler.sample_data_batch(**input_dict)

        # compute returns
        process_samples.compute_returns(paths, gamma)

        # push samples to rb
        self.baseline.buffer.push_many(paths)

        # loop over number of policy updates

        update_stats = []
        total_losses = []
        bellman_losses = []
        reconstruction_losses = []
        reward_losses = []
        total_update_time = 0.0
        update_policy_time = 0.0
        for k in range(self.num_policy_updates):
            # fit the Q function
            # losses, btime = self.baseline.bellman_update()
            total_loss, bellman_loss, reconstruction_loss, \
                reward_loss, update_time = self.baseline.bellman_update(all_losses=True)
            # update the policy
            start = time.time()
            stat = self.update_policy(paths, self.fit_on_policy and k == 0)
            update_policy_time += time.time() - start
            
            update_stats.append(stat)
            total_losses.append(total_loss)
            bellman_losses.append(bellman_loss)
            reconstruction_losses.append(reconstruction_loss)
            reward_losses.append(reward_loss)
            total_update_time += update_time
        
        path_returns = [sum(p["rewards"]) for p in paths]
        mean_return = np.mean(path_returns)
        std_return = np.std(path_returns)
        min_return = np.amin(path_returns)
        max_return = np.amax(path_returns)

        if self.summary_writer:
            self.summary_writer.add_scalar('MeanReturn/train', mean_return, iteration)
            self.summary_writer.add_scalar('MinReturn/train', min_return, iteration)
            self.summary_writer.add_scalar('MaxReturn/train', max_return, iteration)
            self.summary_writer.add_scalar('StdReturn/train', std_return, iteration)

            self.summary_writer.add_scalar('BufferSize', self.baseline.buffer['observations'].shape[0], iteration)
            self.summary_writer.add_scalar('Time/BellmanUpdate', total_update_time, iteration)
            self.summary_writer.add_scalar('Time/PolicyUpdate', update_policy_time, iteration)

            total_means = []
            bellman_means = []
            recon_means = []
            reward_means = []
            for i, (stat, total_loss, losses, recon_loss, reward_loss) in enumerate(zip(update_stats,
                total_losses, bellman_losses, reconstruction_losses, reward_losses)):

                alpha, n_step_size, t_gLL, t_FIM, surr_before, surr_after, kl_dist = stat
                self.summary_writer.add_scalar('alpha/sub_iteration_{}'.format(i), alpha, iteration)
                self.summary_writer.add_scalar('delta/sub_iteration_{}'.format(i), n_step_size, iteration)
                self.summary_writer.add_scalar('time_vpg/sub_iteration_{}'.format(i), t_gLL, iteration)
                self.summary_writer.add_scalar('time_npg/sub_iteration_{}'.format(i), t_FIM, iteration)
                self.summary_writer.add_scalar('surr_improvement/sub_iteration_{}'.format(i), surr_after - surr_before, iteration)
                self.summary_writer.add_scalar('kl_dist/sub_iteration_{}'.format(i), kl_dist, iteration)

                mean_total_loss = self._log_stats('TotalLoss', i, iteration, total_loss)
                total_means.append(mean_total_loss)

                mean_bellman_loss = self._log_stats('BellmanLoss', i, iteration, losses)
                bellman_means.append(mean_bellman_loss)

                mean_recon_loss = self._log_stats('ReconstructionLoss', i, iteration, recon_loss)
                recon_means.append(mean_recon_loss)

                mean_reward_loss = self._log_stats('RewardLoss', i, iteration, reward_loss)
                reward_means.append(mean_reward_loss)

            self.summary_writer.add_scalar('MeanTotalLoss/mean', np.mean(total_means), iteration)
            self.summary_writer.add_scalar('MeanBellmanLoss/mean', np.mean(bellman_means), iteration)
            self.summary_writer.add_scalar('MeanReconstructionLoss/mean', np.mean(recon_means), iteration)
            self.summary_writer.add_scalar('MeanRewardLoss/mean', np.mean(reward_means), iteration)
            
            # log the policy stds
            stds = np.exp(self.policy.log_std.detach().numpy())
            for i, std in enumerate(stds):
                self.summary_writer.add_scalar('PolicyStd/std_{}'.format(i), std, iteration)

            # mse between predicted q and mc rollouts
            pred_1, mc_1 = evaluate_n_step(1, gamma, paths, self.baseline)
            pred_end, mc_end = evaluate_start_end(gamma, paths, self.baseline)

            self.summary_writer.add_scalar('QFunctionMCMSE_single', mse(pred_1, mc_1), iteration)
            self.summary_writer.add_scalar('QFunctionMCMSE_end', mse(pred_end, mc_end), iteration)

        return [mean_return, std_return, min_return, max_return]

    def _log_stats(self, key, i, iteration, losses):
        mean_loss = np.mean(losses)
        min_loss = np.min(losses)
        max_loss = np.max(losses)
        
        self.summary_writer.add_scalar('Mean{}/sub_iteration_{}'.format(key, i), mean_loss, iteration)
        self.summary_writer.add_scalar('Max{}/sub_iteration_{}'.format(key, i), max_loss, iteration)
        self.summary_writer.add_scalar('Min{}/sub_iteration_{}'.format(key, i), min_loss, iteration)
        return mean_loss

    def update_policy(self, paths, fit_on_policy):
        if fit_on_policy:
            observations, actions, weights = self.process_paths(paths)
        else:
            observations, actions, weights = self.process_replay()

        # TODO: more normalize weight modes
        weights = (weights - np.mean(weights)) / (np.std(weights) + 1e-6)

        # update policy
        surr_before = self.CPI_surrogate(observations, actions, weights).data.numpy().ravel()[0]
        alpha, n_step_size, t_gLL, t_FIM, new_params = self.update_from_states_actions(observations, actions, weights)
        
        if np.isnan(new_params).any():
            import pdb; pdb.set_trace()
            raise RuntimeError('policy has nan params')

        surr_after = self.CPI_surrogate(observations, actions, weights).data.numpy().ravel()[0]
        kl_dist = self.kl_old_new(observations, actions).data.numpy().ravel()[0]
        self.policy.set_param_values(new_params, set_new=True, set_old=True)
        return alpha, n_step_size, t_gLL, t_FIM, surr_before, surr_after, kl_dist

    def update_from_states_actions(self, observations, actions, weights):
        t_gLL = 0.0
        t_FIM = 0.0

        # Optimization algorithm
        # --------------------------

        # VPG
        ts = time.time()
        vpg_grad = self.flat_vpg(observations, actions, weights)
        t_gLL += time.time() - ts

        # NPG
        ts = time.time()
        hvp = self.build_Hvp_eval([observations, actions],
                                regu_coef=self.FIM_invert_args['damping'])
        npg_grad = cg_solve(hvp, vpg_grad, x_0=vpg_grad.copy(),
                            cg_iters=self.FIM_invert_args['iters'])
        t_FIM += time.time() - ts

        # Step size computation
        # --------------------------
        if self.alpha is not None:
            alpha = self.alpha
            n_step_size = (alpha ** 2) * np.dot(vpg_grad.T, npg_grad)
        else:
            n_step_size = self.n_step_size
            alpha = np.sqrt(np.abs(self.n_step_size / (np.dot(vpg_grad.T, npg_grad) + 1e-20)))

        # Policy update
        # --------------------------
        curr_params = self.policy.get_param_values()
        new_params = curr_params + alpha * npg_grad
        self.policy.set_param_values(new_params, set_new=True, set_old=False)

        return alpha, n_step_size, t_gLL, t_FIM, new_params

    def process_paths(self, paths):

        observations = np.concatenate([path["observations"] for path in paths])
        actions = np.concatenate([path["actions"] for path in paths])
        times = np.concatenate([path["time"] for path in paths])

        Qs = self.baseline.predict(observations, actions, times)
        _, _, Vs = self.baseline.compute_average_value(observations, times)
        weights = Qs - Vs.detach().cpu().numpy()

        return observations, actions, weights
    
    def process_replay(self):

        # TODO: check which is faster

        samples = self.baseline.buffer.get_sample(self.num_update_states)

        start_cpu = time.time()

        observations = samples['observations'].to('cpu').numpy() # TODO: don't convert to/from numpy
        times = samples['time'].to('cpu').numpy()
        actions = self.policy.get_action_batch(observations)
        Qs = self.baseline.predict(observations, actions, times)
        _, _, Vs = self.baseline.compute_average_value(observations, times)
        weights = Qs - Vs.detach().cpu().numpy()
        # return observations, actions, weights

        end_cpu = time.time()

        observations = samples['observations']
        times = samples['time']
        actions = self.policy.get_action_pytorch(observations)
        Qs = self.baseline.forward(observations, actions, times)
        _, _, Vs = self.baseline.compute_average_value(observations, times)
        weights = Qs - Vs

        end_gpu = time.time()

        print('gpu', end_gpu - end_cpu, 'cpu', end_cpu - start_cpu)

        return weights.detach().cpu().numpy()
