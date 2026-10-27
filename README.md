# Citespec 实验室共享 GPU 基础设施

## 1. 项目定位

本仓库是 Citespec 实验室共享 GPU 服务器的独立基础设施仓库，核心实现为
`citespec-resource`。该实现提供受 SSH 策略约束的成员 CLI、以 root 运行并仅监听
Unix socket 的 Broker、SQLite 状态存储、Docker 容器生命周期管理、资源排队、
monitor/maintenance 模式、重启对账、仅绑定回环地址的本地 Web GUI，以及供校园网/NJU VPN
使用的成员 Portal。

本仓库不依附于研究仓库，不是 `reconstruction` 或 `optic_system` 的 submodule。
本仓库不修改研究仓库的 Meta pin，不读取测试数据，也不操作研究实验环境。

## 2. 当前状态

当前实机已部署并处于 active 模式；公开树不包含服务器地址、管理员账号、SSH 端口
或实例名，完整实机信息保存在未跟踪的 `docs/handoff.local.md`。

| 范围 | 状态 | 证据 |
|---|---|---|
| 代码实现 | 完成 | 成员接入、审批、开户、密钥、撤权、队列、GUI、部署单元均已实现 |
| 自动化验证 | 完成 | 本地 `475 passed, 21 skipped`；Ruff、mypy、JS、Bash 通过 |
| CI | 完成 | GitHub Actions `40465970858`（测试矩阵）和 `40465970936`（Windows/Linux 打包）通过 |
| 实机核心验收 | 完成 | active 验收 PASSED；审计 0 failures；GPU 审计 0 failures |
| 实机运行 | active | Broker、intake、GPU 权限服务、provision path 正常；数据库为空 |
| 外部 onboarding | 阻塞 | 公共 443 返回 403，等待网络/系统管理员；本地 intake 正常 |
| VPN Portal | 代码完成、未部署 | 本地账号/TOTP、注册/审批、成员状态、只读状态、审计与备份已实现并测试；等待内网 DNS/VPN/证书答复 |
| 备份/告警 | 代码完成 | 日备份、校验、恢复、保留和安全告警单元已实现，CI `40465970858` 通过；实机部署与恢复演练待维护窗口 |
| 可选能力 | 未开放 | VS Code、文件传输、development profile/5.6 仍按门禁关闭 |
| amax | 未改动 | 不部署、不重启 Docker/NVIDIA、不操作现有容器 |

- 生产基线为 Python 3.12；本地开发可使用 Python 3.13。
- `lab/resources.local.toml` 保存本地资源清单，已被 `.gitignore` 排除。
- 仓库当前保持 public，便于外部审理；稳定后另行转为 private。
- 文件传输与 VS Code 在目标机验收完成前保持关闭，示例配置中分别为
  `file_transfer_enabled = false` 与 `vscode_enabled = false`。

完整规格见 [server-resource-manager-vibe.md](server-resource-manager-vibe.md)；当前实施状态、
已验证项、剩余工作和下一会话的操作边界见 [docs/handoff.md](docs/handoff.md)。

## 3. 系统组成

- **CLI `citespec-resource`**：成员和管理员的唯一业务入口；所有业务结果使用稳定的
  `--json` envelope。
- **Broker**：以 root 运行的 systemd 服务，仅监听 Unix socket，并通过
  `SO_PEERCRED` 识别调用者 UID。
- **SQLite**：保存 allocation、GPU claim、settings 与审计事件；使用 WAL 和显式
  事务。
- **Docker driver**：实现整卡 GPU 分配、CPU/内存限制、workspace 挂载、到期、
  释放和重启对账。development profile 支持临时 HOME、可写层配额、容器内 SSH
  与 sudo，并允许 VS Code 通过 Broker 隧道接入。
- **Job/file**：tmux 长任务支持断线重连和持久日志；文件传输仅限个人
  workspace，且在验收完成前保持禁用。
- **GUI**：本地 `127.0.0.1` Web UI 与 xterm.js；仅通过 SSH 调用 CLI，不直接连接
  Broker。
- **VPN Portal**：独立的低权限 `citespec-portal` 服务，仅监听 `127.0.0.1:8770`，
  由主机 nginx 在校园网/NJU VPN 入口终止 TLS。它只提供新成员注册、账号激活、
  本人成员信息、只读服务状态，以及管理员审批/成员状态管理；不提供资源申请、
  释放、队列、Web shell、VS Code 或文件传输。
- **成员闭环**：低权限 intake 申请页、管理员邀请/审批、root 窄执行器开户、
  中央 SSH 公钥目录、暂停/恢复/撤销与操作重试。成员资格是独立权威记录；
  已批准成员的正常 GPU 使用仍是自助申请，不逐次人工审批。

## 4. 安全与运行约束

- 成员使用独立 Linux 账号。成员通过宿主机 SSH 登录时仅允许密钥认证，通过
  `ForceCommand` 限制为 `citespec-resource` 白名单命令，并使用 root-owned 登录 shell
  与 `PermitUserRC no`；密码、键盘交互、任意 host shell 和 SSH 转发均不可用。
- 成员公钥只从 root 管理的 `/etc/citespec-resource/authorized-keys.d/<username>` 读取；
  sshd 配置刻意不保留 `~/.ssh/authorized_keys` 回退，撤销后成员无法在 home
  重建登录凭据。管理员公钥仍按普通管理员账号管理。
- Broker 以 root systemd 服务运行，仅开放 Unix socket，并通过 `SO_PEERCRED` 识别
  用户。Broker socket 必须为 `root:citespec-cli 0660`。
- CLI 是唯一业务入口。GUI 的数据请求仅调用 CLI `--json`，交互终端仅调用 CLI
  `shell`。VPN Portal 只通过 Broker 的 `portal_*` 窄命令白名单读写审批/成员状态，
  Broker 会记录实际操作者并重新校验管理员断言；Portal 进程不属于
  `citespec-admins`。
- Portal 使用本地账号、Argon2id 密码、TOTP、服务端会话和 CSRF 双重提交校验。
  激活码一次性、默认 24 小时有效；管理员角色变更仅能通过 CLI 完成；暂停/恢复/
  撤销等操作要求密码 + TOTP 再认证。
- Portal 默认 `[portal].enabled = false`。只有内网 DNS、NJU VPN 路由/DNS、TLS
  证书和 443 防火墙白名单全部验收后，才启动 Portal；不发布公网 A/AAAA，不把
  公网 443 映射到本机。
- GPU 按整卡分配，默认 12 小时、最长 36 小时。到期后停止容器并保留 workspace，
  不自动续租。
- 每用户默认最多 4 张 GPU、4 个 active allocation；管理员同样受预算限制。
- 开发容器以成员 UID 运行，并在容器内提供 passwordless `sudo`。在未启用 user
  namespace 的默认配置下，容器内 UID 0 在 Docker 权限范围内映射到宿主 UID 0，
  因此必须同时满足以下条件：禁止 privileged 模式、Docker socket、host PID/network
  namespace 和 host 系统挂载；workspace 文件系统必须挂载为 `nosuid`。
- 开发容器仅挂载个人 `/workspace`。临时 HOME 与安装的系统包随 allocation 消失；
  `/workspace` 持久保留。
- Docker 可写层使用 `storage_opt` 配额。若 `overlay2`、XFS 或 `prjquota`/`pquota`
  预检失败，必须阻止 development profile 的新分配。
- workspace 使用目录/项目配额，不使用 UID 配额。active 模式要求
  `workspace_quota_required = true` 且 `max_workspace_gb > 0`。
- GPU 权限审计在 Broker 启动、切换到 active、周期性检查及设备指纹变化时执行。
  审计失败时阻止新分配，但保留既有任务及安全的释放/到期路径。
- `monitor` 为只读模式，不操作未知容器，也不暂停既有 lease。存在 active/pending
  lease 时使用 `maintenance`：阻止新申请，但继续执行到期与对账。
- 所有 `apply` 必须尊重队列顺序。`--no-queue` 仅表示不等待、不保留请求，不得越过
  已排队的请求；存在未过期排队请求时，直接申请也必须先触发调度或进入队列。
- 文件传输在 TOCTOU 安全处理、配额和 target-host 测试通过前保持禁用。VS Code
  接入在容器内 sshd、隧道释放和配额测试通过前保持禁用。
- 成员目录仅暴露 `display_name`、`nickname` 和可选的 `public_contact`；不得写入
  私人电话、私人邮箱、学号等敏感信息。

## 5. 实验室资源与配置

实际服务器地址、SSH 端口、已知主机文件和管理员入口仅写入：

```text
lab/resources.local.toml
```

该文件已被 `.gitignore` 排除，不得提交或公开。可提交的模板为：

```text
lab/resources.example.toml
```

amax 的静态硬件与路径信息、部署闸门记录在 [lab/README.md](lab/README.md)。GPU 和
容器的实时占用必须由 Broker 查询，不得写入配置文件或仓库。

## 6. 仓库结构

```text
src/citespec_resource/
  config.py          # TOML 配置加载与校验（含 CPU/RAM 上限与预留）
  models.py          # allocation / GPU / event 数据类
  db.py              # SQLite schema、连接 PRAGMA、settings
  store.py           # 用户/GPU/claim/allocation/event 事务
  identity.py        # UID/组身份解析与 SO_PEERCRED
  netutil.py         # nginx 后端的受信客户端 IP 解析
  gpu_audit.py       # 设备指纹与 GPU 权限审计闸门
  workspace_quota.py # workspace 项目配额闸门
  errors.py          # Broker/CLI 共享错误码与退出码
  stream.py          # shell 长度前缀帧协议
  cli.py             # CLI：join/setup/me/start/apply/admin/... 与工作站中继
  client.py          # Unix socket 客户端（超时/协议错误）
  login_wrapper.py   # SSH ForceCommand 白名单包装器
  onboarding.py      # setup/doctor：托管 SSH 片段、主机指纹、成员状态
  membership/        # 权威成员状态、审批、开户操作、密钥与 legacy import
  enrollment/        # 低权限公开申请页与回执查询
  portal/            # VPN-only 成员 Portal、本地账号/TOTP 与窄 Broker 桥
  broker/            # UDS Broker、Docker driver、到期 worker、重启对账
  gui/               # 本地 Web GUI、静态资源与 vendored xterm.js
deploy/
  preflight.sh       # 只读部署预检，不修改主机
  install_server.sh  # 幂等安装器；intake/broker/executor 单元与目录
  create_user.sh     # 成员/管理员账号；成员密钥写 root 中央目录
  membership/provision_executor.py # 固定动作的 root 开户执行器
  member-shell.sh    # root-owned 成员登录 shell
  verify_gpu_permissions.sh # 只读全 GPU/NVIDIA 设备权限验收
  verify_monitor_only.sh    # 只读 monitor-only 安全验收
  docker/            # 开发容器 Dockerfile、sshd_config、bootstrap.sh
  nginx/             # 仅示例：VPN 入口 Portal/apply vhost，不含证书
  systemd/udev/sshd/ # 部署模板
docs/                # 验收、队列、job/文件传输、legacy 权限撤销与交接文档
tests/               # 配置、数据库与安全规则测试
lab/                 # 本地资源清单与说明
.github/workflows/   # Linux/Windows CI
uv.lock              # 锁定依赖版本
```

## 7. 文档索引

- [docs/acceptance.md](docs/acceptance.md)：active 模式验收、开发容器与 VS Code
  验收，以及必须保留的证据。
- [docs/queue.md](docs/queue.md)：排队状态、预算准入、取消/等待和运维语义。
- [docs/jobs-and-transfer.md](docs/jobs-and-transfer.md)：detached tmux 任务与受限
  文件传输。
- [docs/legacy-access-revocation.md](docs/legacy-access-revocation.md)：旧管理员
  权限撤销与验证清单。
- [docs/backup-restore.md](docs/backup-restore.md)：状态备份、校验、恢复和失败
  告警 runbook（含 `portal.db`）。
- [docs/server-network-request.md](docs/server-network-request.md)：校园网/NJU VPN
  入口的 DNS、VPN、防火墙、TLS 证书问题和验收清单。
- [docs/handoff.md](docs/handoff.md)：当前实施状态、已验证项、目标机待验证项和
  下一会话的操作边界。
- [docs/guides/member-quickstart.md](docs/guides/member-quickstart.md)：新成员申请、
  工作站配置、密钥轮换与日常自助。
- [docs/guides/admin-membership.md](docs/guides/admin-membership.md)：邀请、审批、
  开户、密钥、暂停/恢复/撤销与 legacy import。
- [docs/reports/member-onboarding-and-ux.md](docs/reports/member-onboarding-and-ux.md)：
  本轮实现、实际测试结果与 D1/D2/D3 结论。

- [docs/client-distribution.md](docs/client-distribution.md)：Windows/Linux 客户端安装、
  签名自动更新、构建发布和回滚。
## 8. 实现里程碑

> 以下 M0–M5 是初始实现阶段；历史文档中的 P0–P5 指同一组里程碑。后续变更优先级
> 使用 P0–P2，见 [docs/handoff.md](docs/handoff.md)。

- M0 已完成：Python 基线、依赖锁定、SQLite schema、配置上限、login wrapper
  绝对路径、只读 preflight 与 CI。
- M1 已完成：Unix socket Broker、`SO_PEERCRED`、SQLite 事务、多 GPU claim、
  用户/管理员权限、审计事件，以及 CLI `list/status/apply/admin/config`。
- M2 已完成：Docker driver、GPU/CPU/RAM 限制、workspace 创建、release、到期
  worker、重启对账、未纳管资源展示，以及 detached tmux job 与受限文件传输。
- M3 已完成：幂等安装脚本、成员/管理员账号脚本、udev/sshd/systemd 模板，以及
  只读 monitor-only 安全验收脚本。
- M4 已完成：本地 Web GUI、GPU 总览/申请/release/logs、管理员页、未纳管只读页
  和内嵌 xterm.js 终端。
- M5 核心完成：active 模式核心验收已在实机通过，覆盖 preflight、真实 SSH
  边界、并发 apply、队列、到期/重启、workspace 保留和一次性成员开户闭环。
  外部 onboarding、VS Code、文件传输、development profile 和容器 root/宿主 GPU
  5.6 尚未开放；验收步骤见 [docs/acceptance.md](docs/acceptance.md)。
- 后续安全加固已完成本地实现与测试并部署：active 启动/切换强制 legacy 审计、
  全局/每分配/每用户预算拆分、全 NVIDIA 节点与有效组验收、队列准入/取消/等待、
  开发容器存储与 `nosuid` 闸门、Broker 隧道和容器 SSH 准备。
- Linux-only 的 UDS/`SO_PEERCRED` 集成测试在 Windows 本地跳过，由 Ubuntu CI
  执行；真实 Docker 集成测试需设置 `CITESPEC_RESOURCE_RUN_DOCKER_TESTS=1`。

## 9. 命令行示例

```text
citespec-resource list --json
citespec-resource status [allocation-id] --json
citespec-resource apply --gpus 3,5 --cpus 16 --memory 64 --hours 12 --json
citespec-resource apply --cpus 4 --memory 8 --hours 1 --json
citespec-resource apply --gpus 7 --cpus 8 --memory 32 --wait --wait-timeout 3600
citespec-resource apply --gpus 7 --cpus 8 --memory 32 --no-queue
citespec-resource queue list --json
citespec-resource queue list --history --all --json
citespec-resource queue cancel <queue-id> --reason "no longer needed"
citespec-resource queue wait <queue-id> --timeout 3600
citespec-resource release <allocation-id> --json
citespec-resource logs <allocation-id> --tail 200
citespec-resource shell <allocation-id>
citespec-resource shell <allocation-id> --job train
citespec-resource job start <allocation-id> --name train -- python train.py
citespec-resource job list <allocation-id>
citespec-resource job logs <allocation-id> --name train
citespec-resource job attach <allocation-id> --name train
citespec-resource job stop <allocation-id> --name train
citespec-resource tunnel <allocation-id>
citespec-resource vscode <allocation-id>
citespec-resource menu
citespec-resource maintenance
citespec-resource gui --server <server-alias>
citespec-resource admin users --json
citespec-resource admin resources --json
citespec-resource admin audit --limit 100 --json
citespec-resource admin mode monitor
citespec-resource admin release <allocation-id> --reason maintenance
citespec-resource admin reconcile --json
```

成员通过 SSH `ForceCommand` 只能执行白名单命令；管理员账号保留普通 shell。成员
只能使用配置中的基础镜像或 `image.allowlist` 中的镜像；管理员可指定其他镜像。

`citespec-resource vscode <allocation-id>` 仅在 development profile 且
`vscode_enabled = true` 时可用。本地命令通过 SSH 打开 Broker 隧道，Broker 在目标
容器内启动 inetd-mode sshd，VS Code 以成员 UID 连接仅属于该 allocation 的容器。
host SSH 不因此开放转发或 shell。

## 10. 本地 GUI

```bash
citespec-resource gui --server <server-alias> --admin-server <admin-alias>
```

- 仅绑定 `127.0.0.1`，使用随机端口和随机 token，并自动打开浏览器。
- 所有数据通过 `ssh <alias> citespec-resource ... --json` 获取，不直接连接 Broker。
- 管理员页仅在 `admin` CLI 成功时显示；通过 `[gui] admin_ssh` 或
  `--admin-server` 配置管理员 alias。
- 内嵌 xterm.js 终端通过本地 WebSocket 和 `ssh -t`/asyncssh PTY 运行
  `citespec-resource shell <id>`。
- xterm.js 与 addon-fit 已 vendored 到包内，离线可用，无 Node 构建或 CDN 依赖。

示例使用 `<server-alias>`；实际值以本地 `lab/resources.local.toml` 或 SSH 配置为准。

## 11. VPN-only 成员 Portal（C1）

Portal 是独立于本地 GUI 的低权限 Web 服务，面向成员和管理员使用同一个 HTTPS
地址；管理员能力来自本地 Portal 账号的 `admin` 角色，不另外部署管理入口。它
默认关闭，只监听 `127.0.0.1:8770`，由主机 nginx 在校园网/NJU VPN 入口终止 TLS：

```text
citespec-portal.<lab-domain> → 127.0.0.1:8770   # 成员/管理员 Portal
citespec-apply.<lab-domain>  → 127.0.0.1:8765   # 旧 intake/申请页，保持独立
```

公网不发布 A/AAAA，不把公网 443 映射到本机。DNS、VPN 路由/DNS、证书和防火墙
源地址段由服务器/网络管理确认，详见
[docs/server-network-request.md](docs/server-network-request.md)。在答复和验收完成前，
保持 `[portal].enabled = false`，不要启动 `citespec-resource-portal.service`。

### 11.1 Web 范围

允许：

- `/register`：VPN 内新成员注册；提交经 Broker 写入 intake 数据库；
- `/activate`：使用一次性激活码设置本地密码并绑定 TOTP；
- `/login`、`/logout`、`/account`：本地账号登录、改密和 TOTP 状态；
- `/`、`/status`：本人成员资格与只读服务状态；
- `/admin/registrations`、`/admin/members`、`/admin/audit`：管理员审批、成员
  暂停/恢复/撤销、激活码重置和审计查看。

明确不提供：GPU/容器申请、释放、队列操作、Web shell、VS Code、文件传输，也不
代理现有本地 `citespec-resource gui`。这些仍只能通过 CLI/SSH 使用。

### 11.2 账号、审批与激活

1. 申请人在 VPN 内提交 `/register`；Portal 不直接创建 Linux 账号，只通过
   `portal_registration_submit` 写入 intake。
2. 管理员在 `/admin/registrations` 查看并审批；审批沿用 Broker 的成员审批与
   root 开户流程，同时创建（或复用）`pending_activation` Portal 账号。
3. 一次性激活码只在审批结果页显示一次，默认 24 小时有效；数据库只保存哈希。
4. 申请人打开 `/activate`，输入用户名和激活码，设置密码并扫描/录入 TOTP。
5. 账号变为 `active` 后才能登录。成员 `resume` 只恢复 Linux 成员资格，不会把
   `pending_activation` Portal 账号直接变成 `active`；必须完成激活。
6. Portal 管理员角色变更仅通过 CLI；暂停/恢复/撤销、审批等 Web 操作要求再次
   输入密码 + TOTP。首个管理员和后续角色调整见 11.5。

### 11.3 安全与部署约束

- 密码使用 Argon2id；TOTP secret 使用 root-owned 密钥文件加密后落盘；会话 token
  和激活码只保存哈希；所有 POST 使用服务端会话绑定的 CSRF token。
- Portal 进程属于 `citespec-portal` 和 `citespec-cli`，不得加入 `citespec-admins`；
  Broker 只接受 `PORTAL_COMMANDS` 白名单，并重新校验断言的管理员角色。
- nginx 必须覆盖 `X-Citespec-Client-IP $remote_addr`，应用只信任 loopback 传来的
  该头；请求体上限与 Portal 一致为 64 KiB。
- Portal 数据库、审计记录、TOTP 加密密钥和配置树纳入备份；恢复时先停止 Portal
  writer unit。
- 页面为 Jinja2 服务端渲染，无 JavaScript；CSP 只允许同源 CSS/图片，禁止 frame、
  WebSocket 和外部资源。

### 11.4 安装与本地验证

安装器会创建低权限账号、`citespec-portal` 组、状态目录、root-owned 密钥文件和
`portal.db`，安装 systemd unit；只有 `[portal].enabled = true` 时才 enable，且
**从不自动 start**。安装示例：

```bash
bash deploy/install_server.sh --dry-run --skip-preflight
bash deploy/install_server.sh --yes
# 网络/证书验收后再执行：
sudo systemctl start citespec-resource-portal.service
```

`deploy/nginx/` 中只有 vhost 示例，不包含证书、DNS 或防火墙改动。本地验证命令：

```powershell
.\.venv\Scripts\python -m pytest tests/test_portal.py tests/test_broker.py tests/test_backup.py -q
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m mypy src
```

### 11.5 首个管理员与角色变更（仅 CLI）

Portal 没有 Web 角色管理路由。首个管理员、后续授权和降权都由
`citespec-admins` 成员通过 Broker 的 `admin_portal_accounts_*` 命令完成；Broker
会重新校验 Linux 管理员组，Portal 服务账号不能调用这些命令：

```bash
# 创建首个 Portal 管理员；激活码只显示一次
citespec-resource admin portal create --username labadmin --role admin \
  --display-name "Lab Admin" --contact admin@example.invalid

# 查看本地 Portal 账号（不含密码、TOTP 或激活码）
citespec-resource admin portal list

# 授权/降权；目标账号的现有浏览器会话会被注销
citespec-resource admin portal set-role --username labadmin --role member
citespec-resource admin portal set-role --username another --role admin
```

- 新成员的 Portal 账号由审批流程自动创建为 `member`；如需让某成员成为管理员，
  必须显式执行 `set-role ... --role admin`。
- `create` 对同名 `pending_activation` 账号会重新签发一次性激活码；已激活账号
  请改用 `set-role`，不会通过重复创建绕过激活。
- 不能把最后一个 `active` 管理员降为普通成员，避免 Portal 失去管理入口；先激活
  另一个管理员再降权。
- 角色变更同时写入 Portal 审计和 Broker 审计；审计不记录激活码、密码或 TOTP。
- 若 `portal.db` 尚未由安装器初始化，命令返回“未初始化”，不会自行创建 root
  所有的数据库目录。

## 12. 部署与验收状态

部署前依次执行只读检查和安装流程：

```bash
bash deploy/preflight.sh --config lab/resources.local.toml
bash deploy/audit_legacy.sh --production
bash deploy/install_server.sh --dry-run --skip-preflight
bash deploy/install_server.sh --yes
systemctl start citespec-resource-broker citespec-resource-intake.service citespec-resource-provision.path
bash deploy/verify_monitor_only.sh --member <member>
```

`install_server.sh` 是幂等安装器。真实修改必须显式提供 `--yes`；默认只安装、不
启动，启动后强制使用 `monitor` 模式。安装器默认启用磁盘告警、日志轮转和每日
状态备份 timer，可用 `--no-timers` 关闭。安装器会将严格审计脚本、GPU 权限验收脚本、root-owned
成员 shell、窄开户执行器和 intake 服务安装到固定路径，并创建
`/etc/citespec-resource/authorized-keys.d/`、开户 spool 与低权限 intake 状态目录。
它同时创建 `citespec-portal` 账号/组、`portal.db`、root-owned Portal 密钥和
systemd unit；`[portal].enabled = true` 时仅 enable，不 start。安装 Python 包时
使用 `.[gui,portal]` extras，使 intake/GUI/Portal 依赖可用。`deploy/nginx/` 仅是
vhost 示例，安装器不会修改 nginx、DNS、VPN 或防火墙。

正式成员不再通过手工 `create_user.sh` 直接加入。管理员先在 GUI/CLI 生成邀请，
申请人通过申请页或 `citespec-resource join` 提交；批准后 Broker 写入固定动作 spool，
root 执行器创建/校验账号、写中央公钥、工作区和配额。已有 Linux 账号必须走
显式 manifest 导入（默认 dry-run）：

```bash
citespec-resource admin members import-legacy --file legacy-members.json
citespec-resource admin members import-legacy --file legacy-members.json --apply
```

`create_user.sh` 仅保留为审计过的 break-glass/管理员工具；成员密钥现在默认写入
root 中央目录，不再依赖 home `authorized_keys`。

active 配置必须通过 `audit_legacy.sh --production`，且 Broker 在启动和每次切换到
active 前都会重新执行审计。`/root/.ssh/authorized_keys` 非空默认阻断，只有显式
设置 `allow_root_ssh_keys = true` 才降级为告警。`verify_monitor_only.sh` 为只读
脚本，不会停止容器或改动主机状态。

development profile 部署前，`preflight.sh` 会检查以下条件；任一条件不满足时必须
阻止可写层开发容器：

- Docker 存储驱动为 `overlay2`；
- DockerRootDir 位于带 `prjquota`/`pquota` 的 XFS；
- `xfs_quota` 可用；
- workspace 项目配额可用；
- workspace 文件系统挂载为 `nosuid`。

容器内 sudo 获得的 UID 0 在没有 user namespace 时对应宿主 UID 0；`nosuid` 用于
防止其通过 workspace bind 写入宿主可执行的 setuid 或文件能力文件。成员 SSH 使用
key-only、root-owned 登录 shell、`PermitUserRC no` 和中央公钥目录。已有账号不会
因为同名就被自动采用：必须由管理员用 `admin members import-legacy` 显式审核
账号、UID/组、密钥、有效期与配额；否则 active 审计/审批会阻断。部署前用
`sshd -T -C user=<member>,host=<host>,addr=<addr>` 确认
`authorizedkeysfile` 指向 `/etc/citespec-resource/authorized-keys.d/%u`。

NVIDIA udev 规则按 `KERNEL=="nvidia*"` 匹配，覆盖 `nvidiactl`、`nvidia-uvm`、
`nvidia-uvm-tools`、`nvidia-modeset` 等可能位于 `misc` 子系统的节点。验收脚本会
检查所有 `/dev/nvidia*` 节点、有效组成员关系、ACL，以及成员 UID 的实际读写和
`nvidia-smi` 拒绝结果。

预算拆分为 `global_max_cpus` / `global_max_memory_gb`、每分配上限
`max_cpus_per_allocation` / `max_memory_gb_per_allocation`、每用户上限
`max_cpus_per_user` / `max_memory_gb_per_user`。旧的 `max_cpus` / `max_memory_gb`
仍被接受，但仅作为每分配上限。GPU、CPU 或内存不足时默认进入队列；运行中的分配
释放、到期或 Broker 启动后，队列会在检测到真实空闲 GPU 后按顺序准入。

开发容器镜像必须在 target host 上构建并记录 digest，不得使用浮动 tag 进行验收。
在容器 sshd、隧道释放、配额和 target-host 测试全部通过前，不得启用
`file_transfer_enabled` 或 `vscode_enabled`。

## 13. 开发与验证

生产部署基线为 Python 3.12；本地可使用 Python 3.13。不得使用系统默认的 Python
3.8。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,gui,portal]"
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m mypy src
.\.venv\Scripts\python -c "from citespec_resource.config import load_config; c=load_config('lab/resources.example.toml'); print(c.server.name, c.policy.default_hours, c.policy.global_max_cpus, c.policy.max_cpus_per_user)"
# 可选真实浏览器测试（需安装 Playwright）
.\.venv\Scripts\python -m pip install -e ".[browser]"
.\.venv\Scripts\python -m playwright install chromium
.\.venv\Scripts\python -m pytest tests/test_intake_browser.py -q
```

依赖更新后运行 `uv lock`。部署前必须运行
`bash deploy/preflight.sh --config lab/resources.local.toml`（只读）。在训练结束并
获得管理员明确批准前，不得执行任何会修改主机的 `deploy/` 脚本。


## 成员自助接入

```text
citespec-resource join --portal https://<申请页>/compute   # 无服务器账号也可用
citespec-resource join --status --portal https://<申请页>/compute
# 输入申请编号 + 回执密钥；回执只走 POST，不进 URL
citespec-resource setup --server <地址或别名> --user <Linux用户名>
# 无人值守：再加 --fingerprint SHA256:<管理员公布值> --yes
citespec-resource me                                      # 状态与公钥
citespec-resource start --gpu-count 1                     # 批准后的正常自助使用
```

`setup` 只在首次连接时显示主机 SHA256 指纹并要求明确确认；它写入独立托管 SSH
片段和 `~/.config/citespec-resource/client.toml`（仅连接信息，不复制服务器硬件/策略
配置），不覆盖已有 SSH 配置或私钥。工作站日常命令在无本地 Broker socket 时
经 SSH 中继到服务器，不要求本地运行 Broker。

Portal 上线后，成员在 VPN 内打开 `https://citespec-portal.<lab-domain>/register`
提交注册；批准后使用一次性激活码完成密码/TOTP 激活。Portal 只用于账号、成员状态
和服务状态；GPU/容器申请、释放、队列、Shell、VS Code 和文件传输仍使用上面的
CLI/SSH 命令。

新成员申请、配置与日常使用见 `docs/guides/member-quickstart.md`；管理员审批与
管理见 `docs/guides/admin-membership.md`。实现报告见
`docs/reports/member-onboarding-and-ux.md`；D2 核心实机验收已完成，D3 对外生产
开放仍未完成，剩余项包括外部 onboarding、VS Code、文件传输和 5.6。
