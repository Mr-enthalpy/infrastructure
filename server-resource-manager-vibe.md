# 实验室 GPU 服务器资源管理器 — 需求与设计规格

- 版本：2026-10-22
- 文档状态：初始需求与设计基线，保留用于追溯设计意图
- 部署目标：首期仅针对 example-gpu 一台服务器
- 编写时状态：**不部署**。example-gpu 仍在训练；先完成实现、测试和 monitor-only 验收。

> 本文档描述初始设计目标。当前实施状态、部署边界和验收流程以
> [README.md](README.md)、[docs/handoff.md](docs/handoff.md) 和
> [docs/acceptance.md](docs/acceptance.md) 为准。文中“当前状态”均指
> 2026-10-22 编写时的状态。
>
> 文中的 P0–P5 是初始实现里程碑，不是当前变更优先级；后续优先级 P0–P2 见
> [docs/handoff.md](docs/handoff.md)。

---

## 0. 一句话目标

为实验室共享 GPU 服务器做一个**简单、强制、可审计**的动态资源租用系统：

- 成员用独立 Linux 账号；
- 通过一个 CLI 申请/释放/进入容器；
- 本地 GUI 只包装这个 CLI；
- 资源按 12–36 小时限时占用；
- 非管理员不能停止权限系统，不能释放/杀死他人的容器；
- 容器内是 root，但不能访问 host Docker/root；
- 未纳管进程和容器只显示，不自动杀。

不要做 Kubernetes、Slurm 替代品、Redis、Postgres、微服务、自动续租、自动调度或复杂配额系统。

---

## 1. 不可违反的约束

1. **单 CLI 原则**
   - CLI 是唯一业务实现。
   - GUI 只能调用 CLI 的 `--json` 输出，不得另写一套 API client、认证、权限或资源判断。
   - Broker 不向 GUI 直接暴露 HTTP；GUI 通过 SSH 调用远端 CLI。

2. **强制级别**
   - 成员独立 Linux 账号。
   - 成员不加入 `sudo`、`docker`、`citespec-gpu` 组。
   - 成员 SSH 使用 `ForceCommand`，只能执行 `citespec-resource` 白名单子命令。
   - 普通成员不能拿到普通 host shell。
   - 成员不能直接打开 `/dev/nvidia*`。
   - 容器内 root；容器外普通成员非 root。

3. **不可抢占**
   - 后来者只能申请空闲 GPU。
   - 非 owner 不能释放/停止他人 allocation。
   - 只有管理员可以强制释放。
   - Broker 启动/重启只管理带 `citespec.managed=true` 标签的容器；未知容器标记为“未纳管”，不杀。

4. **时间**
   - 默认 12 小时，最长 36 小时。
   - 到期 Broker 停止容器，状态 `expired`；`/workspace` 保留。
   - 不自动续租；需要继续就释放后重新申请。

5. **不碰研究仓库**
   - 本系统是独立基础设施仓库，不做 `reconstruction` / `optic_system` 的 submodule。
   - 不修改 Meta pin、不读取测试集、不操作实验机。
   - 不删除现有数据、容器或进程。

---

## 2. 推荐技术栈

保持部署简单：

- Python 3.11+
- Broker：`asyncio` Unix domain socket + 换行分隔 JSON；`sqlite3`；Docker SDK for Python
- CLI：Typer（或 argparse；若用 Typer，锁定版本）
- GUI：`citespec-resource gui` 启动本地 `127.0.0.1` Web UI；静态 HTML/CSS/JS；不引入 Node 构建
- 服务：systemd
- 数据：SQLite WAL
- 测试：pytest；FakeDocker + 可控时钟
- 不引入：FastAPI 公网服务、React/Vue 构建链、Redis、Postgres、Docker Compose、K8s

GUI 视觉可使用本地打包的轻量 CSS（如 Pico.css/Bootstrap 单文件），必须离线可用。

---

## 3. 架构

```text
本地工作站
  citespec-resource gui
      │  本地 127.0.0.1 Web UI
      │  subprocess: ssh <alias> citespec-resource ... --json
      ▼
example-gpu sshd
  ForceCommand /usr/local/bin/citespec-resource-login
      │  仅允许 citespec-resource 子命令
      ▼
citespec-resource CLI（以成员 UID 运行）
      │  Unix socket JSON，SO_PEERCRED
      ▼
citespec-resource-broker（root systemd）
      ├── SQLite
      ├── Docker / NVIDIA runtime
      └── nvidia-smi / ps 对账
```

没有公网 HTTP 端口，没有 GUI 后端 API，没有成员可访问的 Docker socket。

---

## 4. 账号、组与权限

### 4.1 Linux 账号

管理员为每个成员创建独立账号，例如 `citespec-<name>`：

- 主组：`citespec-members`
- 附加组：`citespec-cli`（访问 Broker Unix socket）
- 不加入：`sudo`、`docker`、`citespec-gpu`、`video`/`render`（若这些组能访问 GPU）
- 家目录只放配置；训练数据放 `/data1/citespec-users/<username>`
- SSH public key 由管理员加入

管理员组：`citespec-admins`。管理员可用普通 shell 进行维护；成员不行。

### 4.2 GPU 设备权限

创建系统组 `citespec-gpu`，但不把普通成员加入。安装 udev 规则，例如：

```udev
SUBSYSTEM=="nvidia", KERNEL=="nvidia*", GROUP="citespec-gpu", MODE="0660"
```

要求：

- Broker/root 可以访问 GPU；
- 成员在 host 上打开 `/dev/nvidia*` 应失败；
- 容器通过 Docker/NVIDIA runtime 获得分配卡；
- 安装后必须实测 `nvidia-smi`（host root）、容器内 `nvidia-smi`、成员 host 直接访问失败。

如果服务器不允许改 udev 或无法验证设备权限，**停止部署并报告**；不要声称已实现强强制。

### 4.3 SSH 受限 shell

`/etc/ssh/sshd_config.d/60-citespec-resource.conf`：

```sshconfig
Match Group citespec-members
    ForceCommand /usr/local/bin/citespec-resource-login
    AllowTcpForwarding no
    PermitTunnel no
    X11Forwarding no
    PermitTTY yes
```

`citespec-resource-login` 规则：

- 无 `SSH_ORIGINAL_COMMAND`：启动交互式 `citespec-resource menu`。
- 有命令：`shlex.split`，要求第一个 token 严格等于 `citespec-resource`；只执行白名单子命令。
- 不通过 `shell=True`，不做字符串拼接。
- 拒绝 `scp`、`sftp`、`bash`、`sh`、`python`、`systemctl`、`docker` 等。
- `sftp`/`scp` 在 v1 明确不可用；代码用容器内 `git`，数据已在服务器。

管理员账号不匹配 `Match Group citespec-members`，保留普通 shell。

---

## 5. Broker

### 5.1 进程与权限

- systemd unit：`citespec-resource-broker.service`
- 以 root 运行；`Restart=always`
- 只监听 `/run/citespec-resource/broker.sock`
- socket：`root:citespec-cli 0660`
- 不在公网/LAN 监听
- 日志：journald + 可选 JSON 文件
- 配置：`/etc/citespec-resource/config.toml`
- 数据：`/var/lib/citespec-resource/state.db`

### 5.2 身份

Broker 对每个 Unix socket 连接读取 `SO_PEERCRED`：

- 使用 peer UID 映射 `pwd.getpwuid`；
- 不信任客户端自报用户名；
- 成员身份由 UID 决定；
- 管理员权限由 `citespec-admins` 组成员决定；
- 操作审计记录 actor UID/username。

### 5.3 JSON 协议

换行分隔 JSON；请求：

```json
{"cmd":"apply","args":{"gpus":[3],"cpus":8,"memory_gb":32,"hours":12,"image":"citespec-base:2026.10","note":"..."}}
```

响应：

```json
{"ok":true,"data":{...}}
{"ok":false,"error":{"code":"gpu_busy","message":"GPU 3 already allocated"}}
```

CLI 的 `--json` 直接透传稳定结构；GUI 只消费这个结构。

---

## 6. 数据模型

SQLite，开启 WAL 和 foreign keys。

```sql
users(uid INTEGER PRIMARY KEY, username TEXT UNIQUE, role TEXT, created_at TEXT);

gpus(uuid TEXT PRIMARY KEY, index INTEGER, name TEXT, memory_mb INTEGER, state TEXT);

allocations(
  id TEXT PRIMARY KEY, owner_uid INTEGER, owner_name TEXT,
  status TEXT, image TEXT, cpus REAL, memory_gb INTEGER,
  workspace TEXT, note TEXT,
  created_at TEXT, starts_at TEXT, expires_at TEXT,
  container_id TEXT, container_name TEXT
);

allocation_gpus(allocation_id TEXT, gpu_uuid TEXT,
  PRIMARY KEY(allocation_id, gpu_uuid));

active_gpu_claims(gpu_uuid TEXT PRIMARY KEY, allocation_id TEXT UNIQUE);

events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor_uid INTEGER,
       action TEXT, allocation_id TEXT, detail TEXT);
```

规则：

- `active_gpu_claims` 是 GPU 独占的唯一事实来源；申请时在事务内插入，释放/到期删除。
- 同一 GPU 同时只能有一个 active claim。
- 不依赖客户端传入 owner；owner 来自 peer UID。
- 状态：`pending → active → released | expired | failed`。
- 所有时间存 UTC ISO8601；显示用服务器本地时区。

---

## 7. Allocation 生命周期

### 7.1 Apply

1. 校验 `hours` 在 `[1,36]`，默认 12。
2. 校验 GPU 存在、无 active claim、`nvidia-smi` 无未纳管 compute process。
3. 校验请求 CPU/RAM 不超过配置上限。
4. 写 `pending` + `active_gpu_claims`（同一事务）。
5. 创建 workspace（若不存在）。
6. `docker run -d`，带标签和资源限制。
7. 成功后写 `active`、container_id、container_name。
8. 失败：删除 container（若已创建）、回滚 claim、状态 `failed`、写事件。

### 7.2 Release

- owner 或 admin。
- `docker stop` + `docker rm`（或 stop 后保留日志，按实现固定）。
- 删除 claim，状态 `released`。
- workspace 不删除。

### 7.3 Expiry

- Broker 内每 30 秒检查一次 `expires_at`。
- 到期：停止/删除容器，删除 claim，状态 `expired`，写事件。
- 到期前 10 分钟在 `list`/GUI 显示警告；不自动续租。
- 允许 `expires_at` 前手动 release。

### 7.4 Broker 重启对账

启动时：

- `pending` 超过 5 分钟：标记 failed，清理 claim。
- `active`：
  - container 存在且未到期：保持。
  - 已到期：停止，标记 expired。
  - container 丢失：标记 failed/lost，释放 claim。
- 未知容器（无 `citespec.managed=true`）：只显示为 unmanaged，不停止、不删除。

---

## 8. 容器规格

Broker 创建的容器必须：

- `--gpus device=<uuid-or-index>`，只暴露分配的整卡；
- `--cpus <n>`、`--memory <n>g`、`--memory-swap` 与 memory 相同（禁止 swap 扩张）；
- `--name citespec-<user>-<allocation-short>`
- 标签：
  - `citespec.managed=true`
  - `citespec.owner=<username>`
  - `citespec.allocation=<id>`
  - `citespec.gpus=<comma-separated>`
- 工作目录 `/workspace`，挂载 `/data1/citespec-users/<username>:/workspace`
- 不加 `--privileged`
- 不挂 `/var/run/docker.sock`
- 不使用 host PID/IPC/network
- 不暴露 host 端口
- 默认 root 用户；允许容器内 `apt/pip`
- 可加 `--security-opt no-new-privileges`，但不要 drop 到无法安装软件的程度

基础镜像由管理员构建并固定 tag，例如 `citespec-base:2026.10`；用户在容器内安装的环境不持久化，代码/数据放 `/workspace`。

---

## 9. CLI 规格

包名建议：`citespec-resource`；入口点：`citespec-resource`。

```text
citespec-resource list [--json]
citespec-resource status [<id>] [--json]
citespec-resource apply --gpus 3,5 --cpus 16 --memory 64 --hours 12 [--image ...] [--note ...] [--json]
citespec-resource release <id> [--json]
citespec-resource shell <id>
citespec-resource logs <id> [--follow]
citespec-resource menu
citespec-resource gui [--server example-gpu] [--port 0]
```

管理员：

```text
citespec-resource admin users
citespec-resource admin resources
citespec-resource admin release <id> --reason ...
citespec-resource admin audit [--limit 100]
citespec-resource admin reconcile
citespec-resource admin mode monitor|active
```

退出码：

- `0` 成功
- `2` 参数错误
- `3` 资源冲突/忙碌
- `4` 权限拒绝
- `5` 已过期/不存在
- `6` Broker 不可达
- `7` Docker/内部错误

`--json` 输出必须稳定、可被 GUI 直接解析。

---

## 10. GUI 规格

`citespec-resource gui`：

- 启动本地 `127.0.0.1` Web server（随机端口）；
- 自动打开浏览器；
- 配置中读取 SSH alias（默认 `example-gpu`）；
- 所有数据通过 `ssh <alias> citespec-resource ... --json` 获取；
- 不直接访问 Broker，不新增 API client。

页面：

1. **总览**
   - 8 张 GPU 卡片；
   - 每卡：index、空闲/占用、owner、allocation id、开始/到期、剩余时间；
   - CPU/RAM 汇总；
   - 过期警告（<10 分钟高亮）。
2. **申请**
   - 选择空闲 GPU、CPU、RAM、12–36 小时、镜像、备注；
   - 前端只做基本校验，最终以 CLI/Broker 为准；
   - 错误直接显示 CLI 的 error code/message。
3. **我的 allocation**
   - 状态、剩余时间、release、shell、logs。
4. **管理员页**
   - 仅当 `admin` CLI 成功时显示；
   - 强制释放、用户/资源、审计；
   - 不提供任意 shell/系统命令。
5. **未纳管**
   - `nvidia-smi`/`ps` 发现的非 Broker 进程：GPU、PID、系统用户、命令摘要；
   - 只读，不提供 kill 按钮。

GUI 轮询间隔 5 秒；失败时显示离线状态，不缓存过期占用为“空闲”。

视觉要求：GPU 卡片、清晰颜色、倒计时、响应式；本地静态资源，不依赖 CDN。

---

## 11. Server 安装与部署

### 11.1 仓库

独立仓库，建议名 `citespec-resource`，不要作为研究 repo 的 submodule。

```text
citespec-resource/
  pyproject.toml
  README.md
  broker/
    __init__.py
    server.py
    db.py
    docker_driver.py
    expiry.py
    reconcile.py
  cli/
    __init__.py
    main.py
    client.py
    login_wrapper.py
  gui/
    static/index.html
    static/app.js
    static/app.css
    server.py
  deploy/
    systemd/citespec-resource-broker.service
    udev/99-citespec-gpu.rules
    sshd/60-citespec-resource.conf
    install_server.sh
    create_user.sh
    docker/Dockerfile.base
  tests/
  docs/
```

### 11.2 安装顺序

`install_server.sh` 必须幂等、可 dry-run，并且：

1. 只读预检 Docker/systemd/NVIDIA/udev/sshd/组/端口；
2. 创建 `citespec-members`、`citespec-cli`、`citespec-admins`、`citespec-gpu`；
3. 安装 udev 规则并 `udevadm control --reload && udevadm trigger`；
4. 安装 systemd unit；
5. 安装 sshd drop-in，先 `sshd -t` 再 reload；
6. 初始化 SQLite；
7. **默认 `mode=monitor`**：只读显示，不创建/停止容器；
8. 不做任何 GPU 分配，不杀现有容器。

### 11.3 部署闸门

当前 example-gpu 在训练，**禁止部署**。训练结束后：

1. 先 monitor-only；
2. 确认现有容器全部显示 unmanaged 且 Broker 未碰它们；
3. 管理员用一个测试账号做 CPU-only allocation；
4. 再做单 GPU allocation；
5. 最后开放成员。

任何阶段出现 unidentified container 被操作、GPU 被错误抢占、非 owner 成功 release，立即停止并回滚。

---

## 12. 测试与验收

### 单元测试（不依赖 GPU）

- 时长边界：0、1、12、36、37 小时；
- GPU 冲突：两个申请同一卡，第二个必须失败；
- owner 检查：非 owner release 返回 403；
- admin 强制 release；
- 到期：可控时钟，容器 stop，claim 删除；
- Docker 创建失败：回滚 claim/状态；
- Broker 重启：active/pending/expired 对账；
- 未知容器不删除；
- 命令解析：`;`、`&&`、`|`、`bash -c`、`scp` 被拒绝。

### 集成测试（本地 Docker，CPU）

- 创建 CPU 容器、进入 shell、release；
- 两个并发 apply 的竞态；
- 过期自动停止；
- Broker 重启后列表正确；
- Socket 权限：非 `citespec-cli` 连接失败；
- Peer UID 不能伪造。

### 安全验收

- 成员 `ssh example-gpu bash` 被 ForceCommand 拒绝；
- 成员 `docker ps` 无权限；
- 成员 `/dev/nvidia*` 打开失败；
- 容器内不能访问 `/var/run/docker.sock`；
- 容器内不能看到 host PID；
- 非 owner 不能 release；
- 普通成员不能 `systemctl stop citespec-resource-broker`；
- GUI 只通过 CLI，且不显示 kill 未知进程。

### 实机验收（训练结束后）

- monitor-only 10 分钟，现有容器无变化；
- 单 GPU allocation：容器内 `nvidia-smi` 仅见分配卡；
- 到期/释放后 GPU 回到空闲；
- workspace 保留；
- 审计日志完整。

---

## 13. 分阶段执行提示

新会话按阶段实现，每阶段结束提交代码和测试；**不要直接部署到 example-gpu**。

### P0 — 仓库与只读预检

- 创建独立 repo；
- 写只读预检脚本；
- 输出环境报告；
- 不安装、不改 udev/sshd/systemd。

### P1 — Broker + SQLite + CLI + Peer UID

- UDS JSON 协议；
- `SO_PEERCRED`；
- 用户/管理员权限；
- list/status/apply 的 DB 事务（先 FakeDocker）；
- 单元测试。

### P2 — Docker 生命周期

- Docker driver；
- 容器标签/限制/挂载；
- 到期 worker；
- 重启对账；
- CPU 集成测试。

### P3 — 强制与安装脚本

- udev / sshd / systemd / 组；
- `install_server.sh` dry-run；
- `create_user.sh`；
- monitor-only；
- 安全验收脚本。

### P4 — GUI

- `citespec-resource gui`；
- 本地静态 UI；
- 调用 CLI `--json`；
- 不新增 API；
- 使用 Fake CLI 测试解析/错误。

### P5 — 实机验收（历史里程碑）

- 训练结束后，管理员批准；
- monitor-only；
- CPU → 单 GPU；
- 记录所有命令和结果。

---

## 14. 明确禁止

- 不要在 example-gpu 训练期间部署或重启 Docker。
- 不要杀未知容器/进程。
- 不要给成员 Docker socket、sudo 或 GPU 设备组权限。
- 不要用 `--privileged`。
- 不要挂 host root / `/var/run/docker.sock`。
- 不要做自动续租、抢占、自动迁移。
- 不要改研究仓库、测试集、Meta pin 或硬件代码。
- 不要把 monitor-only 模式说成已强制启用。
- 不要在没有 udev/设备权限验证时声称“强强制”已完成。

---

## 15. 当前环境与待确认

当前已知：

- example-gpu：GPU 服务器（具体型号、卡数、CPU/RAM 规格不在此公开文档记录）；当前全部 GPU 被其他任务占用。
- 部分 GPU 已被其他任务使用；本系统不依赖当前空闲卡。
- 服务器曾因主机内存不足崩溃；容器必须加 memory/swap 限制，Broker 不得再造成 OOM。

待管理员确认后填入配置：

- 管理员账号/组名单；
- 12h 默认、36h 上限是否对所有人生效；
- 基础镜像 tag；
- workspace 根路径；
- 是否允许管理员在到期前手动延长（v1 默认不允许自动续租）。

---

## 16. 完成定义

只有当以下全部满足，才可称为 v1：

- 一个成员账号能申请、进入、释放；
- 非 owner 不能释放他人；
- 到期自动停止且 workspace 保留；
- 非管理员不能停 Broker/杀容器；
- 成员无法直接使用 GPU 或 Docker；
- GUI 完全通过 CLI 工作；
- monitor-only 对现有训练容器零影响；
- 全部单元/集成/安全测试通过；
- 部署过程可由一名管理员在 15 分钟内完成。
