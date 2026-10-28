# 会话交接

> **成员自助接入与管理闭环（本轮）**：权威成员状态/审批/开户/撤权已实现
> （`membership/` + `enrollment/` + `onboarding.py`，迁移 3/4，
> `membership_required` 默认 true）。成员公钥改为 root 中央目录；审批绑定申请
> 版本；操作与队列/apply 幂等并可重试；legacy import 默认 dry-run。管理员 CLI：
> invitations/requests/members/operations；GUI 新增申请、成员、操作详情、密钥与
> 生命周期面板；intake 服务、固定动作 root 执行器和 systemd 单元在 `deploy/`。
> `setup` 可在没有服务器 TOML 的工作站上使用，并生成仅连接信息的客户端配置。
> 三份文档：docs/guides/member-quickstart.md、docs/guides/admin-membership.md、
> docs/reports/member-onboarding-and-ux.md（含差距表、实际测试结果与
> **D1=完成 / D2=核心实机验收完成 / D3=未开放** 结论）。

## 0.4 2026-10-27 部署阻断项与测试服务器准备（PR #1）

- 分支 `fix/p1-pre-deploy`；客户端版本提升为 `0.1.1`，`v0.1.0` tag/资产不变。
- 安装器改为版本化 venv + staging smoke test + 原子 `.venv` 切换；已 active 的 unit
  需要 `--maintenance-window`，安装器先停写入者，再用 `backup.py --no-quiesce`
  生成并校验一致快照；回滚恢复旧 venv 和快照。
- 备份 unit 增加 state/intake/portal 目录的 `ReadWritePaths`，使
  `wal_checkpoint(TRUNCATE)` 在 `ProtectSystem=strict` 下可完成。
- 客户端 `install.sh --verify` / `install.ps1 -Verify` 改为强制。
- 非默认 `workspace_root` 通过 broker、logrotate、disk-check、provision 四份
  `10-workspace.conf` drop-in 生效（路径列表、ExecStart、环境变量），不再硬失败。
- Portal 安装后永不 enable；unit 以
  `/etc/citespec-resource/portal-network-accepted` 为启动条件。新增
  `portal_network_audit.py`、`verify_portal_network.sh --acceptance` 和
  `enable_portal.sh`，审计通过后才写标记并 `systemctl enable --now`。
- 新增 `verify_gpu_isolation.sh` 真机 P0 测试；active + development +
  container sudo 在 `/etc/citespec-resource/gpu-isolation.ok` 缺失时被 config、
  preflight 和 broker 拒绝。preflight 默认不调用 `nvidia-smi`，安装器在 wrapper
  完成后执行一次 `--probe-nvidia --nvidia-only`。
- 复审追加硬化：复用已有 `citespec-portal` 账号前 fail-closed 校验系统 UID、主组、
  nologin shell、锁定密码、无 sudo、仅 `citespec-cli` 附加组；Broker 启动时复核
  同一规则。维护窗口检查移到所有组/账号变更之前。GPU 隔离脚本在首次 chmod 前
  设置 `MUTATION_STARTED`，中断或异常退出也恢复并复核设备状态。
- 已知残余边界（P2）：应用仍信任来自 loopback 的 `X-Citespec-Client-IP`，宿主机
  本地用户可直接连接 127.0.0.1 伪造审计 IP/限流键；开放 Portal 浏览器入口前需改为
  权限受限 Unix socket 或可验证代理身份。Portal 保持禁用，不阻塞 monitor 升级。
- 本地验证：`563 passed, 27 skipped`；Ruff、mypy、`bash -n deploy/*.sh` 通过。
  尚未连接真实主机；一次性 VM 回滚演练、测试服务器 monitor 部署、备份/恢复、
  Portal 网络验收和 GPU 5.6 真机测试仍需维护窗口。

## 0.3 2026-10-27 打包客户端、自动更新与 CI

- 新增 `packaging/`：Windows/Linux x64 的 PyInstaller payload、稳定启动器、full zip、
  per-user 安装脚本、版本检查和签名清单生成；发布流程在 `v*` tag 上构建并附加
  GitHub Release 资产。
- 客户端使用 Ed25519 签名清单、SHA-256、zip-slip/符号链接/解压大小限制和原子
  `versions/<version>` 安装；保留上一版本并在失败时回退。更新锁在崩溃 6 小时后
  自动恢复；不完整版本目录只在新 payload 校验通过后替换。
- 启动器按已安装 payload 版本比较，而不是按稳定启动器自身版本；`start` 自动中继，
  `version --json` 与 broker `status`/`list` 带协议 handshake；GUI 对协议不兼容显示
  警告但不阻断文本 CLI。
- 2026-10-27 本地结果：完整测试 `475 passed, 21 skipped`；Portal loopback
  `tests/test_portal.py tests/test_backup.py` 为 `25 passed`；Ruff、mypy、JS、Bash
  和 PowerShell 语法检查通过。Windows x64 包完成 `version --json`、签名更新
  `0.1.0 -> 0.1.1`、重复检查不重装、篡改清单/离线回退 smoke test。
- CI：`40465970858`（Windows/Linux、Python 3.11/3.12/3.13）通过；
  `40465970936`（Windows/Linux 打包、Linux `install.sh` 与启动器 smoke test）通过。
  GitHub Actions secret `CITESPEC_UPDATE_SIGNING_KEY` 已配置。
- `v0.1.0` 已合并到 `main` 并发布签名 Release：release run `40485285111`，
  Windows/Linux x64 payload 和 full zip、签名 manifest 均已上传。已下载发布资产，
  验证 manifest 签名、两个平台 payload 的 SHA-256/大小，并在 Windows full zip 上
  确认 `version --json`（protocol 1）和默认 GitHub 更新源返回 `already up to date`。
- 生产 Portal 仍未启用。下一步是网络维护窗口完成 DNS/VPN/TLS/防火墙现场验收并
  启用 Portal；首个版本由成员安装/更新后记录验收结果。不得在文档或仓库提交私钥、
  真实 IP/端口/管理员信息。

## 0. 2026-10-26 <server-instance> 实机部署与 active 验收

> 本节记录实机结果；本轮代码改动见 cutover 提交，文档校准见本次提交；§1–§9
> 保留实现/验收清单与当前状态。

- 目标机：`<server-address>`（SSH 端口 `<ssh-port>`），实际部署在<server-instance>；
  example-gpu 未改动。
- 管理员：`<admin-account>` 使用普通 shell，主组 `citespec-admins`（附加组 `sudo`、
  `citespec-cli`），**不属于 `citespec-members`**；root bootstrap 公钥已移除，
  `ROOT_KEYS_ABSENT`。
- 存储与 profile：workspace 根 `/home/data1/citespec-users`，ext4 + `prjquota`；
  DockerRootDir `/home/data2/docker-data` 为 ext4（非 XFS+prjquota），因此
  `container_profile = "restricted"`；配额闸门保持启用。
- GPU/镜像：GPU 服务器；当前基镜像 `citespec-base:2026.10`（ID `566d0e599e2e`，
  约 25.2 GiB；在旧基镜像上叠加一个小层，仅收敛可见的 sudoers 文件名；镜像站
  `docker.mirrors.ustc.edu.cn` 已不可解析）；NVIDIA Docker runtime 可用；
  `citespec-gpu-permissions.service` active。旧基镜像标签已在最终验收后删除。
- 服务：`citespec-resource-broker`、`citespec-resource-intake`、GPU 权限服务和 nginx
  active；provision service 由 `.path` 按需触发。Broker 持久化 `mode=active`。
- 本地 TLS：nginx 监听 `0.0.0.0:80/443`，HTTP 301 到 HTTPS，
  `https://127.0.0.1/` 返回 200；自签证书 CN/SAN 为 `<server-address>`
  （仅 IP，无公网 CA）。
- 实机验收：`acceptance_active.py` **PASSED**，覆盖 preflight、真实 SSH 拒绝与
  白名单 CLI、并发 apply 只一个赢家、排队/取消/`queue wait` 只在 active 返回、
  unmanaged GPU holder 被拒绝/排队、文件传输按策略拒绝（`forbidden`）、
  expiry/restart/断线后 workspace 保留且 bystander 不受影响。
- 本地浏览器 onboarding：使用一次性成员完成了 intake 申请、审批和开户路径；
  测试成员 A/B 随后已撤销，Linux 账号、workspace、项目配额、验收公钥和
  本地/root 验收别名均已清理；无 active/pending allocation，无 Citespec 测试容器。
- 最终审计：strict `failures: 0, warnings: 27`（通过）；GPU
  `failures: 0, warnings: 10`（通过）；`fix_gpu_permissions.sh` 通过；
  `mode=active`，GPU 0–3 `free`，`gpu_audit.ok=true`。
- 本轮修复并部署：Docker `socket.SocketIO` 的 `.read/.write` I/O、
  `queue wait` 等到 allocation active、GPU 审计前自动修复设备权限、
  workspace 配额挂载解析；active 验收脚本修正为在交互 shell 提示符就绪后
  再写入命令。这些代码和文档改动见本次 cutover 提交。
- **当前待办**：外部 onboarding 阻塞——公共 443 尚未接到本地 intake，等待网络/
  系统管理员；见 §0.1。VS Code、文件传输和容器 root/宿主 GPU 5.6 仍未开放。
- 下一步：网络/系统管理员确认 443/NAT 与证书后，完成外部浏览器端到端
  onboarding；随后按 P0–P2 计划处理 5.6、备份恢复、安装器、CI 和可选能力。

### 0.1 外部 443 入口（等待网络管理员）

- 公共 `<public-url>` 由 **Apache** 前端终止，不是本机 nginx；
  当前 `curl -k -I` 返回 `403 Forbidden`（此前曾观察到
  `302 Location: .../ng`），`/ng` 返回 404；公共 HTTP 80 超时。外部入口当前
  不可用；本地 intake 正常监听 80/443。
- 本地 nginx 监听 80/443 并返回 301/200，但外部 NAT/端口转发没有把公共
  443（或某个子路径/子域名）映射到这台主机；外部前端不受本机 nginx 控制。
- `apply_url` 仍为 `<public-url>`，与本地 intake 的地址不一致；
  浏览器 onboarding 当前使用本地/内网路径。
- 公共 443、外部 Apache 和现有外部配置本轮未改；待网络/系统管理员确认
  NAT/防火墙/证书方案。建议使用独立子域名 + CA 证书，或由外部 Apache 反向
  代理到本地 nginx。解决后更新 `apply_url`、证书和外部端到端验收。

### 0.2 2026-10-26 一次性切换收尾

- 切换前备份：`<backup-root>/pre-cutover-20261026-174344`；安装器
  事务备份：`<backup-root>/20261026-174737-1343874`。备份目录中
  保留旧项目的历史名称、旧状态库和旧程序目录；活动运行时路径不保留旧名称。
- 旧服务/定时器已停止、禁用并删除；旧组与 intake 账号已改名；旧 nginx 站点和证书、
  sshd drop-in/banner、udev 规则、logrotate 配置、二进制、状态目录、配置目录和
  `/opt` 程序目录均已移除或归档。旧 timer stamp 文件已删除。
- 服务器 Python：使用从 uv 管理解释器复制出的 `/opt/python3.12`，并通过
  `--python /opt/python3.12/bin/python3.12` 安装，避免 venv 指向普通的
  `/root` 路径。服务器 `python3` 仍为 3.8.10，因此 preflight 会保留一条
  “python3 is 3.8.10”的 warn；安装器明确使用 3.12。
- 验收数据库：两份一次性测试账号的 membership、密钥、Linux 账号、home、
  workspace 和项目配额均已清理；验收数据库归档到
  `<backup-root>/acceptance-20261026-175625`，活动状态库重建为空
  并重新置为 `mode=active`。当前 `allocations`、`members`、`member_keys` 和
  队列均为空；没有 Citespec 测试容器。
- 最终扫描：活动路径、systemd 单元、组、账号、配置、SSH/nginx/udev/logrotate
  文本和活动镜像标签中均未发现旧名称；仅备份目录允许保留历史名称。
- 本地客户端：已用 `~/.ssh/id_ed25519_citespec` 替换旧命名的私钥/公钥，更新
  `~/.ssh/config`，并移除根账号的旧 `<admin-host-alias>` 别名；服务器
  `<admin-account>` 的 `authorized_keys` 仅保留该新公钥。
- 开户启动限流已修复：`citespec-resource-provision.service` 在 `[Unit]` 中设置
  `StartLimitIntervalSec=0`，避免正常的 inspect/provision/retry 短突发被 systemd
  默认启动频率限制拒绝。目标机已用连续 8 次服务启动验证不再出现
  `start-limit-hit`；既有 active 验收覆盖真实开户路径。

## 1. 基线信息

- 本轮起点：`01bc2bf`（`Add implementation report, member/admin guides, handoff
  and README updates`），工作区无未提交改动。
- 本轮开始前本地验证：`360 passed, 21 skipped`；Ruff check/format、mypy、
  `node --check` 通过。
- 当前本地验证：`400 passed, 21 skipped`（包含 P0-2 备份/恢复/校验和部署单元
  回归），Ruff check/format、mypy（35 文件）、`node --check`、`bash -n deploy/*.sh`
  通过。21 个 skip 为环境限制；Ubuntu CI 执行 Linux-only 项，Playwright 浏览器
  任务待接入 CI。
- 本轮实现与 sanitized handoff 已提交并推送：`8772763`（cutover）、`d0294be`
  （sanitized handoff + naming guard）；文档校准和 P0-2 实现见后续提交。
- GitHub Actions 运行 `40308426242` 在 `d0294be`、`40315992883` 在 `7e9a4cc`
  （P0-2）上通过；当前分支 `main`，远程 `origin`。P0-2 仍待实机部署和恢复演练。
- example-gpu 未改动，未执行 Docker/NVIDIA 重启、容器操作或验收测试。期间一次
  `admin ... --help` 被旧的自动中继逻辑误判并尝试解析 `example-gpu`，连接未建立，
  未认证、未修改主机状态；该缺陷已修复（含 `--help` 的命令不再中继，且新增
  回归测试）。

本地开发环境说明：Windows 上运行 Python 3.13；Git Bash 位于
`C:\Program Files\Git\bin\bash.exe`。Docker 和 `uv` 在本地不可用；WSL 未安装发行版。
Linux-only 的 UDS、`SO_PEERCRED`、Docker、NVIDIA、quota、sshd-in-container 和
systemd hardening 测试必须在目标机或 Ubuntu CI 执行。

## 2. 目标与当前阶段

目标：为 Citespec 实验室共享 GPU 服务器建立受约束的资源管理器，完成可丢弃开发
容器工作流，并在 Broker 中介下提供 VS Code 接入。当前实机部署在
<server-instance>；example-gpu 未改动。

当前阶段：

- 代码实现和安全加固已完成，包含 Broker、CLI、GUI、Docker driver、队列、
  开发容器 profile、容器 SSH、Broker 隧道和部署脚本。
- 成员自助接入/审批/开户/中央公钥/撤权/legacy import 已实现；2026-10-26 已在
  <server-instance> 实机完成本地 intake 的浏览器申请/审批/开户和 active 验收（§0）。
- 实机核心验收已完成；VS Code、文件传输、外部 onboarding 和容器 root/宿主 GPU
  5.6 未开放。D3 对外生产开放未完成，剩余项见 §9。
- example-gpu 未改动，继续保持原状。

## 3. 已完成的实现

- 成员生命周期与接入：
  - `membership/` 保存权威成员状态、有效期、`access_version`、密钥、审批决定和
    可重试操作；普通 RPC 与特殊流入口都检查 `membership_required`。
  - SQLite 迁移 3/4 有序、事务化、幂等：成员/申请审批/操作表、队列请求 GPU
    张数与幂等键、唯一索引，以及启用成员操作。迁移前用 SQLite backup API
    备份；全新数据库直接建到 schema v4。
  - 成员公钥写入 root 管理的 `/etc/citespec-resource/authorized-keys.d/<user>`；
    sshd drop-in 不保留 home `authorized_keys` 回退。`usermod -L`/中央密钥清空
    阻断登录，撤权/暂停同时关闭该成员的 Broker 流和隧道。
  - `deploy/membership/provision_executor.py` 是固定动作白名单的 root 执行器：
    创建/检查账号、写中央密钥、scrub home 密钥、工作区/配额、禁用/启用、只读
    `inspect_member`；拒绝系统账号和未托管账号。Broker 只写 spool JSON，执行
    失败保留阶段和可重试状态。
  - `enrollment/` 是低权限申请页/回执 API：真实 HTML 表单、POST-only 回执、
    CSP/安全头、按 IP 限流、私钥/超长输入拒绝；intake 服务没有 Broker socket、
    成员数据库、Docker 或 GPU 访问。
  - 审批绑定 `expected_version`；支持 `needs_info`；并发批准只有一个决定。
    队列/apply 使用幂等键，`gpu_count` 由服务端选卡，预设中的 GPU 值按“张数”
    解释而不是卡号。
  - `join` 在没有本地配置时可用 `--portal`；`setup` 可在没有服务器 TOML 的
    工作站使用 `--server --user`，显式确认首次主机指纹，写入托管 SSH 片段和
    `~/.config/citespec-resource/client.toml`（仅连接信息）。
  - 旧账号只能通过 manifest 驱动的 `admin members import-legacy` 显式采用；
    默认 dry-run，冲突时不 apply，不因同名自动授予成员资格。
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
    准入闸门同时要求当前空闲容量足以覆盖"全部存活 allocation 未来增长到
    配额上限 + min_free_docker_gb"的最坏情况（数据库查询失败时按整个预算
    fail-closed）；这缓解但不消除与非 Citespec 工作负载共存的风险——推荐把 Docker
    数据目录放在独立、容量明确的 XFS 文件系统上，避免与操作系统根分区争抢。
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
  - active 的必要安全条件抽取为 `validate_active_mode_policy`，配置加载、
    Broker 启动恢复持久化 active、运行时 admin 切换三处强制同一规则；
    development profile 在 active 下必须启用 min_free_docker_gb 与
    docker_storage_budget_gb，且预算与 Docker 所在文件系统真实容量相容。
  - GUI 申请页渲染全部 GPU（含状态与占有人）；占用 GPU 可选并生成排队请求，
    仅 unknown 状态禁选。
  - 托管 SSH 配置的读写以跨进程锁（fcntl/msvcrt）串行化；同一 allocation 的
    旧隧道退出时，若块端口已被更新的存活隧道接管则不删除块。
  - 工作站默认体验：本地 broker socket 不存在时，常规命令自动经配置的 SSH
    alias 中继执行（`--local` 可强制本地）。
  - Broker 在 `list`/`status` 返回 `capabilities`，CLI 与 GUI 的 VS Code 入口
    以服务端能力为准，不再依赖成员本地（可能过期）的策略副本。
  - CLI code 启动器以非零状态退出时不写 ready；SSH 配置块移入独立的
    `~/.ssh/citespec-vscode-config`，从用户配置顶部 Include 引入，避免既有
    `Host *` 通配选项覆盖托管块。
  - VS Code 授权密钥按 allocation 合并（owner ∪ 当前连接者），容器内在既有
    authorized_keys 上合并去重并封顶 40 行，不再整文件覆盖。
  - 队列自动准入前重新执行 GPU 审计与 workspace 配额闸门，与直接申请同等新鲜。
  - GUI 在 Windows 上以 CTRL_BREAK_EVENT 优雅停止隧道子进程（CLI 安装
    SIGBREAK 处理器完成清理），CLI 启动时清扫本地端口已关闭的失效 SSH 配置块。
  - development profile 的 capabilities 在 cap_drop=ALL 之上精确补回十项，
    含 sshd 预认证权限分离所需的 SYS_CHROOT；不得以 SYS_ADMIN/--privileged
    替代。
    - 容器创建后立即记录 container_id；身份初始化/激活失败且容器清理也失败、
    以及 reconcile 发现 active 容器已停止但删除失败时，allocation 进入非终态
    cleanup_pending：GPU claim 与 CPU/内存/存储预算保持占用，reconcile 周期
    重试清理并在确认容器消失后 finalize。restarting/paused 是存活状态，
    不会被当作垃圾回收。
  - 托管 SSH 配置的 Include 指令只在托管文件中不再有任何 allocation 块时才
    移除（跨 allocation 安全）；同一 allocation 的新旧隧道由端口存活判定接管。
  - VS Code 探测改为可追踪的异步子进程，超时显式 kill 并回收。
  - 容器身份初始化同时建立与宿主 citespec-gpu 同 GID 的容器内补充组并把成员加入
    （sshd 登录从容器 /etc/group 重建补充组，docker group_add 只覆盖 exec 路径）；
    验收要求两条登录路径分别检查 `id -G`、`nvidia-smi`、`torch.cuda`。
  - GUI VS Code 按钮通过 `--readiness-file` JSON 协议等待真实就绪
    （listening/tunnel_verified/ready/error），并显式传递配置路径；不再假成功。
    隧道全程 try/finally：探测失败也会清理 SSH 配置块、监听器和子进程。
    code CLI 缺失时不写 ready，readiness 带 message，GUI 展示可复制的 URI。
  - GUI 提供 job 启动（整条 shell 命令原样传递，容器内以 `bash -lc` 执行）与
    `/workspace/.citespec-logs/` 持久日志查看；release 后仍可列出/读取历史日志
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

1. example-gpu 上的训练、现有容器和资源分配不得被中断。未获得明确批准时，不部署、
   不重启 Docker/NVIDIA、不修改 udev 规则、不创建或停止容器。
2. 成员 SSH 仅允许密钥认证，并通过 `ForceCommand` 限制为白名单 CLI；不得暴露
   host shell 或 SSH 转发。成员公钥只从 root 中央目录读取，home
   `~/.ssh/authorized_keys` 不是认证回退。
3. 成员不得属于 `sudo`、`docker`、`citespec-gpu`、`video` 或 `render` 组。
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
11. 同名 Linux 账号不自动成为成员；只有显式 legacy import 才能采用已有账号。
12. 首次 SSH 主机信任必须由用户核对 SHA256 指纹；不得使用
    `StrictHostKeyChecking=accept-new`，不得关闭主机密钥检查。

## 5. 目标机验证状态

2026-10-26 已在<server-instance>完成 active 核心验收，详细结果见 §0。下列清单按
当前状态分为“已完成”和“未覆盖/未开放”。

### 5.1 已完成

- 真实 SSH 白名单与拒绝路径、成员开户/撤权、中央公钥写入与回读。
- 真实 `useradd`/`usermod`/组、workspace 项目配额、`inspect_member` 和开户失败
  重试的核心路径。
- intake 服务、provision `.path`/`.service` 的 systemd 启动与本地浏览器申请、
  回执查询、审批、开户。
- systemd `ProtectHome=read-only`、`ProtectSystem=strict`、Docker socket 访问和
  GPU 审计在 unit hardening 下运行。
- udev 规则、现有 `/dev/nvidia*` 权限、GPU 审计、active acceptance、队列并发、
  到期/重启、restricted profile 下的分配/释放和 workspace 保留。
- 每个成员 workspace 的项目配额实际生效；执行器开通后回读确认。
- 文件传输策略拒绝路径已验证；真实文件传输能力仍未开放。

### 5.2 未覆盖/未开放

- 外部 443/DNS/TLS onboarding：等待网络/系统管理员；本地 intake 正常。
- development profile：当前 Docker root 为 ext4，未满足 XFS + prjquota；
  VS Code、容器内 sshd、tunnel release 和可写层配额均未开放。
- 文件传输的 TOCTOU、路径逃逸、symlink、配额和中断恢复：门禁保持关闭。
- 容器 root 对宿主 `/dev/nvidia*` 的 5.6 权限影响：P0 前置项；完成前不开放
  development/成员 root。
- `userns-remap` 与 GPU 设备、bind mount 的交互：未启用。
- reboot 和 NVIDIA driver reload 后的 GPU 审计复验。
- `join --portal`/`setup`/`me`/`start`/`gui` 在真实 Windows/Linux 工作站上的完整
  路径；本地 CLI 和手动路径已验证，跨工作站验收待补。
- VS Code 会话内 GPU 访问、GUI readiness、Docker 容量阈值和真实多用户并发下的
  队列饥饿保护。

## 6. 后续变更/复验流程

> 2026-10-26 的<server-instance> active 部署已完成（§0）；以下流程用于后续变更或
> 复验，执行前确认维护窗口和测试资源。

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
bash -n deploy/verify_portal_network.sh
```

### 步骤 3：变更前确认

确认以下条件：

- 训练已结束或不在目标维护范围内；
- 管理员已批准部署或目标机只读验收；
- 测试不会影响现有容器、GPU 分配和其他用户；
- 已确定测试 GPU、测试成员账号和维护窗口。

只读审查和本地测试可随时进行；修改主机状态前完成上述确认。

### 步骤 4：只读目标机检查

获得只读验收批准后，按顺序执行：

```bash
bash deploy/preflight.sh --config lab/resources.local.toml
bash deploy/audit_legacy.sh --production
bash deploy/verify_gpu_permissions.sh --json
bash deploy/verify_portal_network.sh --nginx-root /etc/nginx
bash deploy/verify_monitor_only.sh --member <member> --skip-snapshot
```

`verify_portal_network.sh` 只读：检查 Portal/apply vhost 是否绑定具体私有地址、是否
有 `allow <vpn-source-cidr>; ... deny all;`、是否只反代 loopback，以及 Portal
unit/监听器的当前状态。Portal vhost 尚未部署时它会报告 warning，不会失败。

同时保存：

```bash
docker info --format '{{.Driver}}|{{.DockerRootDir}}'
findmnt -no TARGET,FSTYPE,OPTIONS -T /home/data1/citespec-users
systemctl show citespec-resource-broker \
  -p ProtectHome -p ProtectSystem -p ReadWritePaths -p NoNewPrivileges
xfs_quota -x -c 'report -p -b -h' /data1
```

上述命令除显式 NVIDIA 探测外均为只读。`preflight.sh` **默认不调用**
`nvidia-smi`：以 root 调用会前后比较 `/dev/nvidia*` 的 mode/owner，而未安装 wrapper
时该探测本身可能把节点重置为 `0666 root:root`。需要探测时先安装 wrapper，再显式
使用 `--probe-nvidia`；安装器会在 wrapper 和权限修复完成后自动执行一次
`--probe-nvidia --nvidia-only`。`--skip-nvidia` 是默认行为的显式写法，也会跳过
compute process 查询，因此应在另一时刻单独确认没有训练进程。若 `preflight.sh`、
GPU 审计、`nosuid` 或配额检查失败，停止，不得继续安装或切换到 active。

### 步骤 5：完整验收

完整验收在维护窗口内执行 [`docs/acceptance.md`](acceptance.md) 中的流程。
验收期间：

- 使用专用测试 GPU 和测试账号；
- 不触碰训练容器或其他用户的 allocation；
- 记录镜像 digest，不使用浮动 tag；
- 在容器 sshd、隧道、配额和 release/expiry 测试通过后，才考虑启用
  `vscode_enabled`；
- 在 TOCTOU、配额和 target-host 文件传输测试通过后，才考虑启用
  `file_transfer_enabled`；
- 保留所有命令输出、`docker inspect` 安全字段、审计输出和操作者信息。

## 7. 变更边界

以下操作需要单独批准：

- `install_server.sh --yes` 或任何会修改主机的 `deploy/` 脚本；
- 重启 Docker、NVIDIA 驱动或相关 systemd 服务；
- 应用 udev 规则或修改 `/dev/nvidia*` 权限；
- 创建、停止、删除或重新配置现有容器；
- 修改现有 allocation、workspace、队列或数据库；
- 将 `lab/resources.local.toml`、真实地址、密钥或私人联系方式提交到 Git；
- 未经明确批准在目标机上运行 `acceptance_active.py`（本轮已获批准并已执行；
  重跑仍需批准）；
- 将 `vscode_enabled` 或 `file_transfer_enabled` 设为 `true` 以绕过验收。

## 8. 关键文件

- `src/citespec_resource/broker/server.py`：运行时闸门、严格 managed 匹配、队列公平性、
  隧道生命周期、成员/审批/密钥 RPC、撤权连接关闭、对账/到期钩子。
- `src/citespec_resource/membership/{models,store,service,provisioning}.py`：权威成员
  状态机、审批版本绑定、操作/密钥/legacy import、执行器客户端。
- `src/citespec_resource/enrollment/{server,static}`：公开申请页、回执 POST、限流、
  intake 存储。
- `src/citespec_resource/onboarding.py`：setup/doctor、严格主机密钥、托管 SSH 片段、
  成员状态探测。
- `src/citespec_resource/db.py`：schema 迁移 3/4，备份与幂等索引/操作表。
- `src/citespec_resource/backup.py`：在线 SQLite 备份、清单校验、恢复、保留策略。
- `deploy/alert.sh`：systemd `OnFailure=` 本地日志和可选 webhook 告警。
- `src/citespec_resource/broker/docker_driver.py`：development profile、存储/`nosuid`
  预检、容器 SSH passwd/key 准备、隧道 demux。
- `src/citespec_resource/config.py`：路径校验、active/quota 策略闸门。
- `src/citespec_resource/workspace_quota.py`：XFS/ext4 项目配额闸门。
- `src/citespec_resource/gpu_audit.py`：GPU 审计运行器和设备指纹。
- `src/citespec_resource/cli.py`：`tunnel`、`vscode`、managed SSH 配置、子进程清理。
- `src/citespec_resource/gui/server.py`：队列/job/VS Code 路由和子进程生命周期。
- `deploy/preflight.sh`：Docker 存储、`nosuid`、配额和 profile 检查。
- `deploy/verify_monitor_only.sh`：只读 monitor-only 安全验收。
- `deploy/verify_gpu_permissions.sh`：成员 GPU 权限审计，支持 JSON。
- `deploy/install_server.sh`：事务化安装、timer、成员 shell、intake/executor
  单元、中央密钥/spool/state 目录、udev 回滚。
- `deploy/membership/provision_executor.py`：固定动作 root 执行器。
- `deploy/sshd/60-citespec-resource.conf`：成员中央 `AuthorizedKeysFile` 策略。
- `deploy/create_user.sh`：break-glass 账号工具；成员密钥进中央目录，管理员
  密钥留 home。
- `deploy/configure_quotas.sh`：XFS/ext4 项目配额配置。
- `deploy/docker/Dockerfile.base`、`docker/sshd_config`、`docker/bootstrap.sh`：
  开发容器镜像和容器内 SSH/引导配置。
- `tests/test_docker_driver.py`：容器 SSH、存储预检和 UID 冲突测试。
- `tests/test_config.py`：workspace 路径、active/quota、成员客户端配置测试。
- `tests/test_membership.py`、`tests/test_membership_service.py`、
  `tests/test_provision_executor.py`：状态机、审批/操作/密钥/legacy import、
  执行器固定动作。
- `tests/test_intake_server.py`、`tests/test_broker.py`、`tests/test_gui_server.py`：
  申请页/回执/限流、Broker 闸门/幂等、GUI 路由。
- `tests/test_onboarding.py`、`tests/test_cli.py`：主机指纹、setup/join/me、
  自动中继与幂等键。
- `docs/acceptance.md`：目标机验收清单，5.8 是成员闭环，§6 是真实科研
  Agent 端到端训练验收（C-4）。
- `docs/queue.md`：队列状态和公平性语义。
- `docs/jobs-and-transfer.md`：job 和受限文件传输。
- `docs/legacy-access-revocation.md`：legacy 管理员权限撤销。
- `docs/backup-restore.md`：状态备份、校验、恢复和失败告警 runbook。

## 9. 剩余工作与优先级

当前剩余工作按 P0–P2 排序；外部 443 单独标记为 Blocked。

| 优先级 | 项目 | 当前状态 | 退出条件 |
|---|---|---|---|
| P0 | 文档状态基线 | 本次更新 | README/handoff/report/deploy README 使用同一状态口径 |
| P0 | 备份、恢复与告警 | 代码实现完成，CI `40465970858` 通过；待实机部署/恢复演练 | 定期备份、恢复演练、失败告警均通过 |
| P0 | SSH/管理员/审计基线 | 27 条 warning 待分类 | 无 critical/high；warning 有分类和负责人 |
| P0 | 容器 root/宿主 GPU 5.6 | 未验证 | 容器 root 不能修改宿主 `/dev/nvidia*` 并影响他人 |
| P1 | 安装器可复现性 | 待加强 | 固定镜像 digest；干净主机 dry-run/重复执行/回滚通过 |
| P1 | CI 覆盖 | 测试矩阵和 Windows/Linux 打包 smoke 通过；Playwright 浏览器 job 仍待接入 | Playwright 浏览器 job 和必要 Linux 集成项进入 CI |
| P1 | 客户端打包/自动更新 | `v0.1.0` 已发布；Windows/Linux 构建和发布资产校验通过 | 成员完成安装/更新验收，并记录首个 Linux 运行与更新结果 |
| P1 | GPU 验收脚本 | 默认按 8 卡 | 自动发现或显式传入实际 GPU 数 |
| P1 | Docker 存储路线 | ext4 + restricted | 保持 restricted，或迁移到 XFS + prjquota |
| P1 | 真实科研 Agent 端到端（C-4） | 未执行 | 普通成员仅凭正式指南完成受控真实训练并提交体验报告；不阻塞 monitor，向更多成员推广前完成 |
| P2 | VS Code / 文件传输 | 关闭 | 5.6、容器 sshd、隧道、配额和 TOCTOU 验收通过 |
| P2 | `setup` 图形化密钥配置 | CLI only | GUI 首屏可选择/生成密钥 |
| P2 | 历史容器、备份归档、扫描噪声 | 待清理 | 资源标注清楚，扫描输出可读 |
| Blocked | 外部 443/DNS/TLS onboarding | 公共入口返回 403 | 网络/系统管理员确认 NAT、DNS、TLS 方案 |

关键约束：

- 当前 Docker root 为 ext4，development profile 被存储闸门拒绝；当前生产配置为
  restricted profile。
- development profile 需要 `overlay2` + XFS + `prjquota`/`pquota`、workspace
  `nosuid`，并先通过 5.6。
- `install_server.sh` 在 `workspace_root` 与静态 unit 路径不一致时生成 broker、
  logrotate、disk-check、provision 四份 `10-workspace.conf` drop-in；`state_db`
  等自定义路径仍需同步 systemd `ReadWritePaths`。执行器写路径固定为 `/etc`、
  `/home`、`/home/data1/citespec-users` 和 spool。
- 旧 `create_user.sh`/home 公钥流程不再用于成员；已有账号通过显式 legacy import
  迁移。
- 示例配置的 `ssh_user = "admin"` 是管理员示例；成员使用
  `setup --user <自己的Linux用户名>`。
- `vscode_enabled` 和 `file_transfer_enabled` 当前为 `false`。
