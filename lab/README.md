# Lab resource inventory

Static resource inventory for the `infrastructure` repository.

This directory records static lab resources. Live GPU/container occupancy is
discovered by the broker and must never be written into Git.

## Files

- `resources.example.toml` — committed template with placeholders.
- `resources.local.toml` — real local inventory; **ignored by Git**.
- This README — non-secret context.

## amax snapshot

Status: initialized 2026-10-22. The server is still in use for training; do not
deploy or restart Docker.

| Item | Value |
|---|---|
| Host | `amax` (local SSH alias) |
| GPUs | 8 × NVIDIA GeForce RTX 4090, 49140 MiB reported per card |
| CPU | 128 logical CPUs |
| RAM | 251 GiB |
| Data volume | `/data1` (large shared volume; usage changes) |
| Docker | 28.1.1 observed previously |
| Python | 3.10.12 observed previously; deployment baseline is 3.12 |
| Workspace root | `/data1/udc-users` (to be created by installer) |
| Broker socket | `/run/udc-resource/broker.sock` |
| Broker state | `/var/lib/udc-resource/state.db` |
| Base image | `udc-base:2026.10` |

Observed on 2026-10-22 approximately 17:03 CST: all eight GPUs had other users'
compute processes. This is a transient snapshot, **not a reservation**. Always
re-query `nvidia-smi` before launching.

## Deployment gate

- Run `bash deploy/preflight.sh` first; it is read-only and must pass before
  installing anything.
- Use `deploy/install_server.sh --dry-run` before a real `--yes` install; it
  never starts allocations and forces monitor mode.
- Use `deploy/create_user.sh` for member/admin accounts; admins must not be in
  `udc-members`.
- Use `deploy/verify_monitor_only.sh --member <user>` for the read-only
  monitor-only acceptance window.
- Host Python is 3.10.12; install Python 3.12 via uv or deadsnakes first.
- `policy.mode = "monitor"` and `deployment_allowed = false` until training ends
  and the lab owner explicitly approves.
- The first deployment is monitor-only: read containers/GPU usage, do not create
  or stop anything.
- Unknown containers are displayed as unmanaged; never kill them.
- The server previously crashed under host-memory pressure. Containers must set
  `--memory` and `--memory-swap`; broker must not load full datasets into RAM.

## Enforcement assumptions

- Members have independent Linux accounts and no `sudo`, `docker`, or GPU device
  group membership.
- Member SSH is restricted by `ForceCommand` to the `udc-resource` CLI.
- The broker runs as root via systemd and is the only component with Docker
  access.
- GPU device ACL and forced-command setup must be verified on the actual host
  before claiming strong enforcement.
