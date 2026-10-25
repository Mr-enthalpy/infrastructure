# Active-mode acceptance

## 1. Purpose and authorization

This procedure verifies the security and functional boundaries of the deployed
resource broker. It changes host and database state and is therefore restricted
to a dedicated test allocation and an approved maintenance window.

The procedure must not be executed on any host while training containers or
other users' allocations are present. On amax, it additionally requires training
to have ended and explicit approval from the lab owner. It requires:

- a dedicated test GPU that is currently `free`;
- two distinct dedicated member accounts and SSH aliases;
- public member-directory entries for the supplied local member usernames;
- no active or pending allocation before the test starts;
- a passing `audit_legacy.sh --production` run; invalid root SSH keys are fatal
  unless `allow_root_ssh_keys = true` is explicitly configured;
- a successful development-profile preflight when testing the development
  container or VS Code;
- `policy.file_transfer_enabled = true` only for the controlled file-transfer
  portion after quota and TOCTOU preconditions pass;
- `policy.vscode_enabled = true` only for the controlled VS Code portion after
  container-sshd and tunnel preconditions pass.

The harness is `deploy/acceptance_active.py`. It refuses to run without
`--yes`, `--allow-test-container`, and `--allow-db-time-travel`. It records the
allocations and temporary holder containers it creates and removes its own
temporary holder container on exit. It does not delete workspaces or other
users' containers.

## 2. Preconditions and execution context

Run the harness as root on the server. Root's SSH configuration must be able to
log in as the two member accounts.

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

`--member-a-user` and `--member-b-user` are local Linux usernames. They are used
to test Docker and every NVIDIA device node through
`deploy/verify_gpu_permissions.sh`; SSH aliases alone only demonstrate that
`ForceCommand` rejects arbitrary commands.

Before starting, verify the read-only prerequisites in
[`docs/handoff.md`](handoff.md) and `deploy/preflight.sh`. Record the current
`docker ps -a` and `nvidia-smi` output as the baseline.

## 3. Read-only prerequisites

The following checks must pass before authorizing active mode. They do not
modify host state and must be retained as evidence:

- `preflight.sh --config <config>` reports the selected container profile as
  valid.
- For the development profile, Docker uses `overlay2`; DockerRootDir is on XFS
  with `prjquota` or `pquota`; `xfs_quota` is available; workspace project quotas
  are enabled; and the workspace filesystem is mounted `nosuid`.
- Confirm the `nosuid` mount directly with
  `findmnt -no OPTIONS -T /data1/udc-users`. Without a user namespace, container
  root is host root; a setuid file written through the workspace bind must not
  be honored by the host.
- The GPU audit prerequisites `stat`, `su`, `getent`, `udevadm`, and `getfacl`
  are installed.
- After `configure_quotas.sh`, or after adding a member, confirm that the test
  member's workspace has the configured project hard limit. Use
  `xfs_quota -x -c 'report -p -b -h' <mount>` on XFS or
  `repquota -P <mount>` on ext4. A mount-support check alone is insufficient.
- The broker unit reports the expected hardening properties:
  `ProtectHome=read-only`, `ProtectSystem=strict`, and a `ReadWritePaths` set
  that includes the configured workspace, state database, and broker runtime
  directory.
- Build `deploy/docker/Dockerfile.base` on the target host, record the image
  digest, and use the digest rather than a floating tag for acceptance.

## 4. Five active-mode checks

### 4.1 Real SSH boundary

- A member CLI `list --json` request succeeds.
- `bash`, `docker ps`, and arbitrary GPU probes are rejected through SSH.
- The broker socket parent directory has mode `0711 root:root`, and the member
  can reach the broker socket only through the CLI.
- If local users are supplied, direct `docker ps`, `nvidia-smi`, and read/write
  access to every `/dev/nvidia*` node fail for those accounts.
  `verify_gpu_permissions.sh` checks all eight visible GPUs, every additional
  NVIDIA node, effective group membership, ACLs, and the installed
  `KERNEL=="nvidia*"` udev rule. Re-run it after a reboot and after reloading the
  NVIDIA driver.
- `list` exposes only the public member-directory fields `display_name`,
  `nickname`, and `public_contact`. Each supplied local member must have a
  profile. Private contact fields must not appear in the response.

### 4.2 Concurrent same-GPU apply

- Two members request the same test GPU concurrently with `--no-queue`.
- Exactly one request returns `ok=true`.
- The other returns `gpu_busy`.
- The winner releases the GPU, and the GPU returns to `free`.

### 4.3 Legacy GPU holder without compute

- A temporary running container is created with Docker GPU access and
  `sleep infinity`, so it has no CUDA compute process.
- A member apply with `--no-queue` for that GPU must fail with `gpu_busy`.
  A default apply must create a queued request without creating a GPU claim.
- The temporary container is removed in all cases. A stopped legacy container
  must not block a GPU and is reported as historical only.

### 4.4 Expiry, restart, SSH disconnect, jobs, and workspace

- A test allocation and a CPU-only bystander allocation are created.
- A marker is written through an SSH PTY shell, then the SSH client is
  terminated. The allocation container must remain running.
- Restricted file `put` and `get` transfer the exact bytes through SSH and the
  broker, while a `..` traversal path is rejected as `forbidden`. The policy gate
  may be enabled only for this controlled acceptance run after the quota and
  TOCTOU preconditions are met, and must be disabled immediately afterward.
- A detached tmux job writes `job-started` to its workspace log. Two attach
  sessions are terminated without killing the job; the log must remain readable.
- The broker is restarted. The allocation, marker, detached job, and job log
  must survive, and the job must reattach after the restart.
- The job is stopped. With `--allow-db-time-travel`, only the test allocation's
  `expires_at` is moved near-term in SQLite.
- The expiry worker stops the container, marks the allocation `expired`,
  releases the claim, and keeps the workspace marker.
- The CPU-only bystander is unaffected.

### 4.5 Queue admission and cancellation

- Member A holds the test GPU. Member B applies for the same GPU with queueing
  enabled.
- B receives a `queue_id` with state `queued` and no GPU claim.
- A releases the GPU. Within the expiry/scheduling interval, B's request becomes
  `admitted`, its allocation becomes `active`, and the GPU is claimed only by B.
- Repeat with a second queued request and cancel it as its owner. It must become
  `cancelled`, create no allocation, and leave the GPU free.
- `queue list --history` shows the terminal state. `queue list --all` is rejected
  for a member and accepted for an administrator.
- Queue admission must pause rather than guess when live GPU detection fails.
- A direct apply must not bypass an unexpired queued request even when
  `queue_enabled = false`. `--no-queue` means no waiting and no retained request;
  it does not permit queue jumping.

## 5. Development container and VS Code acceptance

Execute this phase only on a dedicated test allocation after the five checks in
Section 4 pass, and only when the host is not serving other users. It must not
stop, restart, or alter training containers.

### 5.1 Storage and image preflight

- `preflight.sh --config <config>` reports the development profile as valid:
  `overlay2`, XFS DockerRootDir with `prjquota`/`pquota`, working `xfs_quota`,
  workspace project quotas, and a workspace filesystem mounted `nosuid`.
- The image is built from `deploy/docker/Dockerfile.base`; its digest is recorded;
  the configured image reference is pinned to that digest for acceptance.
- The GPU audit prerequisites are present.

### 5.2 Container privilege boundary

- Apply for the test GPU with the development profile and wait for `active`.
- Inside the container, `id` shows the member UID; `sudo -n true` succeeds;
  `sudo -n cat /etc/shadow` succeeds only inside the container.
- From the host, the member still cannot obtain a host shell, run `docker`, or run
  `nvidia-smi`.
- `docker inspect` confirms: `Privileged` is false; the Docker socket is not
  mounted; host PID and network namespaces are not used; no host system
  directories are mounted; and `storage_opt` is present.

### 5.3 Disposable and persistent state

- Write one marker under `/workspace` and one under `$HOME`.
- Release and re-apply. The `/workspace` marker must survive; the `$HOME` marker
  must be absent because HOME lives in the container writable layer (or in the
  optional tmpfs when `container_home_tmpfs_gb > 0`) and is destroyed with the
  container.
- Confirm that the writable-layer quota rejects growth beyond
  `writable_layer_gb` and that the rejection does not affect `/workspace`.

### 5.4 Broker-mediated VS Code tunnel

- With `vscode_enabled = true`, run `udc-resource vscode <allocation-id>`.
  VS Code must connect as the member UID and open `/workspace`.
- The local listener must bind only to `127.0.0.1`. The generated SSH config
  disables host-key verification for the loopback-only alias. Confirm that no
  non-loopback listener exists and document that the local account, not the
  container host key, is the trust boundary.
- Host SSH forwarding must remain disabled. The host login wrapper must reject
  arbitrary commands and direct `ssh -L` attempts. The container sshd may allow
  local TCP forwarding for Remote-SSH, but remote forwarding, Unix-socket
  forwarding, agent forwarding, and tun devices must remain disabled.
- Opening more than `vscode_max_tunnels` connections must be refused, including
  concurrent attempts that race the slot check.
- Release and expiry must cancel the tunnel. The container sshd process and the
  Docker exec session must exit, and no worker thread may remain blocked.

### 5.5 GPU device access on both login paths

The container identity initialization must create a supplementary group with
the host `udc-gpu` GID inside the container. Docker's `group_add` only applies
to docker-exec sessions; an sshd login rebuilds supplementary groups from the
container's `/etc/group`, so the VS Code path is the regression risk. Verify
both paths separately, inside the same test allocation:

- Via `udc-resource shell <allocation-id>` (docker-exec path):
  1. `id -G` must list the host `udc-gpu` GID.
  2. `nvidia-smi -L` must list the allocated GPUs (and only concerns the
     member, not root).
  3. If the image ships PyTorch: `python -c "import torch;
     print(torch.cuda.is_available())"` must print `True`.
- Via the VS Code terminal (container sshd path): repeat the same three
  checks in the SSH login session. A failure here means the identity
  initialization did not propagate the GPU group.

Record `id -G`, `nvidia-smi -L`, and the torch output for both paths in the
evidence. `VS Code can execute /bin/true` alone is not sufficient evidence.

### 5.6 Container-root impact on host device nodes

The development container does not use a user namespace by default: container
UID 0 maps to host UID 0, and the NVIDIA runtime bind-mounts the host device
nodes. Whether container root (available to the member through sudo) can
change the ownership or permissions of those host nodes — and thereby affect
other members' access — cannot be proven by CI and must be verified on an
approved test machine or an explicitly approved test GPU, during the approved
maintenance window, with the host device state recorded before and after.

Procedure (one allocated test GPU, one test member):

1. On the host, record the full pre-state:
   `stat -c '%n %t:%T %a %u:%g' /dev/nvidia* /dev/nvidia-caps/nvidia* > before.txt`.
2. Inside the container, as the member via sudo, attempt (in this order, on
   the allocated GPU's node only):
   `sudo stat /dev/nvidia<allocated>`,
   `sudo chmod 0666 /dev/nvidia<allocated>`,
   `sudo chown 1000:1000 /dev/nvidia<allocated>`.
3. On the host, repeat the `stat` capture into `after.txt` and diff. If the
   host inode changed, the container root can mutate host device state.
4. As a second (non-allocated) test member on the host, attempt to read the
   device before and after the change and record the difference.
5. Restore the original mode/owner on the host node immediately and verify
   with `diff before.txt after-restored.txt`.

Record the complete transcript. If container root can mutate host nodes, note
it in the acceptance report together with the compensating controls that must
then be treated as load-bearing: the continuous GPU-permission audit (device
fingerprint changes block new allocations), the absence of host mounts and
Docker socket in the container, and workspace `nosuid`. Evaluate a
`userns-remap` configuration as a follow-up; do not enable it without the
dedicated verification listed in the handoff.

### 5.7 Closed gates

- File transfer must remain disabled outside the controlled acceptance run until
  TOCTOU-safe handling and quota tests pass.
- A failed GPU audit, workspace quota check, or storage preflight must block new
  allocations while existing tasks continue and remain releasable.

## 6. Evidence to retain

Retain the complete command output and the following artifacts:

- member `list --json` output before and after the test;
- `systemctl status udc-resource-broker` and relevant journal excerpts;
- `docker ps -a` before and after the test;
- test allocation status before and after expiry;
- workspace marker path and existence result;
- queue request state before and after release and cancellation, including
  `queue list --history --json`;
- `audit_legacy.sh --production` output and `verify_gpu_permissions.sh` output
  before and after reboot;
- development-profile preflight output, image digest, and `docker inspect`
  security fields;
- VS Code tunnel logs and cancellation evidence;
- operator name, timestamps, and approval reference for every state change.

## 7. Abort and recovery criteria

Abort the procedure immediately and preserve logs if any of the following
occurs:

- an unmanaged container is started, stopped, removed, or otherwise modified;
- a GPU is allocated to more than one allocation;
- a non-owner can release or stop another user's allocation;
- another user's container or workspace is affected;
- a queue request jumps ahead of an unexpired queued request;
- a security gate fails open;
- the broker cannot be stopped or restarted cleanly.

After aborting, release only the test allocations created by the harness, remove
only the harness-created temporary container, and leave existing training
containers and unmanaged resources untouched. Record the state of the database
and the test workspace before and after recovery.
