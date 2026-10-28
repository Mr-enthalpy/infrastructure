# Active-mode acceptance

## 1. Purpose and authorization

This procedure verifies the security and functional boundaries of the deployed
resource broker. It changes host and database state and is therefore restricted
to a dedicated test allocation and an approved maintenance window.

The procedure must not be executed on any host while training containers or
other users' allocations are present. On example-gpu, it additionally requires training
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

Section 6 is the real research-agent end-to-end phase. It does not block the
first monitor-mode installation, but it must pass before the service is offered
to additional lab members.

## 2. Preconditions and execution context

Run the harness as root on the server. Root's SSH configuration must be able to
log in as the two member accounts.

```bash
cd /path/to/infrastructure
/opt/citespec-resource/.venv/bin/python deploy/acceptance_active.py \
  --member-a citespec-alice \
  --member-b citespec-bob \
  --member-a-user citespec-alice \
  --member-b-user citespec-bob \
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

The following checks must pass before authorizing active mode. They must be
retained as evidence. They are read-only: `preflight.sh` does not invoke
`nvidia-smi` by default, because on an unwrapped host that probe can reset
`/dev/nvidia*` to `0666 root:root`. Pass `--probe-nvidia` only after the wrapper
is installed; the script then compares device mode/owner before and after.
`--skip-nvidia` is the explicit default and also skips the compute-process
query. The installer runs the NVIDIA-only probe itself after wrapper and
permission setup.

- `preflight.sh --config <config>` reports the selected container profile as
  valid.
- For the development profile, Docker uses `overlay2`; DockerRootDir is on XFS
  with `prjquota` or `pquota`; `xfs_quota` is available; workspace project quotas
  are enabled; and the workspace filesystem is mounted `nosuid`.
- Confirm the `nosuid` mount directly with
  `findmnt -no OPTIONS -T "$(python -c 'from citespec_resource.config import load_config; print(load_config("<config>").server.workspace_root)')"`.
  Without a user namespace, container root is host root; a setuid file written through the workspace bind must not
  be honored by the host.
- The GPU audit prerequisites `stat`, `su`, `getent`, `udevadm`, and `getfacl`
  are installed.
- If a Portal/apply vhost is installed, `verify_portal_network.sh` reports a
  specific private `listen` address, `allow <vpn-source-cidr>; ... deny all;`,
  and a loopback-only upstream. A wildcard listener or missing allow rule is a
  blocking failure.
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

- With `vscode_enabled = true`, run `citespec-resource vscode <allocation-id>`.
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
the host `citespec-gpu` GID inside the container. Docker's `group_add` only applies
to docker-exec sessions; an sshd login rebuilds supplementary groups from the
container's `/etc/group`, so the VS Code path is the regression risk. Verify
both paths separately, inside the same test allocation:

- Via `citespec-resource shell <allocation-id>` (docker-exec path):
  1. `id -G` must list the host `citespec-gpu` GID.
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

Procedure (one allocated test GPU, one test member, one non-allocated bystander):

1. The installer places the test at
   `/usr/local/libexec/citespec-resource/verify_gpu_isolation.sh` (source:
   `deploy/verify_gpu_isolation.sh`). Run it on the host during the approved
   window, against a running development-profile container:

   ```bash
   sudo /usr/local/libexec/citespec-resource/verify_gpu_isolation.sh \
     --confirm --gpu <allocated-index> \
     --container <container-id-or-name> \
     --member <test-member> --bystander <other-member> \
     --write-marker
   ```

   The script records `/dev/nvidia*` inode, mode, and owner, then runs
   `stat`, `chmod 0666`, and `chown 1000:1000` as the member through container
   sudo on the allocated node only. It diffs the host state, restores the
   original mode/owner if it changed, checks that the bystander cannot read or
   write the node, and writes evidence under
   `/var/log/citespec-resource/gpu-isolation-<UTC>/`.

2. PASS requires: no inode/mode/owner change on any host device, both
   `chmod` and `chown` refused (or accepted without host effect), and the
   bystander denied read and write. Only a full PASS with `--bystander` may
   write `/etc/citespec-resource/gpu-isolation.ok`.

3. `[policy].mode = "active"` with `container_profile = "development"` and
   `container_sudo = true` is refused by config loading, the broker, and
   preflight until that marker exists. The marker records the GPU, device,
   container, members, and evidence path; it is not a substitute for removing
   the marker after any later kernel/driver/user-namespace change.

If the test fails, do not write the marker and do not open the development
profile. Prefer implementing user-namespace remapping and re-running the test;
if that is not acceptable, disable the development profile and container sudo
before member rollout. The periodic GPU audit is a compensating
detection-and-admission control only — it cannot protect tasks that are already
running on an affected node. Absence of host mounts, the Docker socket, and
workspace `nosuid` remain load-bearing requirements regardless.

To revoke after a passing test, remove the marker and reload the broker:

```bash
sudo rm -f /etc/citespec-resource/gpu-isolation.ok
sudo systemctl restart citespec-resource-broker.service
```


### 5.7 Closed gates

- File transfer must remain disabled outside the controlled acceptance run until
  TOCTOU-safe handling and quota tests pass.
- A failed GPU audit, workspace quota check, or storage preflight must block new
  allocations while existing tasks continue and remain releasable.
- The browser-facing Portal and apply intake stay loopback-only and closed to
  members until the proxy trust boundary is no longer "source address is
  loopback". A local host user can connect directly to `127.0.0.1:8770`/`8765`
  and spoof `X-Citespec-Client-IP`, which affects audit attribution and
  IP-based rate limits. Before exposing either vhost, move the upstream to a
  permission-restricted Unix socket or add an equivalent verifiable proxy
  identity; the nginx `$remote_addr` overwrite alone is not sufficient.

### 5.8 Member onboarding, central keys, and browser intake

These checks run on a disposable Linux host or on the controlled live instance
with one-time accounts and full cleanup. Do not run them while other users'
allocations or training containers are present. Record which environment was
used and retain the evidence. Core active acceptance has been run on the live
instance; external TLS onboarding and optional workstation paths remain
separate checks.

1. **Central-key SSH policy.** Install the drop-in on the disposable host and run
   `sshd -T -C user=<member>,host=<host>,addr=<addr>`. Record that
   `authorizedkeysfile` is exactly
   `/etc/citespec-resource/authorized-keys.d/%u`. Add a key only to the member's home
   `~/.ssh/authorized_keys`; confirm public-key login still fails. Add the same
   key through approval/key-add; confirm login succeeds and a broker operation
   runs. Revoke the key; confirm new SSH connections fail even though the member
   can still edit the home directory.
2. **Approval and provisioning.** Create an invitation, submit through the real
   HTML form and through `citespec-resource join --portal` (including a machine with
   no local server config), approve with the exact `--expected-version`, and
   record the account UID, groups, shell, central key file, workspace, and quota
   read back from the host. Attempt a stale-version approval and confirm it is
   refused. Interrupt the executor mid-operation and retry; confirm the final
   state is either fully active or visibly failed, never half-claimed.
3. **Pre-existing accounts.** Run `admin members import-legacy` dry-run against
   an unrelated account and confirm a conflict; confirm nothing is adopted by
   name. Run apply with a reviewed manifest and verify the shell/groups/keys are
   converged. Do not adopt a privileged or system account.
4. **Bounded intake.** Submit oversized fields, private-key material, duplicate
   idempotency keys, and rapid repeated requests. Confirm friendly rejection,
   rate limiting, no secret in URL/logs, POST-only receipt status, and that one
   invitation cannot mint unlimited applications. Verify the intake account
   cannot read the broker socket, member database, Docker socket, or GPU nodes.
5. **Browser and workstation UX.** Publish the loopback intake service through
   the lab TLS reverse proxy and verify request-size, rate, and network policy.
   Do not treat "the peer is loopback" as proxy authentication; complete the
   §5.7 Unix-socket/verifiable-proxy prerequisite before exposing the vhost.
   In a real browser, complete the application form, confirm receipt is shown
   once, then query status with only the receipt secret. The repository includes
   `tests/test_intake_browser.py` for this path:

   ```bash
   python -m pip install -e ".[browser]"
   python -m playwright install chromium
   python -m pytest tests/test_intake_browser.py -q
   ```

   Playwright skips are recorded as SKIP and do not provide browser evidence.
   On a clean workstation profile, run
   `setup --server ... --user ...`; confirm the SHA256 fingerprint is shown and
   not trusted before confirmation. Also run the same command with
   `--fingerprint SHA256:<wrong>` and confirm it fails without writing
   known_hosts. Confirm existing `~/.ssh/config` is preserved, the managed
   fragment is included once,
   and the generated `~/.config/citespec-resource/client.toml` contains connection
   data only. Confirm a wrong host key fails closed. Then run `me`, `start`,
   `queue`, and `gui`; capture screenshots/output.
6. **Lifecycle and revocation.** Suspend/resume/revoke through the GUI; confirm
   the member's queued requests and broker streams/tunnels close, existing
   allocations follow the documented policy, key files are removed on disable,
   and re-enabling projects the current active key set. Record operation retries
   and audit events.

Mark this section PASS only with captured host output and browser evidence. For
the live instance, use one-time accounts and remove them after the test. A core
PASS does not cover external HTTPS onboarding, VS Code, file transfer, or the
container-root/host-GPU 5.6 gate.

## 6. Real research-agent end-to-end acceptance (C-4)

### 6.1 Purpose

This phase verifies that the deployed system supports real research work, not
only correct allocation bookkeeping. An independent research agent must complete
a real, scoped-down training task through the supported member entry points and
report its experience without being told the expected administrator answers.

It does not block the first monitor-mode installation. It must pass before the
service is offered to additional lab members. It complements, and does not
replace, Sections 4, 5, and 5.8.

### 6.2 Preconditions

- Sections 3 and 4 pass on the same host, and the member-facing entry points
  (central SSH keys, CLI, broker socket, workspace) are in their accepted state.
- One dedicated ordinary member identity, created or enabled by the
  administrator before the test. The agent receives only that member's own key
  pair (registered through the supported `admin members key-add` flow), its
  connection details, the project description, and
  `docs/guides/member-quickstart.md`. It must not receive broker internals, the
  state database, administrator instructions, recovery runbooks, or any
  administrator credential.
- The identity has no host sudo, no Docker group, no `citespec-admins`, no
  `citespec-gpu` membership, and no interactive host shell.
- An approved GPU count, maximum wall time, and storage budget that cannot
  affect other research tasks; no other allocation on the selected GPU.
- development profile, container sudo, and VS Code are not enabled for this test
  unless their gates (Sections 5.1-5.6) have passed. No temporary bypass is
  permitted.

### 6.3 Task

Choose a real training task from an existing research project and scale the
data, epochs, or model so it completes inside the approved lease. It must use
the project's own code and a representative data path, and must produce actual
parameter updates, training logs, model checkpoints, and interpretable
evaluation metrics. `nvidia-smi`, a CUDA smoke test, or an empty loop is not a
completed task.

### 6.4 Procedure

1. The agent reads the member quickstart and establishes the member connection
   without administrator help.
2. The agent obtains one GPU through the normal apply/queue path and confirms
   which resources it owns.
3. The agent places code and data in its own workspace and prepares Python and
   other dependencies through the currently supported path.
4. The agent starts training in the authorized container and observes logs, GPU
   use, and storage use.
5. During training, the agent disconnects and reconnects at least one SSH or
   agent session, and confirms the task does not end because the client
   disconnected.
6. The agent completes the predefined target, saves checkpoints, metrics, and
   logs, and confirms the results are still in the persistent workspace.
7. The agent releases the resources and states which environment changes died
   with the container and which results were retained.
8. The agent submits an independent experience report (Section 6.6) without
   knowing the administrator's expected answers.

### 6.5 Pass criteria

| Dimension | Requirement |
|---|---|
| Identity isolation | The whole run uses the ordinary member identity; no administrator credential and no host privilege |
| Resource admission | The agent gets the authorized GPU and cannot use an unallocated device |
| Environment setup | Dependencies can be prepared through the documented supported path |
| Real training | The predefined task completes with credible checkpoints, logs, and metrics |
| Disconnect recovery | A client disconnect does not end work that is expected to keep running |
| Persistence | Research results remain in the workspace; the disposable environment behaves as documented |
| Release | Resources are reclaimed at the end; no abnormal leftover use |
| Privilege boundary | No host administrative access was obtained while solving experience problems |

### 6.6 Agent experience report

The agent records:

| Observation | What to report |
|---|---|
| Time to first start | Time from reading the guide to the first real training step |
| Friction | Steps that were unclear, repetitive, or easy to get wrong |
| Environment management | Limits hit while installing system packages, configuring Python, or adjusting dependencies |
| CLI/GUI | What was convenient; which prompts lacked a next step |
| Failure handling | Which errors occurred and whether the agent could diagnose and resolve them alone |
| External help | How often and why an administrator or another person had to intervene |
| Agent automation fit | Commands that script well; flows that force interactive input |
| Verdict | Whether the agent would keep using it for daily research; the top three improvements |

Retain the key commands, redacted error output, the operation timeline, and the
training artifacts.

### 6.7 Verdicts

- **Core pass**: the real training run completes, permissions and resource state
  are correct, and no unexpected data is lost.
- **Core failure**: the documented member workflow cannot complete, resource
  isolation is broken, the task stops without cause, or results are lost. Fix
  before rollout.
- **Experience issue**: training completes but the workflow is cumbersome,
  errors are unclear, dependency setup is painful, or repeated human help was
  needed. Add to the UX backlog.
- **Feature not yet open**: the run is limited by development profile, container
  sudo, or VS Code gates that have not passed. Record it as a Section 5 priority;
  it is not a blocker for the base deployment.
- **Project-side issue**: model, data, or training-configuration problems,
  recorded separately from resource-management problems.

### 6.8 Boundaries

- The test identity, SSH key, and resources are administered by the
  administrator. The agent must not solve problems by using administrator
  access.
- Keep the workspace, logs, and checkpoints for review after the test; cleanup is
  confirmed by the administrator.
- A core pass is host-specific. Re-run after storage, GPU driver, container
  profile, or member entry-point changes.

## 7. Evidence to retain

Retain the complete command output and the following artifacts:

- member `list --json` output before and after the test;
- `systemctl status citespec-resource-broker` and relevant journal excerpts;
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
- `sshd -T` central `AuthorizedKeysFile` output and proof that a home-only key
  does not authenticate;
- intake/provision unit status, provisioning operation stages, and the
  approved request version/ID;
- browser screenshot/output for application, receipt, approval, and setup
  fingerprint confirmation; the generated workstation profile with secrets
  redacted;
- for Section 6, the agent's command log, redacted errors, operation timeline,
  experience report, and the resulting checkpoints, metrics, and training logs;
- operator name, timestamps, and approval reference for every state change.

## 8. Abort and recovery criteria

Abort the procedure immediately and preserve logs if any of the following
occurs:

- an unmanaged container is started, stopped, removed, or otherwise modified;
- a GPU is allocated to more than one allocation;
- a non-owner can release or stop another user's allocation;
- another user's container or workspace is affected;
- a queue request jumps ahead of an unexpired queued request;
- a security gate fails open;
- the broker cannot be stopped or restarted cleanly;
- a real training run loses checkpoints or logs that should have persisted in
  the workspace;
- the agent obtained host administrative access while working around an
  experience problem.

After aborting, release only the test allocations created by the harness, remove
only the harness-created temporary container, and leave existing training
containers and unmanaged resources untouched. Record the state of the database
and the test workspace before and after recovery.
