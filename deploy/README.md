# UDC infrastructure deployment

Deployment scripts for the `infrastructure` repository. They install and verify
the `udc-resource` broker, CLI, SSH restrictions, udev rule, and systemd unit.

**Do not run these files on amax while training is active.** The code is
implemented, but deployment still requires training completion and explicit
administrator approval.

## Order

1. Run `bash deploy/preflight.sh --config lab/resources.local.toml` and
   `bash deploy/audit_legacy.sh` (both read-only):
   Docker, systemd, NVIDIA runtime, udev, sshd, groups, cgroup, disk,
   `/dev/nvidia*` permissions, legacy root/sudo/SSH/privileged-container access.
   For the development profile, preflight also requires the `overlay2` driver,
   an XFS DockerRootDir mounted with `prjquota`/`pquota`, `xfs_quota`, and a
   workspace root mounted `nosuid` (container root is host root without a user
   namespace, so setuid files written through the workspace bind must be
   ignored by the host).
   The GPU permission gate also requires `stat`, `su`, `getent`, `udevadm`, and
   `getfacl` (the `acl` package); preflight fails if any is missing.
2. Install Python 3.12 (uv or deadsnakes), then run:

   ```bash
   bash deploy/install_server.sh --dry-run --skip-preflight
   bash deploy/install_server.sh --yes
   ```

   The installer is idempotent. It refuses to modify the host without `--yes`,
   does not overwrite an existing config unless `--force-config`, does not start
   the broker unless `--start`. A fresh database starts in `monitor`; a
   reinstall with active/pending leases preserves `active` or `maintenance`
   so lease expiry is never silently paused. Before changing config, systemd,
   udev, sshd, or symlink files it creates a timestamped backup under
   `/var/backups/udc-resource/`; validation failures trigger an EXIT trap that
   restores those files and, when the broker was stopped, the SQLite state
   snapshot. Group creation and a live broker's database are retained (the
   rollback warns instead of replacing state under a running daemon). Keep an
   existing admin SSH session open regardless.
3. Create member accounts with:

   ```bash
   bash deploy/create_user.sh --yes --ssh-key /path/to/key.pub udc-alice
   ```

   Members get `udc-members` + `udc-cli`, SSH `ForceCommand`, and the
   root-owned `/usr/local/libexec/udc-resource/member-shell` login shell. The
   installer does not change existing account shells; re-run `create_user.sh`
   for each existing member before active mode. Admins use `--admin`, keep a
   normal shell, and must not be in `udc-members`.
4. Install the udev rule (done by the installer) and verify host GPU access is
   denied to a test member. `verify_gpu_permissions.sh` checks every
   `/dev/nvidia*` node, its NVIDIA/misc udev subsystem, mode/owner,
   unexpected ACLs, forbidden groups (`udc-gpu`, `video`, `render`, Docker,
   sudo, admins), and direct member read/write failures. Omit `--member` to
   check every effective `udc-members` account, or use `--json` for the
   Broker gate format. Re-run it after reboot and after the NVIDIA driver
   recreates nodes.
5. The installer selects `monitor` for a fresh install. On reinstall it keeps
   `active`/`maintenance` when leases exist, so the expiry worker cannot be
   paused accidentally.
6. Install the sshd drop-in, run `sshd -t`, then reload. The member match block
   is key-only (`AuthenticationMethods publickey`, password and keyboard
   interactive disabled), sets `PermitUserRC no`, disables forwarding/tunnels,
   and forces the login wrapper. Keep an admin SSH session open while changing
   SSH.
7. Start in monitor-only mode:

   ```bash
   systemctl start udc-resource-broker
   bash deploy/verify_monitor_only.sh --member udc-alice
   ```

   The verifier is read-only: it checks ForceCommand, socket permissions,
   Docker/GPU denials, monitor mode, and that Docker/GPU state is unchanged over
   the snapshot window (default 10 minutes) and all GPU/NVIDIA device
   permissions through `verify_gpu_permissions.sh`.

   `monitor` is read-only and never expires leases. If active/pending leases
   exist, use `maintenance`: it blocks new applies but keeps expiry and
   reconciliation running. The broker refuses `admin mode monitor` while leases
   or managed containers exist.
8. Only after explicit approval, switch to active mode and run a CPU then
   single-GPU acceptance allocation.

See `docs/legacy-access-revocation.md` for the formal legacy administrator
revocation and verification checklist, `docs/jobs-and-transfer.md` for detached
tmux jobs and restricted file transfer, and `docs/acceptance.md` for the
destructive active-mode acceptance.

## Files

- `preflight.sh` (read-only checks; run before installing anything)
- `audit_legacy.sh` (read-only legacy root/sudo/SSH/privileged-container audit)
- `install_server.sh` (idempotent installer; `--dry-run`, `--yes`, `--start`, `--no-timers`)
- `create_user.sh` (member/admin account and optional SSH key setup)
- `member-shell.sh` (root-owned member login shell installed by the installer)
- `verify_gpu_permissions.sh` (read-only all-GPU/NVIDIA device permission acceptance; supports `--json` and no-member mode)
- `verify_monitor_only.sh` (read-only monitor-only security acceptance)
- `check_disk.sh` (workspace free-space alert; exit 0/1/2)
- `configure_quotas.sh` (XFS/ext4 project quotas; dry-run by default, `--yes` to apply)
- `rotate_job_logs.py` (symlink-safe job-log rotation)
- `acceptance_active.py` (destructive active-mode acceptance; see `docs/acceptance.md`)
- `systemd/udc-resource-broker.service`
- `systemd/udc-resource-logrotate.service` / `.timer`
- `systemd/udc-resource-disk-check.service` / `.timer`
- `udev/99-udc-gpu.rules`
- `sshd/60-udc-resource.conf`
- `docker/Dockerfile.base`
- `docker/sshd_config`
- `docker/bootstrap.sh`
- `../lab/members.example.toml` (public collaborator directory example)

Before enabling file transfer, run `configure_quotas.sh` during a maintenance
window and schedule `check_disk.sh --min-free-gb 50` (or a systemd timer). The
example config enables the development profile with a writable rootfs,
passwordless container `sudo`, a per-allocation tmpfs HOME, a `storage_opt`
writable-layer quota, PID limits, and an optional GPU group. The container is
still unprivileged at the Docker level: no privileged mode, Docker socket,
host PID/network namespace, or host system mounts. `userns_mode` is empty by
default and accepts `host` only for an explicit daemon-level decision.
Validate the real image, Docker storage driver, GPU group, container sshd,
and namespace mode on the target host before active acceptance.

The development profile requires `overlay2` on XFS with project quotas; if
`preflight.sh --config` reports any of those checks as failed, do not enable
the profile. VS Code support additionally requires the image built from
`deploy/docker/Dockerfile.base`; it remains gated until the target container
sshd, tunnel teardown, and quota tests pass.

The broker socket must be `root:udc-cli 0660`; members must not get Docker,
sudo, or GPU device groups. The container must not mount the host Docker socket.
`install_server.sh` never creates or stops containers.

## Member display directory and disk controls

Copy `lab/members.example.toml` to `/etc/udc-resource/members.toml` (root-owned,
mode 0644) to add an optional Chinese display name, nickname, and **public**
contact for each Linux account. The broker exposes only `display_name`,
`nickname`, and `public_contact`; never put private phone numbers, private
email, or student IDs in this file. The GUI and `list` output use these fields
for GPU cards, allocations, and unmanaged processes.

Before enabling file transfer, apply per-directory project quotas with:

```bash
bash deploy/configure_quotas.sh --workspace-root /data1/udc-users \
  --limit-gb 200 --yes
```

The command is a dry-run unless `--yes` is supplied. XFS/ext4 mounts must have
`prjquota` enabled during a maintenance window; the script will not remount
`/data1`. Schedule `deploy/check_disk.sh --path /data1/udc-users --min-free-gb 50`
(cron or a systemd timer) for free-space alerts. The installer installs and
enables `udc-resource-logrotate.timer` and `udc-resource-disk-check.timer` by
default; use `install_server.sh --no-timers` to leave them disabled. The log
timer calls the descriptor-relative `rotate_job_logs.py` helper rather than
logrotate's symlink-following copytruncate path; the disk timer runs
`check_disk.sh` and exits non-zero for warning/critical thresholds so the
failure is visible in `systemctl --failed` and the journal.
