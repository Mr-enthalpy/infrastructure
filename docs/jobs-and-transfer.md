# Long-running jobs and restricted file transfer

## 1. Scope and feature gates

Long-running jobs and restricted file transfer are available through the member
CLI and the broker socket. Neither feature exposes a host shell, the Docker
socket, SFTP, or SCP.

File transfer is disabled by default (`policy.file_transfer_enabled = false`).
It must remain disabled until the TOCTOU handling, per-directory quota, and
target-host acceptance checks have passed. Enabling it for an acceptance window
does not authorize permanent production use.

**Scope**: `file_transfer_enabled` gates only the Broker's restricted
`file put`/`get` API (TOCTOU-safe, descriptor-relative, bounded by
`max_upload_bytes`). It does NOT — and is not intended to — prevent data
movement inside the container: the container sshd runs an `internal-sftp`
subsystem for VS Code Remote-SSH, and once a VS Code tunnel exists the member
can read and write `/workspace` through it without any Broker-side byte cap.
That is by design (container-based development is unrestricted inside the
allocation). The disk-protection responsibility therefore rests on the
workspace project quota and the writable-layer budget, not on
`file_transfer_enabled`.

## 2. Prerequisites

- The allocation must be in `active` status.
- The base image must contain `tmux`. `deploy/docker/Dockerfile.base` installs
  it; rebuild the image after changing the Dockerfile.
- The allocation workspace is mounted at `/workspace` in the container and is
  readable by the root broker on the host.
- A project quota must be assigned to every member workspace before uploads are
  enabled. The broker byte limit is not a substitute for a filesystem quota
  because members can also write directly through the mounted workspace.

## 3. Detached jobs

`job start` is the supported entry point for training commands. The command is
detached from the SSH session, continues after the SSH connection closes, and
writes both stdout and stderr to a persistent log in the allocation workspace.

```bash
# Arguments after -- are passed to the job unchanged.
citespec-resource job start <allocation-id> --name train -- \
  python train.py --config configs/train.yaml

citespec-resource job list <allocation-id>
citespec-resource job logs <allocation-id> --name train --tail 200
citespec-resource job attach <allocation-id> --name train
citespec-resource job stop <allocation-id> --name train
```

The tmux session name is `citespec-<name>`. Names are limited to
`[A-Za-z0-9_.-]{1,32}`. Logs are stored at:

```text
<workspace>/.citespec-logs/<name>.log
```

`job attach` starts an interactive tmux client. If SSH disconnects, only the
attach client terminates; the tmux session and the training process continue.
Reconnect and attach again, or use `job logs` without attaching. A completed
command remains readable in its log even though it no longer appears in
`job list`.

Operational requirements:

- Jobs run as the configured `policy.container_user` (default `member`, mapped to
  the allocation owner's host UID/GID) and have access to the allocation's GPU
  devices. Workspace files and `.citespec-logs` are owned by that member.
- `job stop` terminates the tmux session and its process tree. Application-level
  checkpointing is required for graceful training recovery; stopping a training
  process is not a graceful shutdown.
- `docker logs` shows only the container entrypoint. Use `job logs` for job
  output.
- The broker records `job_started` and `job_stopped` audit events.
- Job logs are rotated by `deploy/rotate_job_logs.py`. The helper opens every
  directory with `O_NOFOLLOW` and refuses symlinks. A generic `copytruncate`
  logrotate rule must not be used for the member-writable `.citespec-logs` directory.

## 4. Restricted file transfer

`push` and `pull` stream bytes through the existing SSH/ForceCommand path and the
broker Unix socket. The broker writes only inside the allocation workspace; host
files, absolute paths, `..`, and symlink targets are rejected. The operation
requires an active allocation and either its owner or a broker administrator.

```bash
# Local workstation to server workspace
citespec-resource push <allocation-id> ./data/train.tar /workspace/data/train.tar

# Server workspace to local workstation
citespec-resource pull <allocation-id> /workspace/checkpoints/best.pt ./best.pt
```

The path argument is relative to the workspace. A leading `/workspace/` is
accepted for convenience and normalized to a relative path. Uploads overwrite an
existing regular file atomically and use a unique temporary file with mode
`0600`. Uploads are limited by `policy.max_upload_bytes` (default: 20 GiB).
Directories, symlinks, FIFOs, devices, and recursive transfers are not supported;
use a tar archive for directory trees.

`push` and `pull` require an active allocation. After `release`, workspace files
remain on disk and the persistent job logs are still available through
`job files`/`job logs`, but `pull` returns `conflict: allocation is released, not
active`. Retrieve checkpoints, metrics, and any other non-log artifact before
releasing the allocation.

The remote-side primitives are available for diagnostics:

```bash
citespec-resource file put <allocation-id> path/inside/workspace < local-file
citespec-resource file get <allocation-id> path/inside/workspace > local-file
```

`file get` writes only file content to stdout; errors are written to stderr with a
non-zero exit status. `push` and `pull` use the configured SSH alias or
`--server` and write to a `.part` file before replacing the local destination.

## 5. Security invariants

- Members must not obtain a host shell, Docker access, GPU device group
  membership, SFTP, or SCP.
- The broker must not follow symlinks or escape the allocation workspace.
- File transfer must remain subject to project quotas and the configured upload
  limit.
- Job and transfer operations must be rejected for allocations that are not
  active or that are owned by another member, unless the caller is a broker
  administrator.
- Logs and uploaded files must not be used to bypass the workspace `nosuid`
  mount or host SSH policy.
