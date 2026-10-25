# UDC infrastructure deployment guide

## 1. Scope and safety requirements

This directory contains the deployment and verification tooling for the
`udc-resource` broker, CLI, SSH policy, udev rule, systemd units, development
container image, and host-side verification scripts.

Deployment is prohibited while the host is serving training workloads or when
explicit administrator approval has not been recorded. The current project
status, target-host unknowns, and approved next steps are maintained in
[`docs/handoff.md`](../docs/handoff.md). The acceptance procedure is defined in
[`docs/acceptance.md`](../docs/acceptance.md).

All verification scripts marked as read-only must be executed before any
state-changing step. A read-only script must be reviewed before execution; it
must not stop containers, restart Docker or NVIDIA services, alter allocations,
or modify users, groups, SSH configuration, or storage.

## 2. Preconditions

The following conditions must be satisfied before installation:

- Training has ended and the lab owner has approved deployment.
- A verified administrator SSH session is available and remains open during
  SSH and systemd changes.
- Python 3.12 is installed on the host. The system Python may be older and must
  not be used as the production runtime.
- `lab/resources.local.toml` contains the target host inventory and remains
  excluded from Git.
- The read-only checks in Section 3 pass. For the development profile, the
  storage, quota, and `nosuid` requirements in Section 5 are mandatory.

## 3. Read-only pre-deployment checks

Run the following commands from the repository root as root on the target host:

```bash
bash deploy/preflight.sh --config lab/resources.local.toml
bash deploy/audit_legacy.sh --production
```

`preflight.sh` checks Docker, systemd, the NVIDIA runtime, udev, sshd, required
groups, cgroup configuration, disk capacity, `/dev/nvidia*` permissions, and the
development-profile storage requirements. `audit_legacy.sh` inventories legacy
root, sudo, SSH, Docker, GPU, and privileged-container access. Both scripts are
read-only.

The GPU permission gate also requires `stat`, `su`, `getent`, `udevadm`, and
`getfacl` (provided by the `acl` package). `preflight.sh` must fail if any of
these tools is unavailable.

## 4. Installation sequence

1. Validate the installer in dry-run mode:

   ```bash
   bash deploy/install_server.sh --dry-run --skip-preflight
   ```

2. Install the broker, CLI, login wrapper, member shell, verification scripts,
   systemd units, udev rule, and SSH drop-in:

   ```bash
   bash deploy/install_server.sh --yes
   ```

   The installer is idempotent. It does not modify the host without `--yes`, does
   not overwrite an existing configuration unless `--force-config` is supplied,
   and does not start the broker unless `--start` is supplied. A fresh database
   starts in `monitor` mode. A reinstall with active/pending leases preserves
   `active` or `maintenance` so lease expiry is not silently paused.

   Before changing the configuration, systemd units, udev rules, sshd
   configuration, or managed symlinks, the installer creates a timestamped
   backup under `/var/backups/udc-resource/`. Validation failures trigger an EXIT
   trap that restores the backed-up files and, when the broker was stopped, the
   SQLite state snapshot. Group creation and a live broker database are retained;
   rollback warns rather than replacing state under a running daemon.

3. Create member accounts with an explicit SSH key:

   ```bash
   bash deploy/create_user.sh --yes --ssh-key /path/to/key.pub udc-alice
   ```

   Members receive `udc-members` and `udc-cli` membership, the SSH
   `ForceCommand`, and the root-owned
   `/usr/local/libexec/udc-resource/member-shell` login shell. The installer does
   not change existing account shells; run `create_user.sh` for every existing
   member before active mode. Administrators use `--admin`, retain a normal
   shell, and must not be members of `udc-members`.

   Each account also runs a workspace provisioning transaction: the member
   directory is created, the project ID (= UID) and the hard quota
   (`--limit-gb`, default 200) are assigned, and the result is verified by
   querying the filesystem back. Use `--no-workspace` to skip it. The Broker
   re-verifies — and if needed reprovisions — this quota before every
   allocation, so a missing quota fails the allocation instead of leaving the
   workspace unbounded.

4. Install and validate the SSH policy. The installer installs the sshd drop-in,
   runs `sshd -t`, and reloads sshd. The member match block is key-only
   (`AuthenticationMethods publickey`), disables password and keyboard-interactive
   authentication, sets `PermitUserRC no`, disables forwarding and tunnels, and
   applies the login wrapper. Keep the verified administrator session open.

5. Verify that the installed udev rule denies host GPU access to a test member:

   ```bash
   bash deploy/verify_gpu_permissions.sh --member udc-alice --expected-gpus 8
   ```

   Omit `--member` to check every effective `udc-members` account. Use `--json`
   for the Broker gate format. Re-run the check after a reboot and after the
   NVIDIA driver recreates device nodes.

6. Apply per-directory workspace project quotas before enabling active
   allocations or file transfer. The command is a dry run unless `--yes` is
   supplied:

   ```bash
   bash deploy/configure_quotas.sh \
     --workspace-root /data1/udc-users \
     --limit-gb 200 \
     --yes
   ```

   XFS and ext4 mounts must have project quotas enabled during a maintenance
   window. The script does not remount `/data1`. `create_user.sh` provisions
   each new member's quota automatically; use this script to backfill or change
   the limit for existing workspaces, then verify each member's project hard
   limit with `xfs_quota -x -c 'report -p -b -h' <mount>` on XFS or
   `repquota -P <mount>` on ext4.

7. Start the broker in monitor-only mode and run the read-only security
   acceptance:

   ```bash
   systemctl start udc-resource-broker
   bash deploy/verify_monitor_only.sh --member udc-alice
   ```

   The verifier checks `ForceCommand`, socket permissions, Docker/GPU denials,
   monitor mode, and the absence of Docker/GPU state changes during the snapshot
   window (default: 10 minutes). It also invokes
   `verify_gpu_permissions.sh` for every visible NVIDIA device.

   `monitor` is read-only and never expires leases. When active or pending leases
   exist, use `maintenance`: it blocks new applications while continuing expiry
   and reconciliation. The broker refuses `admin mode monitor` while leases or
   managed containers exist.

8. Only after explicit approval and completion of the read-only checks may the
   broker be switched to active mode and the CPU and single-GPU acceptance
   allocations be created. Do not run the destructive acceptance procedure on a
   host that is still serving training containers or other users' allocations.

## 5. Development-profile gates

The development container profile is allowed only when all of the following
conditions hold:

- The Docker storage driver is `overlay2`.
- DockerRootDir is on XFS mounted with `prjquota` or `pquota`.
- `xfs_quota` is installed and operational.
- Workspace project quotas are available and the workspace root exists.
- The workspace filesystem is mounted with `nosuid`. Without a user namespace,
  container UID 0 maps to host UID 0; `nosuid` prevents setuid or file-capability
  binaries written through the workspace bind from being honored by the host.

If any condition fails, the development profile must remain disabled. Do not
weaken the check or bypass it by using `userns_mode = "host"` without validating
GPU devices and bind mounts first.

Build the base image only on the target host after the storage checks pass:

```bash
docker build -f deploy/docker/Dockerfile.base -t udc-base:2026.10 deploy/docker
```

Record the immutable image digest and use that digest for acceptance. A floating
tag is not acceptable for acceptance evidence.

## 6. File transfer and VS Code

`file_transfer_enabled` and `vscode_enabled` remain `false` until their
respective target-host tests pass. File transfer requires TOCTOU-safe handling,
working per-directory quotas, and target-host acceptance. VS Code requires a
verified container sshd, Broker tunnel lifecycle, quota enforcement, and
allocation release/expiry behavior.

The local listener for VS Code must bind only to `127.0.0.1`. The generated SSH
config disables host-key verification for the loopback-only alias; the local
account, not the container host key, is the trust boundary. Host SSH forwarding
must remain disabled.

## 7. systemd timers and operational controls

The installer installs and enables `udc-resource-logrotate.timer` and
`udc-resource-disk-check.timer` by default. Use `--no-timers` to leave them
disabled. The log timer invokes the descriptor-relative
`rotate_job_logs.py` helper instead of a logrotate `copytruncate` rule. The disk
timer runs `check_disk.sh` and exits non-zero for warning and critical
thresholds so failures are visible in `systemctl --failed` and the journal.

The broker socket must be owned by `root:udc-cli` with mode `0660`. Members must
not receive Docker, sudo, GPU device group, `video`, or `render` access. The
container must not mount the host Docker socket.

## 8. File inventory

- `preflight.sh`: read-only host and development-profile preflight.
- `audit_legacy.sh`: read-only legacy root/sudo/SSH/privileged-container audit.
- `install_server.sh`: idempotent installer; supports `--dry-run`, `--yes`,
  `--start`, and `--no-timers`.
- `create_user.sh`: member/admin account and optional SSH key setup.
- `member-shell.sh`: root-owned member login shell installed by the installer.
- `verify_gpu_permissions.sh`: read-only GPU/NVIDIA device permission
  verification; supports `--json` and no-member mode.
- `verify_monitor_only.sh`: read-only monitor-only security acceptance.
- `check_disk.sh`: workspace free-space alert; exits 0, 1, or 2.
- `configure_quotas.sh`: XFS/ext4 project quota setup; dry-run by default.
- `rotate_job_logs.py`: symlink-safe job-log rotation.
- `acceptance_active.py`: destructive active-mode acceptance harness; see
  `docs/acceptance.md`.
- `systemd/udc-resource-broker.service`: hardened broker unit.
- `systemd/udc-resource-logrotate.service` and `.timer`: log rotation timer.
- `systemd/udc-resource-disk-check.service` and `.timer`: disk alert timer.
- `udev/99-udc-gpu.rules`: NVIDIA device-node ownership rule.
- `sshd/60-udc-resource.conf`: member SSH match block.
- `docker/Dockerfile.base`, `docker/sshd_config`, `docker/bootstrap.sh`:
  development container definitions.
- `../lab/members.example.toml`: public member-directory example.
