# UDC Lab Infrastructure

本仓库是 UDC 实验室共享 GPU 服务器的独立基础设施仓库，核心实现为 `udc-resource`：受限 SSH CLI、root Broker、SQLite 状态、Docker 生命周期、monitor-only 对账和本地 Web GUI。

`infrastructure` 不依附于研究仓库，不是 `reconstruction` / `optic_system` 的 submodule；不修改 Meta pin、不读取测试集、不操作实验机。

## 当前状态

- 代码阶段 P0–P4 已完成；P5 实机验收待训练结束和管理员批准。
- **尚未在 amax 部署**，未启动 Broker，未创建或停止任何训练容器。
- amax 当前仍在训练；禁止部署或重启 Docker。
- `lab/resources.local.toml` 保存本地资源清单，不进入 Git。
- 生产基线 Python 3.12；本机开发可使用 Python 3.13。

完整规格见 [server-resource-manager-vibe.md](server-resource-manager-vibe.md)。

## 系统组成

- **CLI `udc-resource`**：成员和 admin 的唯一入口；业务结果使用稳定 `--json` envelope。
- **Broker**：root systemd 服务，只监听 Unix socket，通过 `SO_PEERCRED` 识别 UID。
- **SQLite**：allocation、GPU claim、settings 和审计事件；WAL + 显式事务。
- **Docker driver**：整卡 GPU、CPU/RAM 限制、workspace 挂载、到期/释放/重启对账。
- **GUI**：本地 `127.0.0.1` Web UI + xterm.js；只通过 SSH 调用 CLI，不直连 Broker。

## 设计约束

- 成员使用独立 Linux 账号；SSH 通过 `ForceCommand` 只允许 `udc-resource` 白名单命令。
- Broker 以 root systemd 运行，只开 Unix socket，通过 `SO_PEERCRED` 识别用户。
- CLI 是唯一用户业务入口；GUI 的数据请求只调用 CLI `--json`，交互终端只调用 CLI `shell`。
- GPU 整卡、12h 默认 / 36h 上限；到期停止容器，保留 workspace，不自动续租。
- 容器内 root，但不 privileged、不挂 host Docker socket、只挂个人 workspace。
- 先 `monitor-only`，只显示现有占用，不碰未知容器。

## 本地实验室资源

实际服务器地址、SSH 端口、已知主机文件、管理员入口等只写入：

```text
lab/resources.local.toml
```

该文件被 `.gitignore` 排除，不得提交或公开。可提交模板是：

```text
lab/resources.example.toml
```

当前 amax 的静态硬件/路径和部署闸门记录在 `lab/README.md`。GPU/容器实时占用必须由 Broker 查询，不写死进配置。

## 目录

```text
src/udc_resource/
  config.py          # TOML 配置加载与校验（含 CPU/RAM 上限与预留）
  models.py          # allocation / GPU / event 数据类
  db.py              # SQLite schema、连接 PRAGMA、settings
  store.py           # 用户/GPU/claim/allocation/event 事务
  identity.py        # UID/组身份解析与 SO_PEERCRED
  errors.py          # Broker/CLI 共享错误码与退出码
  stream.py          # shell 长度前缀帧协议
  cli.py             # CLI：list/status/apply/release/logs/shell/gui/admin
  client.py          # Unix socket 客户端（超时/协议错误）
  login_wrapper.py   # SSH ForceCommand 白名单包装器
  broker/            # UDS Broker、Docker driver、到期 worker、重启对账
  gui/               # 本地 Web GUI、静态资源与 vendored xterm.js
deploy/
  preflight.sh       # 只读部署预检，不修改主机
  install_server.sh  # 幂等安装器；--dry-run / --yes / --start
  create_user.sh     # 成员/管理员账号与 SSH key
  verify_monitor_only.sh  # 只读 monitor-only 安全验收
  systemd/udev/sshd/docker/  # 部署模板
tests/               # 配置、数据库与安全规则测试
lab/                 # 本地资源清单与说明
.github/workflows/   # Linux/Windows CI
uv.lock              # 锁定依赖版本
```

## 阶段状态

- P0 已完成：Python 3.11+ 基线、依赖锁定、SQLite schema、配置上限、login wrapper 绝对路径、只读 preflight、CI。
- P1 已完成：Unix socket Broker、`SO_PEERCRED`、SQLite 事务、多 GPU claim、用户/管理员权限、审计事件、CLI `list/status/apply/admin/config`。
- P2 已完成：真实 Docker driver、GPU/CPU/RAM 限制、workspace 创建、release、到期 worker、重启对账、未纳管资源展示。
- P3 已完成：幂等安装脚本、成员/管理员账号脚本、udev/sshd/systemd 模板、只读 monitor-only 安全验收脚本。
- P4 已完成：本地 Web GUI、GPU 总览/申请/release/logs、管理员页、未纳管只读页、内嵌 xterm.js 终端。
- P5 待实现：训练结束后的实机 monitor-only → CPU → 单 GPU 验收；四项 active 验收脚本见 `docs/acceptance.md`。
- Linux-only 的 UDS/`SO_PEERCRED` 集成测试在 Windows 本地跳过，由 Ubuntu CI 执行。
- 真实 Docker 集成测试需 `UDC_RUN_DOCKER_TESTS=1`，默认跳过。
- amax 仍在训练，**禁止部署**。
- `lab/resources.local.toml` 已写入当前实验室资源；不进入 Git。

## CLI 示例

```text
udc-resource list --json
udc-resource status [allocation-id] --json
udc-resource apply --gpus 3,5 --cpus 16 --memory 64 --hours 12 --json
udc-resource apply --cpus 4 --memory 8 --hours 1 --json   # CPU-only
udc-resource release <allocation-id> --json
udc-resource logs <allocation-id> --tail 200
udc-resource shell <allocation-id>
udc-resource gui --server amax
udc-resource admin users --json
udc-resource admin resources --json
udc-resource admin audit --limit 100 --json
udc-resource admin mode monitor
udc-resource admin release <allocation-id> --reason maintenance
udc-resource admin reconcile --json
```

成员通过 SSH `ForceCommand` 只能执行白名单命令；管理员账号保留普通 shell。成员只能使用配置中的基础镜像；管理员可指定其他镜像。

## 本地 GUI

```bash
udc-resource gui --server amax --admin-server amax-admin
```

- 只绑定 `127.0.0.1`，随机端口并自动打开浏览器；带随机 token。
- 所有数据通过 `ssh <alias> udc-resource ... --json` 获取，不直连 Broker。
- 管理员页仅在 `admin` CLI 成功时显示；`[gui] admin_ssh` 或 `--admin-server` 配置管理员 alias。
- 内嵌 xterm.js 终端通过本地 WebSocket + `ssh -t`/asyncssh PTY 运行 `udc-resource shell <id>`。
- xterm.js 与 addon-fit 已 vendored 到包内，离线可用，无 Node 构建或 CDN。

## 部署（仅在训练结束并获批准后）

```bash
bash deploy/preflight.sh
bash deploy/audit_legacy.sh
bash deploy/install_server.sh --dry-run --skip-preflight
bash deploy/install_server.sh --yes
bash deploy/create_user.sh --yes --ssh-key /path/to/key.pub udc-alice
systemctl start udc-resource-broker
bash deploy/verify_monitor_only.sh --member udc-alice
```

`install_server.sh` 幂等、支持 `--dry-run`，真实修改必须显式 `--yes`；默认只安装、不启动，启动后强制 `monitor` 模式。`verify_monitor_only.sh` 只读，不会停止容器。

## 快速检查

生产部署基线为 Python 3.12；本地可用 Python 3.13。不要使用系统默认的 Python 3.8。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,gui]"
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m mypy src
.\.venv\Scripts\python -c "from udc_resource.config import load_config; c=load_config('lab/resources.example.toml'); print(c.server.name, c.policy.default_hours, c.policy.max_cpus)"
```

依赖更新后运行 `uv lock`；部署前运行 `bash deploy/preflight.sh`（只读）。不要在没有管理员批准和训练结束确认的情况下执行任何会修改主机的 `deploy/` 脚本。
