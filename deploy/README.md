# Citespec infrastructure deployment guide

## 1. Scope and safety requirements

This directory contains the deployment and verification tooling for the
`citespec-resource` broker, CLI, SSH policy, udev rule, systemd units, development
container image, VPN-only membership portal, and host-side verification scripts.

Deployment is prohibited while the host is serving training workloads or when
explicit administrator approval has not been recorded. The current project
status, target-host unknowns, and approved next steps are maintained in
[`docs/handoff.md`](../docs/handoff.md). The acceptance procedure is defined in
[`docs/acceptance.md`](../docs/acceptance.md).

This round implements the intake, approval, executor, central-key SSH policy,
setup/client-profile paths, and a VPN-only membership portal in code and
automated tests. Core active-mode acceptance has been run on the live instance,
including one-time member provisioning and cleanup. External HTTPS onboarding,
the portal network/certificate changes, and the optional VS Code, file-transfer,
and container-root/host-GPU 5.6 gates remain pending; see
[`docs/handoff.md`](../docs/handoff.md) and
[`docs/server-network-request.md`](../docs/server-network-request.md) for current
status and evidence. The real research-agent end-to-end phase (C-4) in
[`docs/acceptance.md`](../docs/acceptance.md) §6 is not required for the first
monitor-mode installation, but must pass before offering the service to
additional lab members.

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
- `server.workspace_root` may differ from the static amax path. The installer
  generates `10-workspace.conf` drop-ins for the broker, log-rotation,
  disk-check, and provisioning units so every sandbox path list, `ExecStart`,
  and the provisioning `CITESPEC_RESOURCE_WORKSPACE_ROOT` use the configured
  root. Stale drop-ins are removed when the configured root matches the static
  path again.
- The read-only checks in Section 3 pass. For the development profile, the
  storage, quota, and `nosuid` requirements in Section 5 are mandatory.
- An existing `citespec-portal` account is reused only when it is a system
  account (UID 1-999) with the `citespec-portal` primary group, a nologin/false
  shell, the portal state directory as home, a locked password, no sudo rule,
  and no supplementary groups other than `citespec-cli`. Anything else stops the
  installation before any group or account is changed.
- For the portal, the internal DNS, campus VPN route/DNS, TLS certificate, and
  443 firewall answers in
  [`docs/server-network-request.md`](../docs/server-network-request.md) are
  recorded and accepted. Keep `[portal].enabled = false` until then.

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
read-only: `preflight.sh` does not invoke `nvidia-smi` by default, because as root
it resets `/dev/nvidia*` to `0666 root:root` unless the Citespec wrapper is
installed. Pass `--probe-nvidia` only after the wrapper exists; the script then
records device mode/owner before and after and fails if the probe changes them.
`--skip-nvidia` is the explicit form of the default. The installer runs
`preflight.sh --probe-nvidia --nvidia-only` itself after installing the wrapper
and applying the GPU permission policy.

After installing a Portal/apply vhost, run the read-only network audit:

```bash
bash deploy/verify_portal_network.sh --nginx-root /etc/nginx
```

It fails on a wildcard `listen` address, a vhost without
`allow <vpn-source-cidr>; ... deny all;`, a non-loopback upstream, or a
client-supplied `X-Citespec-Client-IP`. It warns when the vhosts or Portal unit
are not installed yet.

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
   Python package with the `gui` and `portal` extras, creates the root-managed key
   directory `/etc/citespec-resource/authorized-keys.d`, the provisioning spool
   `/var/lib/citespec-resource/provision`, the isolated intake state directory,
   and the `citespec-portal` account/group, state directory, SQLite database,
   root-owned TOTP key, and systemd unit. A fresh database starts in `monitor`
   mode. A reinstall with active/pending leases preserves `active` or
   `maintenance` so lease expiry is not silently paused.

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
     --workspace-root /home/data1/citespec-users \
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

8. Prepare the VPN-only portal only after the network answers are accepted. The
   installer creates the low-privilege `citespec-portal` account (member of
   `citespec-cli`, never `citespec-admins`), its state directory, `portal.db`, the
   root-owned `/etc/citespec-resource/portal-secret.key` (mode `0640`), and
   `citespec-resource-portal.service`. It never enables or starts the unit, even
   when `[portal].enabled = true`; the unit also has
   `ConditionPathExists=/etc/citespec-resource/portal-network-accepted`.

   The portal binds `127.0.0.1:8770`; host nginx terminates TLS on the private
   campus/VPN listener. Example vhosts live in `deploy/nginx/` — they are not
   installed automatically and contain no certificate, DNS, or firewall changes.
   Confirm the certificate covers the chosen names and that nginx overwrites
   `X-Citespec-Client-IP` with `$remote_addr`, then run
   `bash deploy/verify_portal_network.sh --nginx-root /etc/nginx` and require a
   zero exit before exposing the vhost. Then, after a local activation and
   admin review test, enable the unit through the only supported gate:

   ```bash
   bash deploy/enable_portal.sh --yes --approved-cidr <vpn-source-cidr>
   curl -fsS http://127.0.0.1:8770/healthz
   systemctl status citespec-resource-portal.service
   ```

   `enable_portal.sh` runs the fail-closed acceptance audit, writes
   `/etc/citespec-resource/portal-network-accepted` only on success, and then
   enables/starts the unit. A failed re-run rolls back only the state it added.

   Do not expose the vhost while the upstream trusts `X-Citespec-Client-IP`
   solely because the peer is loopback: any local host user can connect to
   `127.0.0.1:8770` directly and spoof audit/rate-limit attribution. Complete
   the `docs/acceptance.md` §5.7 Unix-socket (or verifiable-proxy) prerequisite
   before opening the browser entry.

   Portal scope is deliberately narrow: registration, activation, local
   password/TOTP login, own member/status view, and admin approval/member-state
   management. It has no resource apply/release/queue, shell, VS Code, or file
   transfer. Browser routes are tested with `pytest tests/test_portal.py`; the
   broker allowlist is tested in `tests/test_broker.py`. Admin role changes remain
   CLI-only, and approval/suspend/resume/revoke require password + TOTP re-auth.
   A one-time activation code is shown once and expires after 24 hours by default.

   Bootstrap the first portal administrator before handing the URL to members:

   ```bash
   citespec-resource admin portal create --username labadmin --role admin \
     --display-name "Lab Admin" --contact admin@example.invalid
   citespec-resource admin portal list
   ```

   The activation code is printed once; deliver it over a trusted channel. Later
   role changes use `citespec-resource admin portal set-role --username <name>
   --role member|admin` and invalidate that account's browser sessions. The last
   active administrator cannot be demoted. These commands require membership
   in `citespec-admins` and are rechecked by the broker; the portal service account
   cannot call them.

9. Only after explicit approval and completion of the read-only checks may the
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

The installer installs and enables `citespec-resource-logrotate.timer`,
`citespec-resource-disk-check.timer`, and `citespec-resource-backup.timer` by
default. Use `--no-timers` to leave them disabled. The log timer invokes the
descriptor-relative `rotate_job_logs.py` helper instead of a logrotate
`copytruncate` rule. The disk timer runs `check_disk.sh` and exits non-zero for
warning and critical thresholds so failures are visible in
`systemctl --failed` and the journal.

The backup timer runs `python -m citespec_resource.backup backup` once a day.
It uses SQLite's online backup API for `state.db`, the intake database, and
`portal.db` when present, copies the root-owned `/etc/citespec-resource` tree
(including central member keys and the portal secret), writes a SHA-256
manifest, verifies the result, and retains the newest 14 `state-*` backups under
`/var/backups/citespec-resource/`. Workspaces are not included. Restore stops
the portal writer unit as well, and inherits the existing `portal.db` owner so
the low-privilege portal service can reopen it. See `docs/backup-restore.md` for
verification, restore, and restore drill commands.

`citespec-resource-alert@.service` is the common `OnFailure=` target for the
broker, intake, portal, disk, log-rotation, and backup units. It writes the journal and
`/var/log/citespec-resource/alerts.log`; an optional root-owned
`/etc/citespec-resource/alert.env` can set `CITESPEC_ALERT_WEBHOOK` for JSON
webhook delivery.

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
- `verify_portal_network.sh`: read-only nginx/Portal listener, allow/deny, and
  loopback-upstream audit.
- `check_disk.sh`: workspace free-space alert; exits 0, 1, or 2.
- `configure_quotas.sh`: XFS/ext4 project quota setup; dry-run by default.
- `rotate_job_logs.py`: symlink-safe job-log rotation.
- `alert.sh`: `OnFailure=` alert recorder; journal + local log + optional webhook.
- `../src/citespec_resource/backup.py`: state/intake/portal backup, verify,
  restore, and retention implementation used by the backup timer.
- `../src/citespec_resource/portal/`: portal HTTP/UI, local auth/TOTP store, and
  narrow broker bridge.
- `acceptance_active.py`: destructive active-mode acceptance harness; see
  `docs/acceptance.md`.
- `systemd/citespec-resource-broker.service`: hardened broker unit.
- `systemd/citespec-resource-logrotate.service` and `.timer`: log rotation timer.
- `systemd/citespec-resource-disk-check.service` and `.timer`: disk alert timer.
- `systemd/citespec-resource-backup.service` and `.timer`: daily state backup.
- `systemd/citespec-resource-alert@.service`: common failure alert template.
- `systemd/citespec-resource-intake.service`: unprivileged public application entry;
  owns only its own SQLite store.
- `systemd/citespec-resource-portal.service`: unprivileged VPN-only membership
  portal; binds loopback and may use only the broker's narrow portal commands.
- `nginx/citespec-portal.conf.example` and `nginx/citespec-apply.conf.example`:
  example VPN-only vhosts with a private `listen` address,
  `allow <vpn-source-cidr>; ... deny all;`, and a loopback upstream;
  certificates, DNS, and firewall rules are external.
- `systemd/citespec-resource-provision.service` and `.path`: root executor triggered
  by fixed JSON requests in the provisioning spool.
- `udev/99-citespec-gpu.rules`: NVIDIA device-node ownership rule.
- `sshd/60-citespec-resource.conf`: member SSH match block with the central
  `AuthorizedKeysFile`; no home authorized_keys fallback.
- `docker/Dockerfile.base`, `docker/sshd_config`, `docker/bootstrap.sh`:
  development container definitions.
- `../lab/members.example.toml`: public member-directory example.
