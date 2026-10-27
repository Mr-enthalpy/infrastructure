# Lab resource inventory

## 1. Purpose

This directory records the static resource inventory for the `infrastructure`
repository. Live GPU and container occupancy is discovered by the broker and must
never be committed to Git.

## 2. Files

- `resources.example.toml`: committed template with placeholder values.
- `resources.local.toml`: the actual local inventory; **ignored by Git**.
- `README.md`: non-secret context for the inventory.

## 3. example-gpu snapshot

Status: initialized on 2026-10-22. The host is currently used for training.
Deployment and Docker restarts are prohibited until training has ended and the
lab owner has approved deployment.

| Item | Value |
|---|---|
| Host | `example-gpu` (local SSH alias) |
| GPUs | `<GPU model/count>` (not published) |
| CPU | `<CPU count>` (not published) |
| RAM | `<RAM>` (not published) |
| Data volume | `/data1` (shared volume; usage changes) |
| Docker | 28.1.1 observed previously |
| Python | 3.10.12 observed previously; deployment baseline is 3.12 |
| Workspace root | `/data1/citespec-users`; created by the installer |
| Broker socket | `/run/citespec-resource/broker.sock` |
| Broker state | `/var/lib/citespec-resource/state.db` |
| Base image | `citespec-base:2026.10` |

Exact hardware inventory is intentionally not published in this repository;
the ignored local inventory is authoritative for the deployment host.

The GPU workload observed on 2026-10-22 at approximately 17:03 CST was a
transient snapshot, not a reservation. It must not be used as current state.
Query `nvidia-smi` and the broker before launching any workload.

## 4. Deployment gate

- Run `bash deploy/preflight.sh` and `bash deploy/audit_legacy.sh` first. Both
  scripts are read-only and must pass before installation or active mode.
- Use `deploy/install_server.sh --dry-run` before a real `--yes` installation.
  The installer does not create allocations and initially enforces monitor mode.
- Use `deploy/create_user.sh` for member and administrator accounts.
  Administrators must not be members of `citespec-members`.
- Use `deploy/verify_monitor_only.sh --member <user>` for the read-only
  monitor-only acceptance window.
- The host Python is 3.10.12. Install Python 3.12 through `uv` or the deadsnakes
  PPA before deployment.
- `policy.mode = "monitor"` and `deployment_allowed = false` remain in effect
  until training ends and the lab owner explicitly approves deployment.
- The first deployment must be monitor-only: it may read container and GPU usage
  but must not create or stop containers.
- Unknown containers are reported as unmanaged and must never be killed.
- The server previously experienced host-memory pressure. Containers must set
  `--memory` and `--memory-swap`; the broker must not load full datasets into
  memory.

## 5. Enforcement assumptions

- Members have independent Linux accounts and no `sudo`, Docker, or GPU device
  group membership.
- Member SSH is restricted by `ForceCommand` to the `citespec-resource` CLI.
- The broker runs as root through systemd and is the only component with Docker
  access.
- GPU device ACLs and forced-command configuration must be verified on the
  actual host before strong enforcement is claimed.
