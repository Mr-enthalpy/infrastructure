# Active-mode acceptance

This acceptance is **destructive by design** and must only run after training
has ended, the lab owner has approved, and a dedicated test GPU and test
accounts are available. Do not run it on amax while training containers or
other users' allocations are present.

The script refuses to run without:

- `--yes`
- `--allow-test-container`
- `--allow-db-time-travel`
- two distinct SSH aliases for member accounts
- an explicit `--gpu` index that is currently `free`
- an empty active/pending allocation set
- a passing legacy access audit

## Run

Run as root **on the server**, with root's SSH config able to log in as the two
member accounts:

```bash
cd /path/to/infrastructure
/opt/udc-resource/.venv/bin/python deploy/acceptance_active.py \
  --member-a udc-alice \
  --member-b udc-bob \
  --member-a-user udc-alice \
  --member-b-user udc-bob \
  --gpu 7 \
  --allow-test-container \
  --allow-db-time-travel \
  --yes
```

`--member-a-user` / `--member-b-user` are local Linux usernames used to test
Docker and every NVIDIA device node through
`deploy/verify_gpu_permissions.sh`; the SSH aliases alone only prove that
ForceCommand rejects arbitrary commands.

The script records its own test allocations and removes the temporary holder
container on exit. It does not delete workspaces or other users' containers.

## Four checks

1. **Real SSH boundary**
   - member CLI `list --json` succeeds;
   - `bash`, `docker ps`, and arbitrary GPU probes are rejected through SSH;
   - socket parent mode is `0711 root:root` and the member can reach the
     broker socket through the CLI;
   - if local users are supplied, direct `docker ps`, `nvidia-smi`, and read/write
     access to every `/dev/nvidia*` node fail for those accounts;
     `verify_gpu_permissions.sh` checks all eight visible GPUs.

2. **Concurrent same-GPU apply**
   - two members apply for the same test GPU at the same time;
   - exactly one receives `ok=true`;
   - the other receives `gpu_busy`;
   - the winner is released and the GPU returns to `free`.

3. **Legacy GPU holder without compute**
   - a temporary running container is created with Docker GPU access and
     `sleep infinity`, so it has no CUDA compute process;
   - member apply for that GPU must fail with `gpu_busy`;
   - the temporary container is always removed. A stopped legacy container
     must not block a GPU and is reported as historical only.

4. **Expiry, restart, SSH disconnect, jobs, workspace**
   - a test allocation and a CPU-only bystander allocation are created;
   - a marker is written through an SSH PTY shell, then the SSH client is
     terminated; the allocation container must stay running;
   - restricted file `put`/`get` transfers the exact bytes through SSH and the
     broker, while a `..` traversal path is rejected as `forbidden`;
   - a detached `tmux` job writes `job-started` to its workspace log; two
     attach sessions are terminated without killing the job; the log must be
     readable afterward;
   - the broker is restarted; the allocation, marker, detached job, and job log
     must survive; the job must reattach after the restart;
   - the job is stopped, then with `--allow-db-time-travel`, only the test
     allocation's `expires_at` is moved near-term in SQLite;
   - expiry worker stops the container, marks the allocation `expired`, releases
     the claim, and keeps the workspace marker;
   - the bystander is unaffected.

## Evidence to retain

Save the script output plus:

- member `list --json` before/after;
- `systemctl status udc-resource-broker`;
- `docker ps -a` before/after;
- test allocation `status` before/after expiry;
- workspace marker path and existence;
- legacy audit output.

Abort immediately if an unmanaged container is touched, a GPU is double
allocated, a non-owner can release, or another user's container/workspace is
affected.
