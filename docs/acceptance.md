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
- public member-directory entries for the supplied local member usernames
- `policy.file_transfer_enabled = true` only after quotas and TOCTOU tests pass
- an explicit `--gpu` index that is currently `free`
- an empty active/pending allocation set
- a passing `audit_legacy.sh --production` (invalid root keys are fatal unless
  `allow_root_ssh_keys=true` is explicitly configured)

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

## Five checks

1. **Real SSH boundary**
   - member CLI `list --json` succeeds;
   - `bash`, `docker ps`, and arbitrary GPU probes are rejected through SSH;
   - socket parent mode is `0711 root:root` and the member can reach the
     broker socket through the CLI;
   - if local users are supplied, direct `docker ps`, `nvidia-smi`, and read/write
     access to every `/dev/nvidia*` node fail for those accounts;
     `verify_gpu_permissions.sh` checks all eight visible GPUs, every additional
     NVIDIA node, effective group membership, ACLs, and the installed
     `KERNEL=="nvidia*"` udev rule. Re-run it after a reboot and after reloading
     the NVIDIA driver.
   - `list` exposes only the public member-directory fields (`display_name`,
     `nickname`, `public_contact`), and each supplied local member has a
     profile. Private contact fields must not appear in the response.

2. **Concurrent same-GPU apply**
   - two members apply for the same test GPU at the same time with
     `--no-queue`;
   - exactly one receives `ok=true`;
   - the other receives `gpu_busy`;
   - the winner is released and the GPU returns to `free`.

3. **Legacy GPU holder without compute**
   - a temporary running container is created with Docker GPU access and
     `sleep infinity`, so it has no CUDA compute process;
   - member apply with `--no-queue` for that GPU must fail with `gpu_busy`, and
     default apply must create a queued request without creating a GPU claim;
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

5. **Queue admission and cancellation**
    - member A holds the test GPU, then member B applies for the same GPU with
      queueing enabled; B receives `queue_id` with state `queued` and no GPU
      claim;
    - A releases the GPU; within the expiry/scheduling interval B's request
      becomes `admitted`, its allocation becomes `active`, and the GPU is
      claimed by B only;
    - repeat with a second queued request and cancel it as its owner; it must
      become `cancelled`, create no allocation, and leave the GPU free;
    - `queue list --history` shows the terminal state; `queue list --all` is
      rejected for a member and accepted for an admin;
    - queue admission must pause (not guess) when live GPU detection fails.

## Development container and VS Code acceptance

Run this only on a dedicated test allocation after the five checks and only
when the host is not serving other users. It must not stop, restart, or alter
training containers.

1. **Storage and image preflight**
   - `preflight.sh --config <config>` reports the development profile as valid:
     `overlay2`, XFS DockerRootDir with `prjquota`/`pquota`, `xfs_quota`,
     workspace project quotas, and a workspace filesystem mounted `nosuid`.
     Confirm the last point with `findmnt -no OPTIONS -T /data1/udc-users`;
     container root is host root without a user namespace, so a setuid file
     written through the workspace bind must not be honored by the host.
   - The GPU audit prerequisites `stat`, `su`, `getent`, `udevadm`, and `getfacl`
     are installed.
   - After `configure_quotas.sh` (or after adding a member), confirm the test
     member's workspace directory has the configured project hard limit with
     `xfs_quota -x -c 'report -p -b -h' <mount>` (XFS) or `repquota -P <mount>`
     (ext4). A quota gate that only sees mount support is not enough.
   - Build `deploy/docker/Dockerfile.base` on the target host and record the
     image digest; do not use a floating tag for acceptance.
2. **Container privilege boundary**
   - Apply for the test GPU with the development profile and wait for active.
   - Inside the container, `id` shows the member UID; `sudo -n true` succeeds;
     `sudo -n cat /etc/shadow` succeeds only inside the container.
   - From the host, the member still cannot run a host shell, `docker`, or
     `nvidia-smi`; host root is never reachable from the container.
   - Inspect the container: no `Privileged`, no `/var/run/docker.sock`, no host
     PID/network namespace, no host system mounts, and `storage_opt` is present.
3. **Disposable versus persistent state**
   - Write a marker under `/workspace` and a marker under `$HOME`.
   - Release and re-apply; the `/workspace` marker survives and the `$HOME`
     marker is gone (fresh tmpfs).
   - Confirm the writable-layer quota rejects growth beyond the configured
     `writable_layer_gb` without affecting `/workspace`.
4. **Broker-mediated VS Code tunnel**
   - With `vscode_enabled = true`, run `udc-resource vscode <allocation-id>`;
     VS Code connects as the member UID and opens `/workspace`.
   - The local listener is bound to `127.0.0.1` only. The generated SSH config
     disables host-key verification for the loopback-only alias; confirm no
     non-loopback listener and document that the trust boundary is the local
     account, not the container host key.
   - Host SSH forwarding remains disabled; the host login wrapper still rejects
     arbitrary commands and a direct `ssh -L` attempt. The container sshd may
     allow local TCP forwarding for Remote-SSH, but no remote/Unix-socket
     forwarding, agent forwarding, or tun devices.
   - Opening more than `vscode_max_tunnels` connections is refused, including
     concurrent attempts that race the slot check.
   - Release and expiry cancel the tunnel; the container sshd process and Docker
     exec session exit, and no worker thread remains blocked.
5. **Gates remain closed**
   - File transfer stays disabled until TOCTOU-safe handling and quota tests
     pass.
   - A failed GPU audit, workspace quota check, or storage preflight blocks new
     allocations while existing tasks continue and can still be released.

Retain the image digest, `docker inspect` security fields, preflight output,
tunnel logs, and the before/after workspace and HOME marker results.

## Evidence to retain

Save the script output plus:

- member `list --json` before/after;
- `systemctl status udc-resource-broker`;
- `docker ps -a` before/after;
- test allocation `status` before/after expiry;
- workspace marker path and existence;
- queue request before/after release and cancellation, including `queue list
  --history --json`;
- `audit_legacy.sh --production` JSON output (or human output) and
  `verify_gpu_permissions.sh` output before/after reboot;
- development-profile preflight output, image digest, and `docker inspect`
  security fields;
- VS Code tunnel logs and cancellation evidence.

Abort immediately if an unmanaged container is touched, a GPU is double
allocated, a non-owner can release, or another user's container/workspace is
affected.
