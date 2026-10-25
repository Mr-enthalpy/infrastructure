# 会话交接

## 1. 基线信息

- 代码基线：`d4cf3a8`（`Keep storage preflight tests off the global os module`）。
- 本文档与仓库文档整理位于同一后续提交；开始新会话时以 `git log -1` 和
  `git status` 为准。
- 代码基线 `d4cf3a8` 对应的 GitHub Actions CI：run `40068346418`，六个 job 全部
  通过。
- 代码基线 `d4cf3a8` 的本地验证：`243 passed, 18 skipped`；Ruff check/format
  通过；mypy 通过。
- 当前分支：`main`；远程：`origin`。
- amax 未被访问、未被部署，未执行 Docker/NVIDIA 重启、容器操作或验收测试。

本地开发环境说明：Windows 上运行 Python 3.13；Git Bash 位于
`C:\Program Files\Git\bin\bash.exe`。Docker 和 `uv` 在本地不可用；WSL 未安装发行版。
Linux-only 的 UDS、`SO_PEERCRED`、Docker、NVIDIA、quota、sshd-in-container 和
systemd hardening 测试必须在目标机或 Ubuntu CI 执行。

## 2. 目标与当前阶段

目标：为 UDC 实验室共享 GPU 服务器建立受约束的资源管理器，完成可丢弃开发容器
工作流，并在 Broker 中介下提供 VS Code 接入；在全部安全闸门通过前不进行 amax
部署。

当前阶段：

- 代码实现和安全加固已完成，包含 Broker、CLI、GUI、Docker driver、队列、
  开发容器 profile、容器 SSH、Broker 隧道和部署脚本。
- 本地测试和 CI 已通过。
- P5 实机验收尚未开始。
- amax 仍用于训练。训练结束和管理员明确批准是继续目标机工作的前置条件。

## 3. 已完成的实现

- 配置与运行时闸门：
  - active 模式要求 `deployment_allowed = true`，并通过 legacy 审计。
  - active 模式要求 `workspace_quota_required = true` 且
    `max_workspace_gb > 0`。
  - workspace 路径必须是绝对路径、非根路径、无 `..`，且不能是规范化后的根路径。
  - development profile 与 restricted profile 的配置一致性校验。
- GPU 权限：
  - 连续 GPU 权限审计在 Broker 启动、切换到 active、周期检查和设备指纹变化时
    执行。
  - 审计失败阻止新分配，但保留既有任务和安全的释放/到期路径。
  - 设备指纹覆盖 `/dev/nvidia*` 及 `/dev/nvidia-caps/nvidia*`。
- 存储与配额：
  - `preflight.sh` 和 `DockerDriver.storage_preflight` 对 development profile
    强制检查 `overlay2`、XFS、`prjquota`/`pquota`、`xfs_quota` 和 workspace
    `nosuid`。
  - Broker 在队列准入前重新检查存储闸门。
  - 支持 XFS 和 ext4 项目配额；不使用 UID 配额。
  - Broker 在每次 allocation 物化前校验成员 workspace 的实际 project ID
    （lsattr）、新文件继承（探针文件）和硬配额（xfs_quota report /
    repquota 解析）；未生效时自动重新开通并复查，仍失败则分配失败。
  - `create_user.sh` 在建号时执行完整的 workspace 开通事务（建目录 → 项目
    ID → 硬配额 → 查询验证），可用 `--no-workspace` 跳过。
  - Docker 存储目录整机容量闸门：`policy.min_free_docker_gb`（默认 20 GiB）
    不足时阻止新分配，既有任务与释放/到期不受影响。
- 队列：
  - 顺序为 `priority DESC, created_at ASC, rowid`（插入序），且队列空检查位于
    `create_allocation` 的数据库事务内。
  - 受约束 backfill：被阻塞请求封锁其申请的 GPU，后续请求只能在 GPU 完全不
    相交时越过；等待超过 `queue_backfill_reserve_minutes` 后停止 backfill。
  - Docker 全局可写层预算：`docker_storage_budget_gb` 按每个 pending/active
    allocation 的 `writable_layer_gb` 预留，防止多个配额容器共同写满磁盘。
  - 严格队首阻塞；后续请求不得越过未过期队首。
  - `--no-queue` 表示不等待、不保留请求，但不得越过队列。
  - `queue_enabled = false` 仅阻止新排队请求，不允许直接申请绕过已有队列。
- Docker 与管理容器：
  - development profile：成员 UID、容器内 passwordless sudo、可写 rootfs、
    临时 HOME（默认可写层内）、持久 `/workspace`、`storage_opt` 可写层配额、
    PID 限制。
  - GPU 容器显式设置 `NVIDIA_VISIBLE_DEVICES=<分配的 UUID 列表>`，CPU-only
    容器设置 `NVIDIA_VISIBLE_DEVICES=void`；CUDA 基础镜像继承的
    `NVIDIA_VISIBLE_DEVICES=all` 不再与 DeviceRequests 取并集，
    `docker inspect` 解析以 DeviceRequests 为权威。
  - 禁止 privileged、Docker socket、host PID/network namespace、host 系统挂载。
  - managed container 仅在 DB container ID、allocation 状态、owner 和 GPU UUID
    集合全部匹配时才豁免 unmanaged-GPU 检查。
- 容器 SSH 与 VS Code：
  - 容器身份初始化（/etc/passwd、/etc/group、HOME）在 allocation 创建时执行，
    容器只有初始化成功后才转入 active；VS Code 准备流程只负责 SSH 密钥与
    连接配置（幂等重跑身份脚本作为防御）。
  - 默认 HOME 位于容器可写层内（受 storage_opt 配额约束、随容器销毁）；
    `container_home_tmpfs_gb > 0` 时改为等容量 tmpfs。
  - 容器身份初始化同时建立与宿主 udc-gpu 同 GID 的容器内补充组并把成员加入
    （sshd 登录从容器 /etc/group 重建补充组，docker group_add 只覆盖 exec 路径）；
    验收要求两条登录路径分别检查 `id -G`、`nvidia-smi`、`torch.cuda`。
  - GUI VS Code 按钮通过 `--readiness-file` JSON 协议等待真实就绪
    （listening/tunnel_verified/ready/error），并显式传递配置路径；不再假成功。
    隧道全程 try/finally：探测失败也会清理 SSH 配置块、监听器和子进程。
    code CLI 缺失时不写 ready，readiness 带 message，GUI 展示可复制的 URI。
  - GUI 提供 job 启动（整条 shell 命令原样传递，容器内以 `bash -lc` 执行）与
    `/workspace/.udc-logs/` 持久日志查看；release 后仍可列出/读取历史日志
    （`job files`、`job_logs` 不要求 active）。
  - CLI 支持 `--remote <ssh-alias>` 把常规命令中继到服务器执行（本地交互命令
    除外）；VS Code 隧道就绪前执行真实 SSH 探测（容器内 `/bin/true`），身份
    优先继承 `ssh -G <alias>` 解析的 IdentityFile。
  - 容器 sshd 使用 inetd 模式，由 Broker 通过 Docker exec 隧道启动。
  - Host SSH 保持 key-only、`ForceCommand` 限制和转发禁用。
  - 容器 sshd 仅允许 local TCP forwarding，用于 Remote-SSH；remote、Unix-socket、
    agent forwarding 和 tun 设备保持禁用。
  - 本地 VS Code 监听器绑定 `127.0.0.1`；回环别名关闭 host-key 验证，本地账号是
    信任边界。
  - 容器 SSH 准备会重写/复用成员 UID 的 passwd 条目，拒绝 UID 0 隧道，并在
    `flock` 可用时串行化。
- 部署：
  - `install_server.sh` 事务化安装，包含备份、激活前校验和回滚。
  - member shell 为 root-owned；`PermitUserRC no` 阻止 `.bashrc`/`.ssh/rc` 在
    `ForceCommand` 前执行。
  - 磁盘告警和日志轮转 timer 默认启用，可用 `--no-timers` 关闭；仅在审计和
    Broker 启动成功后启用。
  - 脚本安装使用 `O_NOFOLLOW`、目录 fd 链、普通文件检查和 CRLF 归一化。
- 文件传输：
  - 已实现受限 `push`/`pull` 和 `file put`/`file get`，但保持禁用，直到 TOCTOU
    安全处理、项目配额和 target-host 测试通过。

## 4. 当前安全不变量

1. amax 上的训练、现有容器和资源分配不得被中断。未获得明确批准时，不部署、
   不重启 Docker/NVIDIA、不修改 udev 规则、不创建或停止容器。
2. 成员 SSH 仅允许密钥认证，并通过 `ForceCommand` 限制为白名单 CLI；不得暴露
   host shell 或 SSH 转发。
3. 成员不得属于 `sudo`、`docker`、`udc-gpu`、`video` 或 `render` 组。
4. 容器内 sudo 获得的 UID 0 在未启用 user namespace 时对应宿主 UID 0。必须依赖
   无 host 挂载、无 Docker socket、无 host namespace 和 workspace `nosuid` 维持
   边界；不得以 `userns_mode = "host"` 绕过验证。
5. workspace 必须使用目录/项目配额；active 模式必须有可用配额。
6. GPU 审计失败、配额检查失败或存储预检失败时，新分配必须被阻止；既有任务必须
   仍可释放和到期。
7. 队列顺序不可绕过；`--no-queue` 不是插队机制。
8. 文件传输和 VS Code 在对应 target-host 验收完成前保持禁用。示例配置中
   `file_transfer_enabled = false`，`vscode_enabled = false`。
9. 成员目录仅暴露 `display_name`、`nickname` 和可选 `public_contact`；不得暴露
   私人联系方式、学号或其他敏感字段。
10. 不得删除或覆盖未知容器、他人 workspace 或现有 allocation。

## 5. 目标机待验证项

以下项目无法在 Windows 本地完成，必须在获得批准后于目标机验证：

- Docker 实际存储驱动是否为 `overlay2`，DockerRootDir 是否位于 XFS，以及
  `prjquota`/`pquota` 是否可用。
- workspace 文件系统是否以 `nosuid` 挂载；若未挂载，development profile 和运行时
  存储闸门必须保持阻断。
- 每个成员 workspace 的项目配额是否实际生效；`configure_quotas.sh` 在新增成员后
  是否已重新运行。
- `userns-remap` 与 GPU 设备、bind mount 的交互；当前默认不启用，且不得未经验证
  启用。
- 容器内 sshd 的 inetd framing、UID 1000 passwd 重写、成员密钥登录和隧道释放。
- VS Code Remote-SSH 的本地监听、连接、并发槽位限制和 release/expiry 清理。
- systemd `ProtectHome=read-only`、`ProtectSystem=strict`、Docker socket 访问以及
  GPU 审计中的 `su` 在 unit hardening 下是否正常工作。
- udev 规则应用后，现有 `/dev/nvidia*` 属主/权限是否立即变化，以及是否影响正在
  运行的训练容器。
- GPU 审计在 reboot 和 NVIDIA driver reload 后是否仍通过。
- 容器可写层配额、临时 HOME（默认在可写层内；`container_home_tmpfs_gb > 0` 时为
  tmpfs）、持久 `/workspace` 和 release/re-apply 行为。
- 文件传输的 TOCTOU、路径逃逸、symlink、配额和中断恢复测试。
- 成员配额闭环的解析在真实 XFS/ext4 上的行为：`lsattr -p` 的 project ID、
  继承探针文件、`xfs_quota report` 的块单位（代码按 512B/1K/4K/1M 容错）、
  `repquota -P` 的输出格式。
- GUI VS Code readiness 文件协议（含 `tunnel_verified` 阶段的容器内 SSH 探测）、
  job 启动/日志端点，以及 Docker 容量闸门（`min_free_docker_gb`、
  `docker_storage_budget_gb`）在真实主机上的阈值行为。
- 队列 backfill 与饥饿保护在真实多用户并发下的行为（逻辑已在 DB 层测试，但
  实机 clock/GPU 状态变化更复杂）。
- VS Code 会话内的 GPU 设备访问：`id -G` 含 udc-gpu GID、`nvidia-smi -L`、
  `torch.cuda.is_available()`；与 docker-exec 路径分别验收（见 acceptance 5.5）。

## 6. 下一会话执行顺序

### 步骤 1：确认基线

```bash
git status --short
git log --oneline -5
```

确认工作区干净、当前提交包含文档整理，并阅读本文件、
[`docs/acceptance.md`](acceptance.md)、[`deploy/README.md`](../deploy/README.md)。

### 步骤 2：本地验证

在 Windows 或 Linux 开发环境运行：

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m mypy src
```

在 Git Bash 中检查部署脚本：

```bash
bash -n deploy/preflight.sh
bash -n deploy/install_server.sh
bash -n deploy/verify_monitor_only.sh
```

### 步骤 3：等待明确批准

在以下条件全部满足前停止：

- 训练已结束；
- 管理员明确批准部署或目标机只读验收；
- 已确认不会影响现有容器、GPU 分配和其他用户；
- 已确定测试 GPU、测试成员账号和维护窗口。

“继续”或其他泛化指令不构成部署批准。未获得明确批准时，只进行本地只读审查和
测试。

### 步骤 4：只读目标机检查

获得只读验收批准后，按顺序执行：

```bash
bash deploy/preflight.sh --config lab/resources.local.toml
bash deploy/audit_legacy.sh --production
bash deploy/verify_gpu_permissions.sh --json
bash deploy/verify_monitor_only.sh --member <member> --skip-snapshot
```

同时保存：

```bash
docker info --format '{{.Driver}}|{{.DockerRootDir}}'
findmnt -no TARGET,FSTYPE,OPTIONS -T /data1/udc-users
systemctl show udc-resource-broker \
  -p ProtectHome -p ProtectSystem -p ReadWritePaths -p NoNewPrivileges
xfs_quota -x -c 'report -p -b -h' /data1
```

上述命令均为只读。若 `preflight.sh`、GPU 审计、`nosuid` 或配额检查失败，停止，
不得继续安装或切换到 active。

### 步骤 5：完整验收

只有在训练结束并获得完整目标机验收批准后，才可执行
[`docs/acceptance.md`](acceptance.md) 中的流程。验收期间：

- 使用专用测试 GPU 和测试账号；
- 不触碰训练容器或其他用户的 allocation；
- 记录镜像 digest，不使用浮动 tag；
- 在容器 sshd、隧道、配额和 release/expiry 测试通过后，才考虑启用
  `vscode_enabled`；
- 在 TOCTOU、配额和 target-host 文件传输测试通过后，才考虑启用
  `file_transfer_enabled`；
- 保留所有命令输出、`docker inspect` 安全字段、审计输出和操作者信息。

## 7. 明确禁止的操作

在未获得明确批准时，不得执行：

- `install_server.sh --yes` 或任何会修改主机的 `deploy/` 脚本；
- 重启 Docker、NVIDIA 驱动或相关 systemd 服务；
- 应用 udev 规则或修改 `/dev/nvidia*` 权限；
- 创建、停止、删除或重新配置现有容器；
- 修改现有 allocation、workspace、队列或数据库；
- 将 `lab/resources.local.toml`、真实地址、密钥或私人联系方式提交到 Git；
- 在目标机上运行 `acceptance_active.py`；
- 将 `vscode_enabled` 或 `file_transfer_enabled` 设为 `true` 以绕过验收。

## 8. 关键文件

- `src/udc_resource/broker/server.py`：运行时闸门、严格 managed 匹配、队列公平性、
  隧道生命周期、密钥读取、对账/到期钩子。
- `src/udc_resource/broker/docker_driver.py`：development profile、存储/`nosuid`
  预检、容器 SSH passwd/key 准备、隧道 demux。
- `src/udc_resource/config.py`：路径校验、active/quota 策略闸门。
- `src/udc_resource/workspace_quota.py`：XFS/ext4 项目配额闸门。
- `src/udc_resource/gpu_audit.py`：GPU 审计运行器和设备指纹。
- `src/udc_resource/cli.py`：`tunnel`、`vscode`、managed SSH 配置、子进程清理。
- `src/udc_resource/gui/server.py`：队列/job/VS Code 路由和子进程生命周期。
- `deploy/preflight.sh`：Docker 存储、`nosuid`、配额和 profile 检查。
- `deploy/verify_monitor_only.sh`：只读 monitor-only 安全验收。
- `deploy/verify_gpu_permissions.sh`：成员 GPU 权限审计，支持 JSON。
- `deploy/install_server.sh`：事务化安装、timer、成员 shell、udev 回滚。
- `deploy/configure_quotas.sh`：XFS/ext4 项目配额配置。
- `deploy/docker/Dockerfile.base`、`docker/sshd_config`、`docker/bootstrap.sh`：
  开发容器镜像和容器内 SSH/引导配置。
- `tests/test_docker_driver.py`：容器 SSH、存储预检和 UID 冲突测试。
- `tests/test_config.py`：workspace 路径和 active/quota 校验测试。
- `docs/acceptance.md`：目标机验收清单。
- `docs/queue.md`：队列状态和公平性语义。
- `docs/jobs-and-transfer.md`：job 和受限文件传输。
- `docs/legacy-access-revocation.md`：legacy 管理员权限撤销。

## 9. 已知限制与风险

- 开发容器在没有 user namespace 时，容器 root 映射到宿主 root；当前安全性依赖
  Docker 隔离和 `nosuid` workspace。不得添加 host 挂载或 Docker socket。
- `install_server.sh` 仅对非默认 `workspace_root` 或 `state_db` 发出警告；自定义
  路径必须手工更新 systemd `ReadWritePaths`。
- 新增成员后需要重新运行 `configure_quotas.sh`；当前没有自动分配每个成员项目配额
  的流程。
- 应用 udev 规则可能改变正在运行的训练容器的设备属主/权限；必须在训练结束后获得
  批准再执行。
- 本地 Windows 测试不能验证 Linux UDS、Docker、NVIDIA、quota、sshd-in-container
  和 systemd hardening。
- 容器内 UID 1000 passwd 重写已在代码中修复，但仍需 Linux 目标机验证。
- `vscode_enabled` 和 `file_transfer_enabled` 在示例配置中保持 `false`；任何启用
  都必须以目标机验收证据为依据。
