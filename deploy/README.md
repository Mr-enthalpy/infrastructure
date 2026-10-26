# Citespec infrastructure deployment guide

## 1. Scope and safety requirements

This directory contains the deployment and verification tooling for the
`citespec-resource` broker, CLI, SSH policy, udev rule, systemd units, development
container image, and host-side verification scripts.

Deployment is prohibited while the host is serving training workloads or when
explicit administrator approval has not been recorded. The current project
status, target-host unknowns, and approved next steps are maintained in
[`docs/handoff.md`](../docs/handoff.md). The acceptance procedure is defined in
[`docs/acceptance.md`](../docs/acceptance.md).

This round implements the intake, approval, executor, central-key SSH policy,
and setup/client-profile paths in code and automated tests. The intake and
provision units, real `sshd` central-key behavior, `useradd`, quotas, and the
browser flow have **not** been executed on a disposable Linux host; D2 remains
pending. Do not cite local unit tests as proof that these host integrations work.

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

2. Install the broker, CLI, login wrapper, member shell, provisioning executor,
   intake service, verification scripts, systemd units, udev rule, and SSH
   drop-in:

   ```bash
   bash deploy/install_server.sh --yes
   ```

   If Python 3.11+ is only available through a root-owned uv environment, pass an
   accessible interpreter explicitly, for example
   `--python /opt/python3.12/bin/python3.12`; otherwise uv may make the service
   venv point at a path that the low-privilege services cannot traverse.

   The installer is idempotent. It does not modify the host without `--yes`, does
   not overwrite an existing configuration unless `--force-config` is supplied,
   and does not start the broker unless `--start` is supplied. It installs the
   Python package with the `gui` extra (needed by the intake service), creates
   the root-managed key directory `/etc/citespec-resource/authorized-keys.d`, the
   provisioning spool `/var/lib/citespec-resource/provision`, and the isolated intake
   state directory. A fresh database starts in `monitor` mode. A reinstall with
   active/pending leases preserves `active` or `maintenance` so lease expiry is
   not silently paused.

   Before changing the configuration, systemd units, udev rules, sshd
   configuration, or managed symlinks, the installer creates a timestamped
   backup under `/var/backups/citespec-resource/`. Validation failures trigger an EXIT
   trap that restores the backed-up files and, when the broker was stopped, the
   SQLite state snapshot. Group creation and a live broker database are retained;
   rollback warns rather than replacing state under a running daemon.

3. Install and validate the SSH policy **before** creating members. The installer
   installs the sshd drop-in, runs `sshd -t`, and reloads sshd. The member match
   block is key-only (`AuthenticationMethods publickey`), disables password and
   keyboard-interactive authentication, sets `PermitUserRC no`, disables
   forwarding/tunnels, and points sshd at the root-managed per-member file:

   ```bash
   sshd -T -C user=<member>,host=<host>,addr=<addr> | grep -i '^authorizedkeysfile'
   # expected: /etc/citespec-resource/authorized-keys.d/%u
   ```

   There is deliberately no `~/.ssh/authorized_keys` fallback. Keep the verified
   administrator session open while changing sshd.

4. Create the first administrator (break-glass only; administrators keep a
   normal shell and must not be in `citespec-members`):

   ```bash
   bash deploy/create_user.sh --admin --yes --ssh-key /path/to/admin.pub admin
   ```

   Members are **not** created by hand in the normal flow. Start the low-power
   intake service and generate an invitation now; the applicant can apply while
   the remaining read-only gates are checked:

   ```bash
   systemctl start citespec-resource-intake.service citespec-resource-provision.path
   citespec-resource admin invitations --create --validity-hours 168
   ```

   The intake process binds `127.0.0.1:8765` only. Publish it through the lab's
   existing TLS reverse proxy (with request-size limits and a non-public network
   policy); this repository does not install or configure that proxy. The
   `onboarding.apply_url` must be the externally reachable HTTPS URL.

   Approval happens after the broker starts in step 7. The Broker records the
   frozen decision, writes a fixed-action spool request, and the root executor
   creates the account, central keys, workspace, and quota. The executor reuses
   `create_user.sh` as a privileged helper; `create_user.sh` itself writes member
   keys to the central directory and is reserved for audited break-glass use.
   Do not run `create_user.sh` on an existing member solely to migrate the shell:
   that would leave the central key file absent and lock the member out under the
   new sshd policy.

5. Verify that the installed udev rule denies host GPU access to a test member:

   ```bash
   bash deploy/verify_gpu_permissions.sh --member citespec-alice --expected-gpus 8
   ```

   Omit `--member` to check every effective `citespec-members` account. Use `--json`
   for the Broker gate format. Re-run the check after a reboot and after the
   NVIDIA driver recreates device nodes.

6. Apply per-directory workspace project quotas before enabling active
   allocations or file transfer. The command is a dry run unless `--yes` is
   supplied:

   ```bash
   bash deploy/configure_quotas.sh \
     --workspace-root /data1/citespec-users \
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
   systemctl enable --now citespec-resource-broker
   systemctl enable --now citespec-resource-intake.service citespec-resource-provision.path
   bash deploy/verify_monitor_only.sh --member <approved-member>
   ```

   The verifier checks `ForceCommand`, central `AuthorizedKeysFile`, socket
   permissions, Docker/GPU denials, monitor mode, and the absence of Docker/GPU
   state changes during the snapshot window (default: 10 minutes). It also invokes
   `verify_gpu_permissions.sh` for every visible NVIDIA device. Intake runs as
   the unprivileged `citespec-intake` account with no broker socket or GPU access; the
   provisioning path unit is the only root action triggered by an approval.

   Now review and approve the application, or explicitly import an existing
   Linux account:

   ```bash
   citespec-resource admin requests list
   citespec-resource admin requests show --request-id <id>
   citespec-resource admin requests approve --request-id <id> \
     --expected-version <reviewed-version> --review-note "identity checked"
   citespec-resource admin operations show --operation-id <id>

   # Existing account: manifest-driven; dry-run by default.
   citespec-resource admin members import-legacy --file legacy-members.json
   citespec-resource admin members import-legacy --file legacy-members.json --apply
   ```

   Approval is refused if the intake content changed after preview. Every account
   operation is persisted with stages and is retryable after a crash; a failed
   host projection does not leave the database claiming access. The legacy
   manifest names the exact username, keys, validity, and quota; apply is
   refused while hard conflicts remain, and nothing is adopted because a name
   happens to exist.

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
docker build -f deploy/docker/Dockerfile.base -t citespec-base:2026.10 deploy/docker
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

The installer installs and enables `citespec-resource-logrotate.timer` and
`citespec-resource-disk-check.timer` by default. Use `--no-timers` to leave them
disabled. The log timer invokes the descriptor-relative
`rotate_job_logs.py` helper instead of a logrotate `copytruncate` rule. The disk
timer runs `check_disk.sh` and exits non-zero for warning and critical
thresholds so failures are visible in `systemctl --failed` and the journal.

The broker socket must be owned by `root:citespec-cli` with mode `0660`. Members must
not receive Docker, sudo, GPU device group, `video`, or `render` access. The
container must not mount the host Docker socket.

## 8. File inventory

- `preflight.sh`: read-only host and development-profile preflight.
- `audit_legacy.sh`: read-only legacy root/sudo/SSH/privileged-container audit.
- `install_server.sh`: idempotent installer; supports `--dry-run`, `--yes`,
  `--start`, and `--no-timers`.
- `create_user.sh`: break-glass member/admin account helper; member keys go to
  the root-managed central directory and admin keys stay in the admin home.
- `membership/provision_executor.py`: root, fixed-action spool executor used by
  approved provisioning, key projection, disable/revoke, and inspect-only
  legacy checks.
- `member-shell.sh`: root-owned member login shell installed by the installer.
- `verify_gpu_permissions.sh`: read-only GPU/NVIDIA device permission
  verification; supports `--json` and no-member mode.
- `verify_monitor_only.sh`: read-only monitor-only security acceptance.
- `check_disk.sh`: workspace free-space alert; exits 0, 1, or 2.
- `configure_quotas.sh`: XFS/ext4 project quota setup; dry-run by default.
- `rotate_job_logs.py`: symlink-safe job-log rotation.
- `acceptance_active.py`: destructive active-mode acceptance harness; see
  `docs/acceptance.md`.
- `systemd/citespec-resource-broker.service`: hardened broker unit.
- `systemd/citespec-resource-logrotate.service` and `.timer`: log rotation timer.
- `systemd/citespec-resource-disk-check.service` and `.timer`: disk alert timer.
- `systemd/citespec-resource-intake.service`: unprivileged public application entry;
  owns only its own SQLite store.
- `systemd/citespec-resource-provision.service` and `.path`: root executor triggered
  by fixed JSON requests in the provisioning spool.
- `udev/99-citespec-gpu.rules`: NVIDIA device-node ownership rule.
- `sshd/60-citespec-resource.conf`: member SSH match block with the central
  `AuthorizedKeysFile`; no home authorized_keys fallback.
- `docker/Dockerfile.base`, `docker/sshd_config`, `docker/bootstrap.sh`:
  development container definitions.
- `../lab/members.example.toml`: public member-directory example.
