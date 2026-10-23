# Long-running jobs and restricted file transfer

Both features are available through the member CLI and the broker socket. They
do not expose a host shell, Docker socket, SFTP, or SCP.

## Prerequisites

- Use an allocation in `active` status.
- The base image must contain `tmux`. `deploy/docker/Dockerfile.base` installs
  it; rebuild the image after changing the Dockerfile.
- The allocation workspace is mounted at `/workspace` in the container and is
  also readable by the root broker on the host.

## Detached jobs

Use `job start` for training commands. The command is detached from the SSH
session, keeps running when the SSH connection drops, and writes both stdout
and stderr to a persistent log in the allocation workspace.

```bash
# Everything after -- is passed to the job unchanged.
udc-resource job start <allocation-id> --name train -- \
  python train.py --config configs/train.yaml

udc-resource job list <allocation-id>
udc-resource job logs <allocation-id> --name train --tail 200
udc-resource job attach <allocation-id> --name train
udc-resource job stop <allocation-id> --name train
```

The session name is `udc-<name>`; names are limited to
`[A-Za-z0-9_.-]{1,32}`. The log is stored at:

```text
<workspace>/.udc-logs/<name>.log
```

`job attach` is an interactive tmux client. If SSH disconnects, only the
attach client dies; the tmux session and training process continue. Reconnect
and attach again, or use `job logs` without attaching. A completed command
remains readable in its log even though it no longer appears in `job list`.

Notes:

- Jobs run as root inside the allocation container, with access to the
  allocation's GPU devices.
- `job stop` kills the tmux session and its process. Prefer application-level
  checkpointing; stopping a training process is not graceful.
- `docker logs` only shows the container entrypoint. Use `job logs` for job
  output.
- The broker records `job_started`/`job_stopped` audit events.

## Restricted file transfer

`push` and `pull` stream bytes through the existing SSH/ForceCommand path and
the broker's Unix socket. The broker writes only inside the allocation
workspace; host files, absolute paths, `..`, and symlink targets are rejected.
The operation requires an active allocation and either its owner or a broker
administrator.

```bash
# Local workstation -> server workspace
udc-resource push <allocation-id> ./data/train.tar /workspace/data/train.tar

# Server workspace -> local workstation
udc-resource pull <allocation-id> /workspace/checkpoints/best.pt ./best.pt
```

The third argument is a path relative to the workspace. A leading `/workspace/`
is accepted for convenience and normalized to a relative path. Uploads
overwrite an existing regular file atomically; uploads use a unique temporary
file and `0600` permissions. Directories, symlinks, and recursive transfers are
not supported. Use a tar archive for directory trees.

The remote-side primitives are also available for diagnostics:

```bash
udc-resource file put <allocation-id> path/inside/workspace < local-file
udc-resource file get <allocation-id> path/inside/workspace > local-file
```

`file get` writes only file content to stdout; errors go to stderr and a
non-zero exit status. `push`/`pull` use the configured SSH alias (or `--server`)
and write to a `.part` file before replacing the local destination.
