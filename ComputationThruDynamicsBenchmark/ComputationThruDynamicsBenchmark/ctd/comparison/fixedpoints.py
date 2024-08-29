import numpy as np
import pytorch_lightning as pl
import torch

from ctd.comparison.utils import FixedPoints


def find_fixed_points(
    model: pl.LightningModule,
    state_trajs: np.array,
    inputs: np.array,
    n_inits=1024,
    noise_scale=0.0,
    learning_rate=1e-2,
    max_iters=10000,
    device="cpu",
    seed=0,
    compute_jacobians=False,
):
    # set the seed
    torch.manual_seed(seed)
    np.random.seed(seed)

    model = model.to(device)
    state_trajs = state_trajs.to(device)
    inputs = inputs.to(device)

    # Prevent gradient computation for the neural ODE
    for parameter in model.parameters():
        parameter.requires_grad = False

    # Choose random points along the observed trajectories
    if len(state_trajs.shape) > 2:
        n_samples, n_steps, state_dim = state_trajs.shape
        state_pts = state_trajs.reshape(-1, state_dim)
        if len(inputs.shape) > 1:
            inputs = inputs.reshape(-1, inputs.shape[-1])
        idx = torch.randint(n_samples * n_steps, size=(n_inits,), device=device)
    else:
        n_samples_steps, state_dim = state_trajs.shape
        state_pts = state_trajs
        idx = torch.randint(n_samples_steps, size=(n_inits,), device=device)

    # Select the initial states
    states = state_pts[idx]
    if len(inputs.shape) > 1:
        inputs = inputs[idx]
    else:
        inputs = inputs.unsqueeze(0).repeat(n_inits, 1)

    # Add Gaussian noise to the sampled points
    states = states + noise_scale * torch.randn_like(states, device=device)

    # Require gradients for the states
    states = states.detach()
    initial_states = states.detach().cpu().numpy()
    states.requires_grad = True

    # Create the optimizer
    opt = torch.optim.Adam([states], lr=learning_rate)

    # Run the optimization
    iter_count = 1
    q_prev = torch.full((n_inits,), float("nan"), device=device)
    while True:
        # Compute q and dq for the current states
        print("MODEL")
        print(model)
        print("INPUTS")
        print(inputs)
        print("STATES")
        print(states)
        F = model(inputs, states)
        q = 0.5 * torch.sum((F.squeeze() - states.squeeze()) ** 2, dim=1)
        dq = torch.abs(q - q_prev)
        q_scalar = torch.mean(q)

        # Backpropagate gradients and optimize
        q_scalar.backward()
        opt.step()
        opt.zero_grad()

        # Detach evaluation tensors
        q_np = q.cpu().detach().numpy()
        dq_np = dq.cpu().detach().numpy()
        # Report progress
        if iter_count % 500 == 0:
            mean_q, std_q = np.mean(q_np), np.std(q_np)
            mean_dq, std_dq = np.mean(dq_np), np.std(dq_np)
            print(f"\nIteration {iter_count}/{max_iters}")
            print(f"q = {mean_q:.2E} +/- {std_q:.2E}")
            print(f"dq = {mean_dq:.2E} +/- {std_dq:.2E}")

        # Check termination criteria
        if iter_count + 1 > max_iters:
            print("Maximum iteration count reached. Terminating.")
            break
        q_prev = q
        iter_count += 1
    # Collect fixed points

    qstar = q.cpu().detach().numpy()
    all_fps = FixedPoints(
        xstar=states.cpu().detach().numpy().squeeze(),
        x_init=initial_states,
        qstar=qstar,
        dq=dq.cpu().detach().numpy(),
        n_iters=np.full_like(qstar, iter_count),
    )

    print(f"Found {len(all_fps.xstar)} unique fixed points.")
    if compute_jacobians:
        # Compute the Jacobian for each fixed point
        def J_func(model, inputs_, x):
            # This function takes both the additional inputs and the state.
            F = model(inputs_, x)
            return F.squeeze()

        def compute_jacobians_func(model, inputs, x_data):
            all_J = []
            x = torch.tensor(x_data, device=device)

            for i in range(x.size(0)):
                inputs_1 = inputs[i, :].unsqueeze(0)
                single_x = x[i, :].unsqueeze(0)

                J = torch.autograd.functional.jacobian(
                    lambda x: J_func(model, inputs_1, x), single_x
                )
                all_J.append(J.squeeze())

            return all_J

        all_J = compute_jacobians_func(model, inputs, all_fps.xstar)
        # Recombine and decompose Jacobians for the whole batch
        if all_J:
            dFdx = torch.stack(all_J).cpu().detach().numpy()
            all_fps.J_xstar = dFdx
            all_fps.decompose_jacobians()

            return all_fps
        else:
            return []
    else:
        return all_fps


def find_fixed_points_coupled(
    model: pl.LightningModule,
    context_inputs: np.array,
    env_states: np.array,
    model_states: np.array,
    joint_states: np.array,
    n_inits=1024,
    noise_scale=0.0,
    learning_rate=1e-2,
    max_iters=10000,
    device="cpu",
    seed=0,
    compute_jacobians=False,
):
    # set the seed
    torch.manual_seed(seed)
    np.random.seed(seed)

    model = model.to(device)
    model_states = model_states.to(device)
    env_states = env_states.to(device)
    context_inputs = context_inputs.to(device)
    joint_states = joint_states.to(device)

    # Model takes in "model_input" and "hidden"
    # Model input is the concatenation of
    # the environment states and the context inputs (in that order)
    # Hidden is the hidden state of the model

    rand_inds = torch.randint(0, env_states.size(0), (n_inits,), device=device)
    env_states = env_states[rand_inds]
    model_states = model_states[rand_inds]
    context_inputs = context_inputs[rand_inds]
    joint_states = joint_states[rand_inds]

    env_states = env_states.detach() + noise_scale * torch.randn_like(
        env_states, device=device
    )
    model_states = model_states.detach() + noise_scale * torch.randn_like(
        model_states, device=device
    )

    env_states.requires_grad = True
    model_states.requires_grad = True
    # Create the optimizer
    opt = torch.optim.Adam([env_states, model_states], lr=learning_rate)
    initial_states = torch.cat((env_states, model_states), dim=1).detach().cpu().numpy()

    # Run the optimization
    iter_count = 1
    q_model_prev = torch.full((n_inits,), float("nan"), device=device)
    q_env_prev = torch.full((n_inits,), float("nan"), device=device)
    while True:
        # Compute q and dq for the current states
        (
            action,
            hidden_step,
            env_states_step,
            joint_states_step,
        ) = model.forward_step_coupled(
            env_states, context_inputs, model_states, joint_states
        )

        q_model = 0.5 * torch.sum(
            (hidden_step.squeeze() - model_states.squeeze()) ** 2, dim=1
        )
        q_env = 0.5 * torch.sum(
            (env_states_step.squeeze() - env_states.squeeze()) ** 2, dim=1
        )

        dq_model = torch.abs(q_model - q_model_prev)
        dq_env = torch.abs(q_env - q_env_prev)

        q_model_scalar = torch.mean(q_model)
        q_env_scalar = torch.mean(q_env)

        q_scalar = q_model_scalar + q_env_scalar
        q = q_model + q_env
        dq = dq_model + dq_env

        # Backpropagate gradients and optimize
        q_scalar.backward()
        opt.step()
        opt.zero_grad()

        # Detach evaluation tensors
        q_np = q.cpu().detach().numpy()
        dq_np = dq.cpu().detach().numpy()
        # Report progress
        if iter_count % 10 == 0:
            mean_q, std_q = np.mean(q_np), np.std(q_np)
            mean_dq, std_dq = np.mean(dq_np), np.std(dq_np)
            print(f"\nIteration {iter_count}/{max_iters}")
            print(f"q = {mean_q:.2E} +/- {std_q:.2E}")
            print(f"dq = {mean_dq:.2E} +/- {std_dq:.2E}")

        # Check termination criteria
        if iter_count + 1 > max_iters:
            print("Maximum iteration count reached. Terminating.")
            break
        q_model_prev = q_model
        q_env_prev = q_env
        iter_count += 1
    # Collect fixed points
    states = torch.cat((env_states, model_states), dim=1)
    qstar = q.cpu().detach().numpy()
    all_fps = FixedPoints(
        xstar=states.cpu().detach().numpy().squeeze(),
        x_init=initial_states,
        qstar=qstar,
        dq=dq.cpu().detach().numpy(),
        n_iters=np.full_like(qstar, iter_count),
    )

    print(f"Found {len(all_fps.xstar)} unique fixed points.")
    if compute_jacobians:  # TODO: Fix this
        # Compute the Jacobian for each fixed point
        def J_func(model, inputs_, x):
            # This function takes both the additional inputs and the state.
            F = model(inputs_, x)
            return F.squeeze()

        def compute_jacobians_func(model, inputs, x_data):
            all_J = []
            x = torch.tensor(x_data, device=device)

            for i in range(x.size(0)):
                inputs_1 = inputs[i, :].unsqueeze(0)
                single_x = x[i, :].unsqueeze(0)

                J = torch.autograd.functional.jacobian(
                    lambda x: J_func(model, inputs_1, x), single_x
                )
                all_J.append(J.squeeze())

            return all_J

        all_J = compute_jacobians_func(model, all_fps.xstar)
        # Recombine and decompose Jacobians for the whole batch
        if all_J:
            dFdx = torch.stack(all_J).cpu().detach().numpy()
            all_fps.J_xstar = dFdx
            all_fps.decompose_jacobians()

            return all_fps
        else:
            return []
    else:
        return all_fps


def find_fixed_points_dt(
    model: pl.LightningModule,
    state_trajs: np.array,
    inputs: np.array,
    n_inits=1024,
    noise_scale=0.0,
    learning_rate=1e-2,
    max_iters=10000,
    device="cpu",
    seed=0,
    compute_jacobians=False,
):
    # set the seed
    torch.manual_seed(seed)
    np.random.seed(seed)

    model = model.to(device)
    state_trajs = state_trajs.to(device)
    inputs = inputs.to(device)

    # Prevent gradient computation for the neural ODE
    for parameter in model.parameters():
        parameter.requires_grad = False

    # Choose random points along the observed trajectories
    if len(state_trajs.shape) > 2:
        n_samples, n_steps, state_dim = state_trajs.shape
        state_pts = state_trajs.reshape(-1, state_dim)
        if len(inputs.shape) > 1:
            inputs = inputs.reshape(-1, inputs.shape[-1])
        idx = torch.randint(n_samples * n_steps, size=(n_inits,), device=device)
    else:
        n_samples_steps, state_dim = state_trajs.shape
        state_pts = state_trajs
        idx = torch.randint(n_samples_steps, size=(n_inits,), device=device)

    # Select the initial states
    states = state_pts[idx]
    if len(inputs.shape) > 1:
        inputs = inputs[idx]
    else:
        inputs = inputs.unsqueeze(0).repeat(n_inits, 1)

    # Add Gaussian noise to the sampled points
    states = states + noise_scale * torch.randn_like(states, device=device)

    # Require gradients for the states
    states = states.detach()
    initial_states = states.detach().cpu().numpy()
    states.requires_grad = True

    # Create the optimizer
    opt = torch.optim.Adam([states], lr=learning_rate)

    # Run the optimization
    iter_count = 1
    q_prev = torch.full((n_inits,), float("nan"), device=device)
    x_store = np.zeros((n_inits, max_iters, state_dim))
    q_store = np.zeros((n_inits, max_iters))
    while True:
        # Compute q and dq for the current states
        x_store[:, iter_count - 1, :] = states.cpu().detach().numpy()
        q_store[:, iter_count - 1] = q_prev.cpu().detach().numpy()
        _, F = model.decoder(inputs, states)
        q = 0.5 * torch.sum((F.squeeze() - states.squeeze()) ** 2, dim=1)
        dq = torch.abs(q - q_prev)
        q_scalar = torch.mean(q)

        # Backpropagate gradients and optimize
        q_scalar.backward()
        opt.step()
        opt.zero_grad()

        # Detach evaluation tensors
        q_np = q.cpu().detach().numpy()
        dq_np = dq.cpu().detach().numpy()
        # Report progress
        if iter_count % 500 == 0:
            mean_q, std_q = np.mean(q_np), np.std(q_np)
            mean_dq, std_dq = np.mean(dq_np), np.std(dq_np)
            print(f"\nIteration {iter_count}/{max_iters}")
            print(f"q = {mean_q:.2E} +/- {std_q:.2E}")
            print(f"dq = {mean_dq:.2E} +/- {std_dq:.2E}")

        # Check termination criteria
        if iter_count + 1 > max_iters:
            print("Maximum iteration count reached. Terminating.")
            break
        q_prev = q
        q_store[:, iter_count - 1] = q_prev.cpu().detach().numpy()
        iter_count += 1
    # Collect fixed points

    qstar = q.cpu().detach().numpy()
    all_fps = FixedPoints(
        xstar=states.cpu().detach().numpy().squeeze(),
        x_init=initial_states,
        qstar=qstar,
        dq=dq.cpu().detach().numpy(),
        n_iters=np.full_like(qstar, iter_count),
    )

    print(f"Found {len(all_fps.xstar)} unique fixed points.")
    if compute_jacobians:
        # Compute the Jacobian for each fixed point
        def J_func(model, inputs_, x):
            # This function takes both the additional inputs and the state.
            _, F = model(inputs_, x)
            return F.squeeze()

        def compute_jacobians_func(model, inputs, x_data):
            all_J = []
            x = torch.tensor(x_data, device=device)

            for i in range(x.size(0)):
                inputs_1 = inputs[i, :].unsqueeze(0)
                single_x = x[i, :].unsqueeze(0)

                J = torch.autograd.functional.jacobian(
                    lambda x: J_func(model, inputs_1, x), single_x
                )
                all_J.append(J.squeeze())

            return all_J

        all_J = compute_jacobians_func(model, inputs, all_fps.xstar)
        # Recombine and decompose Jacobians for the whole batch
        if all_J:
            dFdx = torch.stack(all_J).cpu().detach().numpy()
            all_fps.J_xstar = dFdx
            all_fps.decompose_jacobians()

            return all_fps
        else:
            return []
    else:
        return all_fps
