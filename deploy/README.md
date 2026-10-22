# UDC infrastructure deployment

Deployment scripts for the `infrastructure` repository. They install and verify
the `udc-resource` broker, CLI, SSH restrictions, udev rule, and systemd unit.

**Do not run these files on amax while training is active.** The code is
implemented, but deployment still requires training completion and explicit
administrator approval.

## Order

1. Run `bash deploy/preflight.sh` and `bash deploy/audit_legacy.sh` (both read-only):
   Docker, systemd, NVIDIA runtime, udev, sshd, groups, cgroup, disk,
   `/dev/nvidia*` permissions, legacy root/sudo/SSH/privileged-container access.
2. Install Python 3.12 (uv or deadsnakes), then run:

   ```bash
   bash deploy/install_server.sh --dry-run --skip-preflight
   bash deploy/install_server.sh --yes
   ```

   The installer is idempotent. It refuses to modify the host without `--yes`,
   does not overwrite an existing config unless `--force-config`, does not start
   the broker unless `--start`, and always forces the persisted mode to
   `monitor`.
3. Create member accounts with:

   ```bash
   bash deploy/create_user.sh --yes --ssh-key /path/to/key.pub udc-alice
   ```

   Members get `udc-members` + `udc-cli` and SSH `ForceCommand`. Admins use
   `--admin`, keep a normal shell, and must not be in `udc-members`.
4. Install the udev rule (done by the installer) and verify host GPU access is
   denied to a test member.
5. Install the systemd unit with `policy.mode = "monitor"` (done by the
   installer).
6. Install the sshd drop-in, run `sshd -t`, then reload. Keep an admin SSH
   session open while changing SSH.
7. Start in monitor-only mode:

   ```bash
   systemctl start udc-resource-broker
   bash deploy/verify_monitor_only.sh --member udc-alice
   ```

   The verifier is read-only: it checks ForceCommand, socket permissions,
   Docker/GPU denials, monitor mode, and that Docker/GPU state is unchanged over
   the snapshot window (default 10 minutes).
8. Only after explicit approval, switch to active mode and run a CPU then
   single-GPU acceptance allocation.

## Files

- `preflight.sh` (read-only checks; run before installing anything)
- `audit_legacy.sh` (read-only legacy root/sudo/SSH/privileged-container audit)
- `install_server.sh` (idempotent installer; `--dry-run`, `--yes`, `--start`)
- `create_user.sh` (member/admin account and optional SSH key setup)
- `verify_monitor_only.sh` (read-only monitor-only security acceptance)
- `acceptance_active.py` (destructive active-mode acceptance; see `docs/acceptance.md`)
- `systemd/udc-resource-broker.service`
- `udev/99-udc-gpu.rules`
- `sshd/60-udc-resource.conf`
- `docker/Dockerfile.base`

The broker socket must be `root:udc-cli 0660`; members must not get Docker,
sudo, or GPU device groups. The container must not mount the host Docker socket.
`install_server.sh` never creates or stops containers.
