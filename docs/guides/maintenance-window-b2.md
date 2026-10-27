# 可逆的 monitor 模式升级维护窗口（B-2）

本文是目标主机的维护窗口操作单，不是自动化脚本。所有命令在目标主机上以 root
执行；`<...>` 必须在窗口前替换为实际值。不要把真实地址、SSH 端口、主机别名、
管理员身份、密钥或凭据写入本文件或提交到 Git。

## 1. 边界与原则

- 只升级 Citespec 到 `fix/p1-pre-deploy` 的构建；**不启用 Portal、不切换 active、
  不改 nginx/DNS/VPN/防火墙、不操作无关容器**。
- 升级后保持 `policy.mode = "monitor"`、`deployment_allowed = false`、
  `[portal].enabled = false`。
- 安装使用 `--no-timers` 和 `--maintenance-window`，且不传 `--start`；timer 和
  broker 由操作者显式控制。
- 安装器把新版本装到 `venvs/venv-<version>-<timestamp>-<pid>`，通过 smoke test 后
  原子切换 `.venv`，旧环境保留；在 `--maintenance-window` 下先停止写入者，再用
  `backup.py` 生成一致快照。失败时 trap 会恢复旧 venv 和快照。
- 目标机没有 LVM/文件系统快照；本文的“快照”是**文件级备份**，因此必须先做
  rollback rehearsal，且备份必须校验。
- 安装失败时让安装器的 `EXIT/HUP/INT/TERM` trap 完成回滚；不要 `kill -9` 安装器。
  只有“安装成功但验收失败”才执行本文第 8 节的手动回滚。
- 容器策略：默认不碰任何容器；所有 exited 容器不得启动、停止或删除。仅当某个
  running 容器确实阻塞升级且经批准时，才可 `docker stop` 那一个容器；**不得
  `docker rm`**。需要停止该容器时只 stop，不 delete，并记录其完整 ID 和原状态。

## 2. 窗口前检查（只读）

- [ ] 训练已结束或确认不在影响范围；实验室负责人批准窗口。
- [ ] 保留一个已验证的管理员 SSH 会话，另开第二个会话用于观察；不要在窗口内关闭
      两者。
- [ ] 目标机根分区和 `<WINDOW_BACKUP_ROOT>` 所在文件系统有足够空间：
      至少 `2 × /opt/citespec-resource` + 所有 SQLite 库 + 100 MiB 余量。不要把备份
      写到空间紧张的根分区。
- [ ] 记录当前容器基线，不修改容器：

      ```bash
      install -d -m 0700 /root/citespec-window-precheck
      docker ps -a --no-trunc \
        --format '{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}' \
        > /root/citespec-window-precheck/containers.tsv
      docker inspect --format '{{.Id}} {{.State.Status}} {{.State.StartedAt}}' \
        $(docker ps -aq) > /root/citespec-window-precheck/containers-inspect.txt
      ```

- [ ] 记录 service/timer 的 active/enabled 状态、包版本、配置和 DB schema：

      ```bash
      systemctl list-units --all 'citespec-*' --no-pager \
        > /root/citespec-window-precheck/units.txt
      systemctl list-timers --all 'citespec-*' --no-pager \
        > /root/citespec-window-precheck/timers.txt
      /opt/citespec-resource/.venv/bin/pip freeze \
        > /root/citespec-window-precheck/pip-freeze.txt
      /opt/citespec-resource/.venv/bin/citespec-resource --version \
        > /root/citespec-window-precheck/version.txt
      ```

- [ ] 确认当前没有训练进程占用要使用的 GPU。**不要用 `nvidia-smi` 做这一步**：
      未安装 wrapper 时它会重置 `/dev/nvidia*`。使用 `stat`/`getfacl` 记录设备状态，
      并从现有 broker/作业记录确认；如果必须查 compute process，安排在安装 wrapper
      之后，或用已批准的只读方式。

- [ ] 确认 Portal/nginx 当前未承载生产流量；本窗口不修改 nginx。若已有 vhost，
      只运行只读审计：

      ```bash
      bash deploy/verify_portal_network.sh --nginx-root /etc/nginx
      ```

## 3. 建立文件级备份

> 安装器在 `--maintenance-window` 下会再取一次一致快照，默认写到
> `/var/backups/citespec-resource/snapshots/`，并校验 `manifest.json`。本节的手动
> 备份仍应执行：它提供独立的整机恢复点和窗口证据，不依赖安装器成功。

```bash
set -euo pipefail
umask 077

WINDOW=$(date -u +%Y%m%dT%H%M%SZ)
B="<WINDOW_BACKUP_ROOT>/$WINDOW"
CFG=/etc/citespec-resource/config.toml
VENV=/opt/citespec-resource/.venv
STATE=/var/lib/citespec-resource/state.db

install -d -m 0700 "$B" "$B/db" "$B/meta"
```

### 3.1 停止 Citespec 服务（不动其他服务）

先记录再停止；不要 `disable` 旧 unit，回滚需要恢复原状态。

```bash
systemctl stop citespec-resource-logrotate.timer \
  citespec-resource-disk-check.timer citespec-resource-backup.timer 2>/dev/null || true
systemctl stop citespec-resource-broker.service \
  citespec-resource-intake.service citespec-resource-provision.path 2>/dev/null || true
```

为了让 `--no-timers` 窗口内即使重启也不会自动触发，先记录 timer 原状态；**不要
在安装前 disable**，否则安装器捕获到的“原状态”会变成 disabled，失败回滚就无法恢复
旧状态。成功验收后再在启动步骤里 disable。

```bash
for t in citespec-resource-logrotate.timer \
         citespec-resource-disk-check.timer citespec-resource-backup.timer; do
  if systemctl is-enabled --quiet "$t" 2>/dev/null; then
    echo "$t enabled" >> "$B/meta/timer-enabled.txt"
  fi
done
systemctl is-active citespec-resource-broker.service \
  citespec-resource-intake.service citespec-resource-provision.path || true
pgrep -af 'citespec-resource' || true
```

如果仍有 Citespec 进程或 `/run/citespec-resource/broker.sock`，等待退出；不要在
服务仍写入时复制数据库。

### 3.2 手动 SQLite `.backup` + checkpoint

对 `state.db`、intake DB、以及存在时的 `portal.db` 各做一次。checkpoint 必须是
`TRUNCATE`，且 busy 时中止整个备份。

```bash
INTAKE=$("$VENV/bin/python" - "$CFG" <<'PY'
import sys
from citespec_resource.config import load_config
print(load_config(sys.argv[1]).onboarding.intake_db)
PY
) || { echo "cannot resolve intake DB"; exit 1; }

PORTAL=/var/lib/citespec-resource/portal.db

backup_db() {
  src=$1
  dst=$2
  [ -f "$src" ] || { echo "skip missing DB: $src"; return 0; }
  rm -f "$dst" "$dst-wal" "$dst-shm"
  "$VENV/bin/python" - "$src" "$dst" <<'PY'
import sqlite3
import sys

src, dst = sys.argv[1:3]
source = sqlite3.connect(src)
try:
    row = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row and row[0] != 0:
        raise SystemExit(f"checkpoint busy for {src}: {row}")
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        target.close()
finally:
    source.close()
print(f"backed up {src} -> {dst}")
PY
}

backup_db "$STATE" "$B/db/state.db"
backup_db "$INTAKE" "$B/db/intake.db"
backup_db "$PORTAL" "$B/db/portal.db"
```

记录每个 DB 的 owner/mode，回滚时恢复：

```bash
stat -c '%n %U:%G %a' "$STATE" "$INTAKE" "$PORTAL" 2>/dev/null \
  > "$B/meta/db-owners.txt" || true
```

### 3.3 备份安装前缀、配置、unit、NVIDIA wrapper 和 SSH/udev

```bash
cp -a /opt/citespec-resource "$B/opt-citespec-resource"
cp -a /var/lib/citespec-resource/provision "$B/provision" 2>/dev/null || true

tar --ignore-failed-read -C / -cpf "$B/system-state.tar" \
  etc/citespec-resource \
  etc/systemd/system/citespec-resource-broker.service \
  etc/systemd/system/citespec-resource-intake.service \
  etc/systemd/system/citespec-resource-provision.path \
  etc/systemd/system/citespec-resource-provision.service \
  etc/systemd/system/citespec-resource-portal.service \
  etc/systemd/system/citespec-gpu-permissions.service \
  etc/systemd/system/citespec-resource-logrotate.service \
  etc/systemd/system/citespec-resource-logrotate.timer \
  etc/systemd/system/citespec-resource-disk-check.service \
  etc/systemd/system/citespec-resource-disk-check.timer \
  etc/systemd/system/citespec-resource-backup.service \
  etc/systemd/system/citespec-resource-backup.timer \
  etc/systemd/system/citespec-resource-alert@.service \
  etc/udev/rules.d/99-citespec-gpu.rules \
  etc/ssh/sshd_config.d/60-citespec-resource.conf \
  etc/nginx \
  usr/bin/nvidia-smi \
  usr/bin/nvidia-smi.real \
  usr/bin/nvidia-modprobe \
  usr/bin/nvidia-modprobe.real \
  usr/local/lib/citespec-resource \
  usr/local/libexec/citespec-resource
```

记录 NVIDIA 路径和本次可能新增的 Citespec 路径是否原本存在，回滚时据此删除新文件：

```bash
for p in /usr/bin/nvidia-smi /usr/bin/nvidia-smi.real \
         /usr/bin/nvidia-modprobe /usr/bin/nvidia-modprobe.real \
         /usr/local/lib/citespec-resource/libcitespec-nvidia-chmod.so \
         /usr/local/lib/citespec-resource /usr/local/libexec/citespec-resource \
         /etc/citespec-resource/portal-secret.key \
         /etc/citespec-resource/portal-network-accepted \
         /etc/citespec-resource/gpu-isolation.ok \
         /var/lib/citespec-resource/portal.db \
         /etc/systemd/system/citespec-resource-portal.service; do
  if [ -e "$p" ]; then stat -c '%n exists %a %U:%G' "$p"; else echo "$p absent"; fi
done > "$B/meta/pre-paths.txt"

getent passwd citespec-portal > "$B/meta/portal-account.txt" 2>/dev/null || true
getent group citespec-portal >> "$B/meta/portal-account.txt" 2>/dev/null || true
```

### 3.4 生成校验和并验证备份可读

```bash
( cd "$B" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) \
  > "$B/SHA256SUMS"
tar -tf "$B/system-state.tar" > "$B/meta/tar-list.txt"
( cd "$B" && sha256sum -c SHA256SUMS --quiet )
```

`sqlite3` 的 `integrity_check` 已在 3.2 的 Python 脚本中执行；再确认备份文件不是
空文件，并保留输出作为证据。

## 4. Rollback rehearsal（变更前）

目标机没有快照，因此在一次性 VM 或临时目录做恢复演练；不要在目标机覆盖在线
数据库。

1. 在一次性 VM 上解包 `system-state.tar`，把 `db/state.db` 放入临时路径，运行：

   ```bash
   sqlite3 <tmp>/state.db 'PRAGMA integrity_check;'
   sqlite3 <tmp>/state.db 'PRAGMA user_version;'
   systemd-analyze verify <tmp>/etc/systemd/system/citespec-resource-*.service
   bash -n /path/to/repo/deploy/install_server.sh
   ```

2. 用备份的 `opt-citespec-resource` 验证旧 CLI 可启动：

   ```bash
   <tmp>/opt-citespec-resource/.venv/bin/citespec-resource --version
   ```

3. 在目标机只做**只读**校验：`tar -tf`、`sha256sum -c`、`sqlite3` 对备份文件做
   `PRAGMA quick_check`（打开副本，不碰在线库）。
4. 记录一次“如果现在回滚”的预计耗时；若无法在窗口内完成，取消窗口。

## 5. 准备 monitor 配置

复制当前配置并只修改 `[policy]` 的两项：

- `mode = "monitor"`
- `deployment_allowed = false`
- `[portal].enabled = false`

不要用全局 `sed` 盲改。复制后人工编辑，并验证：

```bash
cp -a "$CFG" "$B/config.monitor.toml"
# 人工编辑 $B/config.monitor.toml
"$VENV/bin/python" - "$B/config.monitor.toml" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)
assert data["policy"]["mode"] == "monitor", data["policy"]["mode"]
assert data["policy"]["deployment_allowed"] is False
assert data.get("portal", {}).get("enabled", False) is False
print("monitor config verified")
PY
```

若当前 `config.toml` 与 monitor 配置只有上述差异，也把原配置保留在备份中；回滚时
直接恢复原文件。

## 6. 升级命令

先手动跑 preflight。默认不调用 `nvidia-smi`，因为 wrapper 尚未安装：

```bash
cd <repo-root>
bash deploy/preflight.sh --config "$B/config.monitor.toml"
```

记录任何 failure；有 failure 就停止，不要继续安装。安装器本身仍会运行
`audit_legacy.sh --production`：monitor 模式下 critical findings 只告警、不阻断安装，
但必须记录并交由管理员决定；不得因此把配置切到 active。然后 dry-run：

```bash
bash deploy/install_server.sh \
  --dry-run --skip-preflight --no-timers --maintenance-window \
  --config-source "$B/config.monitor.toml" --force-config
```

确认 dry-run 输出的 config、workspace、unit、版本化 venv 和“不启用 timer”
符合预期后执行：

```bash
bash deploy/install_server.sh \
  --yes --skip-preflight --no-timers --maintenance-window \
  --config-source "$B/config.monitor.toml" --force-config
```

说明：

- 不传 `--start`；安装器只 enable 非 Portal unit，不启动。
- `--maintenance-window` 允许安装器停止仍 active 的受管写入者（如果第 3.1 节已
  停止，则确认它们仍停止），随后用 `backup.py --no-quiesce` 取一致快照；快照失败
  会中止安装。
- 新 venv 先 staging、smoke test，再原子切换 `.venv`；旧 venv 保留在
  `/opt/citespec-resource/venvs/legacy-*`。
- `--no-timers` 不会自动 disable 旧 timer；第 3.1 节已停止它们，回滚时按记录的
  原状态恢复。
- 若安装器失败，**不要手动清理**，先读它的 `[rollback]` 输出并确认
  `/var/backups/citespec-resource/<timestamp>/` 和 `snapshots/`；其 EXIT trap 会
  恢复文件、unit/timer 状态、NVIDIA 路径、旧 venv 和一致快照。确认回滚成功后，
  按第 7 节的启动顺序启动旧服务并验收。

## 7. 启动与验收

按写入者依赖顺序启动，不启动 timer、不启动 Portal：

```bash
systemctl daemon-reload
systemctl start citespec-resource-provision.path
systemctl start citespec-resource-intake.service
systemctl start citespec-resource-broker.service

# --no-timers 只阻止安装器启用 timer；若旧安装已启用，这里显式 disable。
for t in citespec-resource-logrotate.timer \
         citespec-resource-disk-check.timer citespec-resource-backup.timer; do
  systemctl disable --now "$t" 2>/dev/null || true
done
```

验收项：

- [ ] `systemctl is-active` 三个 unit 均为 active；`systemctl --failed` 无新增失败。
- [ ] `ss -ltnp` 没有新的非 loopback Citespec 监听；broker 仍只使用 Unix socket。
- [ ] `stat -c '%U:%G %a' /run/citespec-resource/broker.sock` 符合预期。
- [ ] `citespec-resource --version` 正常，`citespec-resource status` / 现有管理命令
      可连接 broker。
- [ ] `citespec-resource admin mode monitor` 成功；若因既有 lease 被拒绝，停止，
      不要强行切换，按第 8 节回滚或改用 maintenance 方案重新审批。
- [ ] `preflight.sh --config /etc/citespec-resource/config.toml --probe-nvidia
      --nvidia-only` 正常执行（此时 wrapper 应已安装；探测不应改设备权限）。
- [ ] 完整 `preflight.sh --config /etc/citespec-resource/config.toml` 通过（默认不再
      重复调用 `nvidia-smi`）。
- [ ] `bash deploy/verify_gpu_permissions.sh --json` 通过；`stat`/`getfacl` 显示
      `/dev/nvidia*` 为成员不可用的安全状态。
- [ ] `bash deploy/verify_portal_network.sh --nginx-root /etc/nginx` 只报告预期
      warning（Portal 未启用）；Portal unit 未 enable，且
      `/etc/citespec-resource/portal-network-accepted` 不存在。
- [ ] 未开放 development profile：`/etc/citespec-resource/gpu-isolation.ok` 不存在，
      active + development + container sudo 会被拒绝。只有真机
      `verify_gpu_isolation.sh` PASS 后才评估是否写标记。
- [ ] `systemctl is-enabled` 三个 timer 均为 disabled；`systemctl is-active` 均非
      active。
- [ ] 容器基线未变：`docker ps -a` 的 ID/状态与 `containers.tsv` 一致；exited
      容器仍为 exited；没有删除任何容器。
- [ ] `journalctl -u citespec-resource-broker -u citespec-resource-intake --since`
      无持续错误。
- [ ] 记录升级后的 `pip freeze`、unit 状态、DB `user_version` 和备份路径。

### 7.1 Portal 网络验收后启用（本窗口外）

本窗口**不启用 Portal**。只有在 DNS、证书、VPN 路由和防火墙由网络管理方验收后，
才运行唯一入口：

```bash
install -d -m 0755 /etc/citespec-resource
bash /usr/local/libexec/citespec-resource/enable_portal.sh \
  --yes \
  --approved-cidr <campus-or-vpn-cidr> \
  [--approved-cidr <additional-cidr>]...
```

该脚本先要求 `[portal].enabled = true`，再运行
`verify_portal_network.sh --acceptance`（读取 `nginx -T`、校验 TLS、源限制、loopback
反代和 listener）；全部通过后才写
`/etc/citespec-resource/portal-network-accepted` 并 `systemctl enable --now`。任一步
失败会删除标记并回滚 enablement。撤销：

```bash
systemctl disable --now citespec-resource-portal.service
rm -f /etc/citespec-resource/portal-network-accepted
```

保留 `/home/data2`（或实际数据盘）上的备份至少到下一次成功备份/验收，不要在同一
窗口删除。

## 8. 手动回滚（安装成功但验收失败）

触发条件：启动失败、schema 无法打开、SSH 策略异常、Portal/网络边界异常、容器状态
变化、磁盘/权限异常，或窗口内无法完成验收。

```bash
set -euo pipefail
B="<WINDOW_BACKUP_ROOT>/<UTC>"
INTAKE=''  # 填入第 3.2 节得到并记录的实际 intake DB 路径
: "${INTAKE:?set INTAKE from section 3.2}"

systemctl stop citespec-resource-broker.service \
  citespec-resource-intake.service citespec-resource-provision.path 2>/dev/null || true
systemctl stop citespec-resource-logrotate.timer \
  citespec-resource-disk-check.timer citespec-resource-backup.timer 2>/dev/null || true
```

1. 恢复安装前缀（备份包含版本化 `venvs/` 和旧 `.venv` 符号链接；先确认旧 venv
   可执行）：

   ```bash
   test -x "$B/opt-citespec-resource/.venv/bin/python" || {
     echo "backup prefix is not executable"; exit 1;
   }
   rm -rf /opt/citespec-resource
   cp -a "$B/opt-citespec-resource" /opt/citespec-resource
   ```

2. 恢复配置、unit、udev、sshd、nginx 和 NVIDIA 路径：

   ```bash
   tar -C / -xpf "$B/system-state.tar"
   ```

3. 按 `meta/pre-paths.txt` 删除本次新增的路径（例如原本不存在的
   `nvidia-smi.real`、shim、`portal-secret.key`、`portal.db` 和 Portal unit），
   不要把原本存在的文件删掉：

   ```bash
   for p in /usr/bin/nvidia-smi.real /usr/bin/nvidia-modprobe.real \
            /usr/local/lib/citespec-resource/libcitespec-nvidia-chmod.so \
            /usr/local/lib/citespec-resource /usr/local/libexec/citespec-resource \
            /etc/citespec-resource/portal-secret.key \
            /etc/citespec-resource/portal-network-accepted \
            /etc/citespec-resource/gpu-isolation.ok \
            /var/lib/citespec-resource/portal.db \
            /etc/systemd/system/citespec-resource-portal.service; do
     if grep -Fqx "$p absent" "$B/meta/pre-paths.txt"; then
       rm -rf -- "$p"
     fi
   done
   ```

   如果升级前没有 `citespec-portal` 账号，确认没有进程和文件使用它后，再按
   `meta/portal-account.txt` 的记录决定是否 `userdel`/`groupdel`；删除系统账号
   不是恢复服务所必需，保留一个无 Portal unit/密钥的禁用账号也可接受，但要在记录
   中注明。

4. 恢复 spool 和数据库（先删 WAL/SHM，再放回 `.backup` 副本）：

   ```bash
   rm -rf /var/lib/citespec-resource/provision
   cp -a "$B/provision" /var/lib/citespec-resource/provision 2>/dev/null || true

   restore_db() {
     src=$1; dst=$2; required=${3:-1}
     if [ ! -f "$src" ]; then
       [ "$required" -eq 0 ] && return 0
       echo "missing required DB backup: $src" >&2
       exit 1
     fi
     rm -f "$dst-wal" "$dst-shm"
     cp -a "$src" "$dst"
   }
   restore_db "$B/db/state.db" /var/lib/citespec-resource/state.db 1
   restore_db "$B/db/intake.db" "$INTAKE" 1
   restore_db "$B/db/portal.db" /var/lib/citespec-resource/portal.db 0
   # 按 meta/db-owners.txt 恢复 owner/mode
   ```

   如果保留安装器生成的
   `/var/backups/citespec-resource/snapshots/<timestamp>/manifest.json`，优先用它
   恢复同一时点的全部数据库、配置和 spool：

   ```bash
   "$VENV/bin/python" -m citespec_resource.backup restore \
     /var/backups/citespec-resource/snapshots/<timestamp> \
     --config /etc/citespec-resource/config.toml --yes
   ```

   `restore` 会自行停止写入者、强制 maintenance、校验每个文件，并在失败时回滚
   整组替换；手动 `restore_db` 仅作为没有可用快照时的后备。

5. 恢复服务状态并重载：

   ```bash
   systemctl daemon-reload
   sshd -t && systemctl reload sshd
   udevadm control --reload-rules
   udevadm trigger --action=change --sysname-match='nvidia*'
   ```

6. 按窗口前记录恢复 enable/active；至少恢复原 broker/intake/provision.path 状态，
   不要启用本次新增 timer：

   ```bash
   systemctl enable citespec-resource-broker.service
   systemctl enable citespec-resource-intake.service
   systemctl enable citespec-resource-provision.path
   systemctl start citespec-resource-provision.path
   systemctl start citespec-resource-intake.service
   systemctl start citespec-resource-broker.service
   if [ -f "$B/meta/timer-enabled.txt" ]; then
     while read -r t state; do
       [ "$state" = enabled ] && systemctl enable "$t"
     done < "$B/meta/timer-enabled.txt"
   fi
   ```

   若窗口前某些 unit 原本是 disabled，按第 2 节记录改为 disable，而不是一律
   enable。

7. 回滚验收：

   - `citespec-resource --version` 为旧版本；
   - `citespec-resource status` 可连接；
   - `sqlite3 state.db 'PRAGMA integrity_check;'` 为 `ok`，schema 与升级前一致；
   - `systemctl --failed` 无新增失败；
   - `docker ps -a` 与 `containers.tsv` 一致；
   - 无本次新增 timer 处于 enabled/active；
   - 保留回滚日志和备份路径。

## 9. 收尾

- 成功：在验收记录中写明提交、构建来源、备份路径、`pip freeze`、DB schema、unit/
  timer 状态、容器基线对比和未启用项（Portal、active、timers）。
- 失败：执行第 8 节，记录触发原因；不要把半升级状态留到窗口结束。
- 不在公开报告中写真实主机名、地址、SSH 端口、管理员账号或密钥。
- 备份至少保留到下一次成功备份或一次完整验收之后（以较晚者为准）；不要在同一
  窗口删除。
