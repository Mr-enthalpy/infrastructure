# Legacy administrator access revocation

## 1. Purpose and scope

This checklist defines the minimum actions required to revoke every legacy
administrator path before an active-mode deployment. It is intentionally
independent of the broker: an account that can still use `sudo`, the Docker
socket, a privileged container, or the NVIDIA device nodes can bypass the broker
even if its database role is `member`.

Run the read-only commands first and retain their output as evidence. Do not
stop containers, restart services, or modify accounts while training is active.
`deploy/audit_legacy.sh` and `deploy/verify_gpu_permissions.sh` are read-only
gates; they do not replace this checklist.

## 2. Inventory

Run the following commands as root on the server:

```bash
date -Is
hostname
getent group sudo docker udc-admins udc-members udc-cli udc-gpu || true
getent passwd
```

For each account that was previously an administrator:

```bash
user=<legacy-admin>
id "$user"
getent group | awk -F: -v u="$user" '$4 ~ "(^|,)"u"(,|$)" {print $1}'
sudo -l -U "$user" || true
find /home/"$user" /root -maxdepth 3 -name authorized_keys -print -exec ls -l {} \; 2>/dev/null
lastlog -u "$user" 2>/dev/null || true
```

Inventory the host access paths:

```bash
sshd -T | grep -E '^(permitrootlogin|passwordauthentication|pubkeyauthentication|allowgroups|allowusers|forcecommand)'
ls -l /var/run/docker.sock /run/docker.sock 2>/dev/null || true
find /etc/sudoers.d -maxdepth 1 -type f -print -exec visudo -cf {} \; 2>/dev/null
docker ps -a --no-trunc --format '{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}'
docker info --format '{{json .SecurityOptions}}' 2>/dev/null || true
```

Inventory containers and GPU access. Running unmanaged GPU holders must be
stopped. Stopped containers are historical records and must not be treated as
current GPU holders, but they still require an ownership decision.

```bash
bash deploy/audit_legacy.sh --production
docker ps -a --format '{{.ID}}' | while read -r id; do
  docker inspect -f '{{.Name}} privileged={{.HostConfig.Privileged}} pid={{.HostConfig.PidMode}} gpus={{json .HostConfig.DeviceRequests}}' "$id"
done
```

## 3. Revoke Linux and SSH access

Use a second, verified administrator session. Do not close the only known-good
session until the new restrictions have been tested.

- [ ] Remove the account from every administrator group, including
      `udc-admins`, `sudo`, `wheel`, and any legacy administrator group.
- [ ] Remove it from `docker`, `udc-gpu`, `video`, and `render`.
- [ ] Remove `NOPASSWD` and sudoers drop-ins for the account. Validate with
      `visudo -cf /etc/sudoers` and every file in `/etc/sudoers.d/`.
- [ ] Remove the account's `authorized_keys` entries, including keys in
      `/root/.ssh/authorized_keys` if it had root access.
- [ ] Lock the account (`usermod -L`) and expire it (`chage -E0`) if it is no
      longer an approved administrator. Do not merely change the password.
- [ ] Terminate leftover sessions and processes with
      `pkill -KILL -u <legacy-admin>`.
- [ ] Remove the account from SSH allow-lists or add it to a deny-list. For a
      member account, confirm that `ForceCommand` still applies.
- [ ] If the old key or host access may have been compromised, rotate the
      affected SSH keys and update every client's `known_hosts`.
- [ ] Run `sshd -t` before reloading sshd, and keep the verified session open.

The following commands are intentionally explicit; substitute the approved
account names:

```bash
gpasswd -d "$user" udc-admins || true
gpasswd -d "$user" sudo || true
gpasswd -d "$user" docker || true
gpasswd -d "$user" udc-gpu || true
usermod -L "$user"
chage -E0 "$user"
pkill -KILL -u "$user" || true
```

## 4. Revoke Docker and GPU paths

- [ ] Ensure `getent group docker` has no human members. The broker runs as root
      and uses the Docker socket; members must not.
- [ ] Confirm that `/var/run/docker.sock` is not group-readable or
      group-writable by `udc-members` or any legacy administrator group.
- [ ] Ensure `udc-gpu` has no effective members, including primary-group
      membership, and that every `/dev/nvidia*` node, including `nvidia-uvm`,
      `nvidia-uvm-tools`, and `nvidia-modeset`, is `0660 root:udc-gpu` or
      stricter with no extra ACL. Run:
      `bash deploy/verify_gpu_permissions.sh --member <member> --expected-gpus 8`.
      Re-run it after reboot and after reloading the NVIDIA driver. The udev rule
      must match `KERNEL=="nvidia*"`, not only `SUBSYSTEM=="nvidia"`.
- [ ] Stop and remove legacy privileged containers, host-PID containers, and
      containers that mount the host Docker socket. Do not modify a container
      owned by a current allocation without the owner's approval.
- [ ] Stop running unmanaged GPU holders. Stopped containers may remain as
      historical records, but the reason for retaining them must be recorded.
- [ ] Confirm that no legacy process still owns a GPU:
      `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv`.
- [ ] Re-run `bash deploy/audit_legacy.sh --production`. It must report no
      running unmanaged GPU access and no privileged legacy containers. A
      non-empty `/root/.ssh/authorized_keys` is fatal unless the configuration
      explicitly sets `allow_root_ssh_keys = true`.

## 5. Revoke legacy broker and administrator state

Broker authorization uses the Linux `groups.admins` group resolved from
`SO_PEERCRED`; the `users.role` column is an audit and display record. Both must
be revoked.

- [ ] Back up the SQLite database before editing it.
- [ ] Re-resolve the account through a login or CLI request so `ensure_user`
      refreshes the role, or update it explicitly:

  ```bash
  sqlite3 /var/lib/udc-resource/state.db \
    "UPDATE users SET role='member' WHERE username='<legacy-admin>';"
  ```

- [ ] Confirm that `udc-resource admin users --json` no longer shows the account
      as an administrator.
- [ ] Confirm that the account cannot run `udc-resource admin ...` through the
      broker after removal from `udc-admins`.
- [ ] Check audit events for old administrator actions and preserve the evidence.
- [ ] Do not delete active allocations; let expiry or release handle them. If a
      legacy account owns running resources, transfer or release them through
      the documented workflow.

## 6. Verification before active mode

Run all of the following commands and retain the output. Every command must
produce the stated result:

```bash
id <legacy-admin>
getent group docker udc-gpu udc-admins
sudo -l -U <legacy-admin>                 # must fail or show no privileges
su -s /bin/bash <legacy-admin> -c 'docker ps'       # must fail
su -s /bin/bash <legacy-admin> -c 'nvidia-smi -L'   # must fail
su -s /bin/bash <legacy-admin> -c 'test -r /dev/nvidia0 && echo BAD'  # must not print BAD
ssh -o BatchMode=yes <legacy-admin>@<server> true   # must fail
bash deploy/audit_legacy.sh --production
bash deploy/verify_gpu_permissions.sh --member <current-member> --expected-gpus 8
```

Also verify the positive path with a current member account:

```bash
ssh <current-member>@<server> udc-resource list --json
ssh <current-member>@<server> udc-resource apply --gpus 0 --cpus 1 --memory 1 --hours 1 --json
```

Release the test allocation afterward. Re-run the checks after a reboot and after
reloading the NVIDIA driver; device-node permissions and group membership can
otherwise regress without an immediately visible failure.

## 7. Evidence to retain

- inventory output, including group membership and sudoers validation;
- the old account's `id`, `sudo -l`, Docker/GPU denial results, and SSH denial;
- `audit_legacy.sh` and `verify_gpu_permissions.sh` output;
- the list of stopped or removed legacy containers and the reason for retaining
  any container;
- the database backup path and the `users.role` update result;
- timestamp, operator name, and approval reference for every change.
