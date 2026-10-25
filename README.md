# UDC 实验室共享 GPU 基础设施

## 1. 项目定位

本仓库是 UDC 实验室共享 GPU 服务器的独立基础设施仓库，核心实现为
`udc-resource`。该实现提供受 SSH 策略约束的成员 CLI、以 root 运行并仅监听
Unix socket 的 Broker、SQLite 状态存储、Docker 容器生命周期管理、资源排队、
monitor/maintenance 模式、重启对账，以及仅绑定回环地址的本地 Web GUI。

本仓库不依附于研究仓库，不是 `reconstruction` 或 `optic_system` 的 submodule。
本仓库不修改研究仓库的 Meta pin，不读取测试数据，也不操作研究实验环境。

## 2. 当前状态

- 代码实现：P0–P4 及后续安全加固已完成；P5 实机验收尚未执行。
- 目标环境：尚未在 amax 部署，未启动 Broker，未创建或停止任何训练容器。
- amax 当前仍承担训练任务。在训练结束且管理员明确批准前，不得部署、重启
  Docker 或 NVIDIA 服务，也不得改动现有容器或资源分配。
- `lab/resources.local.toml` 保存本地资源清单，已被 `.gitignore` 排除，不得提交。
- 生产基线为 Python 3.12；本地开发可使用 Python 3.13。
- 文件传输与 VS Code 接入仍需目标机验收，示例配置中分别保持
  `file_transfer_enabled = false` 与 `vscode_enabled = false`。

完整规格见 [server-resource-manager-vibe.md](server-resource-manager-vibe.md)；当前实施状态、
目标机待验证项和下一会话的操作边界见 [docs/handoff.md](docs/handoff.md)。

## 3. 系统组成

- **CLI `udc-resource`**：成员和管理员的唯一业务入口；所有业务结果使用稳定的
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

## 4. 安全与运行约束

- 成员使用独立 Linux 账号。成员通过宿主机 SSH 登录时仅允许密钥认证，通过
  `ForceCommand` 限制为 `udc-resource` 白名单命令，并使用 root-owned 登录 shell
  与 `PermitUserRC no`；密码、键盘交互、任意 host shell 和 SSH 转发均不可用。
- Broker 以 root systemd 服务运行，仅开放 Unix socket，并通过 `SO_PEERCRED` 识别
  用户。Broker socket 必须为 `root:udc-cli 0660`。
- CLI 是唯一业务入口。GUI 的数据请求仅调用 CLI `--json`，交互终端仅调用 CLI
  `shell`。
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
src/udc_resource/
  config.py          # TOML 配置加载与校验（含 CPU/RAM 上限与预留）
  models.py          # allocation / GPU / event 数据类
  db.py              # SQLite schema、连接 PRAGMA、settings
  store.py           # 用户/GPU/claim/allocation/event 事务
  identity.py        # UID/组身份解析与 SO_PEERCRED
  gpu_audit.py       # 设备指纹与 GPU 权限审计闸门
  workspace_quota.py # workspace 项目配额闸门
  errors.py          # Broker/CLI 共享错误码与退出码
  stream.py          # shell 长度前缀帧协议
  cli.py             # CLI：list/status/apply/release/logs/shell/gui/admin/job/queue/tunnel/vscode
  client.py          # Unix socket 客户端（超时/协议错误）
  login_wrapper.py   # SSH ForceCommand 白名单包装器
  broker/            # UDS Broker、Docker driver、到期 worker、重启对账
  gui/               # 本地 Web GUI、静态资源与 vendored xterm.js
deploy/
  preflight.sh       # 只读部署预检，不修改主机
  install_server.sh  # 幂等安装器；--dry-run / --yes / --start / --no-timers
  create_user.sh     # 成员/管理员账号与 SSH key
  member-shell.sh    # root-owned 成员登录 shell
  verify_gpu_permissions.sh # 只读全 GPU/NVIDIA 设备权限验收
  verify_monitor_only.sh    # 只读 monitor-only 安全验收
  docker/            # 开发容器 Dockerfile、sshd_config、bootstrap.sh
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
- [docs/handoff.md](docs/handoff.md)：当前实施状态、已验证项、目标机待验证项和
  下一会话的操作边界。

## 8. 实施阶段

- P0 已完成：Python 基线、依赖锁定、SQLite schema、配置上限、login wrapper
  绝对路径、只读 preflight 与 CI。
- P1 已完成：Unix socket Broker、`SO_PEERCRED`、SQLite 事务、多 GPU claim、
  用户/管理员权限、审计事件，以及 CLI `list/status/apply/admin/config`。
- P2 已完成：Docker driver、GPU/CPU/RAM 限制、workspace 创建、release、到期
  worker、重启对账、未纳管资源展示，以及 detached tmux job 与受限文件传输。
- P3 已完成：幂等安装脚本、成员/管理员账号脚本、udev/sshd/systemd 模板，以及
  只读 monitor-only 安全验收脚本。
- P4 已完成：本地 Web GUI、GPU 总览/申请/release/logs、管理员页、未纳管只读页
  和内嵌 xterm.js 终端。
- P5 待执行：训练结束后的实机 monitor-only → CPU → 单 GPU 验收，以及开发容器、
  VS Code、GPU 审计和存储闸门的 target-host 验证。验收步骤见
  [docs/acceptance.md](docs/acceptance.md)。
- 后续安全加固已完成本地实现与测试：active 启动/切换强制 legacy 审计、全局/每
  分配/每用户预算拆分、全 NVIDIA 节点与有效组验收、队列准入/取消/等待、开发
  容器存储与 `nosuid` 闸门、Broker 隧道和容器 SSH 准备。
- Linux-only 的 UDS/`SO_PEERCRED` 集成测试在 Windows 本地跳过，由 Ubuntu CI
  执行。
- 真实 Docker 集成测试需设置 `UDC_RUN_DOCKER_TESTS=1`，默认跳过。

## 9. 命令行示例

```text
udc-resource list --json
udc-resource status [allocation-id] --json
udc-resource apply --gpus 3,5 --cpus 16 --memory 64 --hours 12 --json
udc-resource apply --cpus 4 --memory 8 --hours 1 --json
udc-resource apply --gpus 7 --cpus 8 --memory 32 --wait --wait-timeout 3600
udc-resource apply --gpus 7 --cpus 8 --memory 32 --no-queue
udc-resource queue list --json
udc-resource queue list --history --all --json
udc-resource queue cancel <queue-id> --reason "no longer needed"
udc-resource queue wait <queue-id> --timeout 3600
udc-resource release <allocation-id> --json
udc-resource logs <allocation-id> --tail 200
udc-resource shell <allocation-id>
udc-resource shell <allocation-id> --job train
udc-resource job start <allocation-id> --name train -- python train.py
udc-resource job list <allocation-id>
udc-resource job logs <allocation-id> --name train
udc-resource job attach <allocation-id> --name train
udc-resource job stop <allocation-id> --name train
udc-resource tunnel <allocation-id>
udc-resource vscode <allocation-id>
udc-resource menu
udc-resource maintenance
udc-resource gui --server amax
udc-resource admin users --json
udc-resource admin resources --json
udc-resource admin audit --limit 100 --json
udc-resource admin mode monitor
udc-resource admin release <allocation-id> --reason maintenance
udc-resource admin reconcile --json
```

成员通过 SSH `ForceCommand` 只能执行白名单命令；管理员账号保留普通 shell。成员
只能使用配置中的基础镜像或 `image.allowlist` 中的镜像；管理员可指定其他镜像。

`udc-resource vscode <allocation-id>` 仅在 development profile 且
`vscode_enabled = true` 时可用。本地命令通过 SSH 打开 Broker 隧道，Broker 在目标
容器内启动 inetd-mode sshd，VS Code 以成员 UID 连接仅属于该 allocation 的容器。
host SSH 不因此开放转发或 shell。

## 10. 本地 GUI

```bash
udc-resource gui --server amax --admin-server amax-admin
```

- 仅绑定 `127.0.0.1`，使用随机端口和随机 token，并自动打开浏览器。
- 所有数据通过 `ssh <alias> udc-resource ... --json` 获取，不直接连接 Broker。
- 管理员页仅在 `admin` CLI 成功时显示；通过 `[gui] admin_ssh` 或
  `--admin-server` 配置管理员 alias。
- 内嵌 xterm.js 终端通过本地 WebSocket 和 `ssh -t`/asyncssh PTY 运行
  `udc-resource shell <id>`。
- xterm.js 与 addon-fit 已 vendored 到包内，离线可用，无 Node 构建或 CDN 依赖。

示例中的 `amax` 仅为 SSH alias 示例；实际值以本地 `lab/resources.local.toml`
和 SSH 配置为准。

## 11. 部署与验收状态

部署前依次执行只读检查和安装流程：

```bash
bash deploy/preflight.sh --config lab/resources.local.toml
bash deploy/audit_legacy.sh --production
bash deploy/install_server.sh --dry-run --skip-preflight
bash deploy/install_server.sh --yes
bash deploy/create_user.sh --yes --ssh-key /path/to/key.pub udc-alice
systemctl start udc-resource-broker
bash deploy/verify_monitor_only.sh --member udc-alice
```

`install_server.sh` 是幂等安装器。真实修改必须显式提供 `--yes`；默认只安装、不
启动，启动后强制使用 `monitor` 模式。安装器默认启用磁盘告警和日志轮转 timer，
可用 `--no-timers` 关闭。安装器会将严格审计脚本、GPU 权限验收脚本和 root-owned
成员 shell 安装到 `/usr/local/libexec/udc-resource/`。

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
key-only、root-owned 登录 shell 和 `PermitUserRC no`。已存在的成员必须运行
`deploy/create_user.sh --yes <user>` 迁移到新 shell，否则 active 审计会阻断。

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

## 12. 开发与验证

生产部署基线为 Python 3.12；本地可使用 Python 3.13。不得使用系统默认的 Python
3.8。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,gui]"
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m mypy src
.\.venv\Scripts\python -c "from udc_resource.config import load_config; c=load_config('lab/resources.example.toml'); print(c.server.name, c.policy.default_hours, c.policy.global_max_cpus, c.policy.max_cpus_per_user)"
```

依赖更新后运行 `uv lock`。部署前必须运行
`bash deploy/preflight.sh --config lab/resources.local.toml`（只读）。在训练结束并
获得管理员明确批准前，不得执行任何会修改主机的 `deploy/` 脚本。


## 成员自助接入（新增）

新成员申请、配置与日常使用见 `docs/guides/member-quickstart.md`；管理员审批与管理见 `docs/guides/admin-membership.md`。实现报告见 `docs/reports/member-onboarding-and-ux.md`。
