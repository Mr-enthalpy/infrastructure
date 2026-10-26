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
> **D1=达成（本地实现/测试）/D2=已在<server-instance> active 模式执行（见 §0；外部
> onboarding 与可选功能仍待）/D3=未授权** 结论）。

## 0. 2026-10-26 <server-instance> 实机部署与 active 验收

> 本节记录实机结果；§1–§9 保留此前的实现/验收清单。本轮代码和文档改动见本次
> cutover 提交。

- 目标机：`<server-address>`（SSH 端口 `<ssh-port>`），实际部署在<server-instance>；**未部署到
  amax**。
- 管理员：`<admin-account>` 使用普通 shell，主组 `citespec-admins`（附加组 `sudo`、
  `citespec-cli`），**不属于 `citespec-members`**；root bootstrap 公钥已移除，
  `ROOT_KEYS_ABSENT`。
- 存储与 profile：workspace 根 `/home/data1/citespec-users`，ext4 + `prjquota`；
  DockerRootDir `/home/data2/docker-data` 为 ext4（非 XFS+prjquota），因此
  `container_profile = "restricted"`；配额闸门保持启用。
- GPU/镜像：4 张 GPU；当前基镜像 `citespec-base:2026.10`（ID `566d0e599e2e`，
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
- **未完成/风险**：公共 443 尚未接到本地 intake，外部 onboarding 不可用；
  见 §0.1。VS Code、文件传输、容器 root 对宿主 GPU 设备的 5.6 验证仍保持
  关闭/待验。
- 下一步：由网络/系统管理员解决 443/NAT 与证书；完成外部浏览器端到端
  onboarding；完成 VS Code、文件传输和 5.6 的 target-host 验收；在此之前不
  打开对应能力。

### 0.1 外部 443 冲突（未解决）

- 公共 `<public-url>` 由 **Apache** 前端终止，不是本机 nginx；
  当前 `curl -k -I` 返回 `403 Forbidden`（此前曾观察到
  `302 Location: .../ng`），`/ng` 返回 404；公共 HTTP 80 超时。不能据此
  认为本地 intake 已对外可用。
- 本地 nginx 确实监听 80/443 并返回 301/200，但外部 NAT/端口转发没有把公共
  443（或某个子路径/子域名）映射到这台主机；外部前端也不受本机 nginx 控制。
- `apply_url` 仍为 `<public-url>`，与本地 intake 的地址不一致；
  在外部路由和证书问题解决前，浏览器 onboarding 只能走本地/内网路径。
- 本轮没有修改公共 443、未重启外部 Apache，也未改动现有外部配置。需要网络/
  系统管理员确认 NAT/防火墙/证书方案；建议使用独立子域名 + CA 证书，或明确
  由外部 Apache 反向代理到本地 nginx。解决后再更新 `apply_url`、证书和
  外部端到端验收。

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
- 本轮结束本地验证：`392 passed, 21 skipped`（新增 1 个 Playwright 浏览器
  测试在本机因缺依赖 SKIP；新增命名/部署回归测试 3 项），Ruff check/format、mypy
  （34 文件）、`node --check`、`bash -n deploy/*.sh` 通过。21 个 skip 均不计为验收。
- 本轮实现与文档改动**尚未提交**；最终测试结果见
  `docs/reports/member-onboarding-and-ux.md`。开始新会话时以 `git log -1` 和
  `git status` 为准；不要把本轮文档中的数字当作已提交 CI 结果。
- 当前分支：`main`；远程：`origin`。本次提交推送后由 GitHub Actions 验证。
- amax 未被部署，未执行 Docker/NVIDIA 重启、容器操作或验收测试。期间一次
  `admin ... --help` 被旧的自动中继逻辑误判并尝试解析 `amax`，连接未建立，
  未认证、未修改主机状态；该缺陷已修复（含 `--help` 的命令不再中继，且新增
  回归测试）。

本地开发环境说明：Windows 上运行 Python 3.13；Git Bash 位于
`C:\Program Files\Git\bin\bash.exe`。Docker 和 `uv` 在本地不可用；WSL 未安装发行版。
Linux-only 的 UDS、`SO_PEERCRED`、Docker、NVIDIA、quota、sshd-in-container 和
systemd hardening 测试必须在目标机或 Ubuntu CI 执行。

## 2. 目标与当前阶段

目标：为 Citespec 实验室共享 GPU 服务器建立受约束的资源管理器，完成可丢弃开发容器
工作流，并在 Broker 中介下提供 VS Code 接入；在全部安全闸门通过前不进行 amax
部署（本轮部署目标为<server-instance>，见 §0）。

当前阶段：

- 代码实现和安全加固已完成，包含 Broker、CLI、GUI、Docker driver、队列、
  开发容器 profile、容器 SSH、Broker 隧道和部署脚本。
- 成员自助接入/审批/开户/中央公钥/撤权/legacy import 已实现；2026-10-26 已在
  <server-instance>实机完成本地 intake 的浏览器申请/审批/开户和 active 验收（§0）。
- P5 active 模式实机验收已在<server-instance>通过；VS Code、文件传输和公共 443 外部
  onboarding 仍关闭/待验；D2 的剩余项见 §9。
- 未在 amax 部署；amax 仍用于训练。

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

1. amax 上的训练、现有容器和资源分配不得被中断。未获得明确批准时，不部署、
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

## 5. 目标机待验证项

以下项目无法在 Windows 本地完成，必须在获得批准后于目标机/隔离 Linux
环境验证：

> 2026-10-26 已在<server-instance>完成 active 验收；本节保留完整清单，已覆盖项见 §0，
> 未覆盖项仍是后续工作。

- **D2 成员闭环**：真实 sshd 的 `authorizedkeysfile` 是否指向
  `/etc/citespec-resource/authorized-keys.d/%u`；home-only 公钥是否确实无法登录；
  撤权后新连接/ControlMaster 是否失效。
- 真实 `useradd`/`usermod`/组/中央密钥原子写入/回读、workspace 项目配额与
  `inspect_member` 在目标发行版上的行为；开户失败和中断后的操作重试。
- intake 服务、provision `.path`/`.service` 的 systemd 启动、权限隔离、
  超时/崩溃恢复；真实浏览器提交、回执查询、限流和 CSP。
- `join --portal` 无配置工作站、`setup` 指纹确认与托管 SSH 片段/客户端配置、
  `me`/`start`/`gui` 在真实 Windows/Linux 工作站上的路径。
- Docker 实际存储驱动是否为 `overlay2`，DockerRootDir 是否位于 XFS，以及
  `prjquota`/`pquota` 是否可用。
- workspace 文件系统是否以 `nosuid` 挂载；若未挂载，development profile 和运行时
  存储闸门必须保持阻断。
- 每个成员 workspace 的项目配额是否实际生效；执行器/`create_user.sh` 开通后
  用 `lsattr -p`、探针文件继承和 `xfs_quota report`/`repquota` 回读确认。
  `configure_quotas.sh` 仍用于历史 workspace 的 backfill 或调整限额。
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
- VS Code 会话内的 GPU 设备访问：`id -G` 含 citespec-gpu GID、`nvidia-smi -L`、
  `torch.cuda.is_available()`；与 docker-exec 路径分别验收（见 acceptance 5.5）。
- 容器内 sudo（容器 root == 宿主 UID 0）能否 chmod/chown 宿主 /dev/nvidia*
  节点并影响其他成员的访问：必须在批准的测试机/测试 GPU 上按 acceptance 5.6
  的流程验证，保留前后 stat 记录。这是对普通成员开放前的硬性前置条件：
  周期性 GPU 审计只能检测漂移并阻止新分配，不能替代隔离——若证实容器 root 可
  变更宿主设备节点，必须先落实更强的隔离或权限控制（并评估 userns-remap），
  不得仅依赖审计告警。

## 6. 下一会话执行顺序

> 2026-10-26 的<server-instance> active 部署已完成（§0）；以下流程用于后续变更或复验，
> 仍需明确批准。

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
findmnt -no TARGET,FSTYPE,OPTIONS -T /data1/citespec-users
systemctl show citespec-resource-broker \
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
- `docs/acceptance.md`：目标机验收清单，5.8 是 D2 成员闭环。
- `docs/queue.md`：队列状态和公平性语义。
- `docs/jobs-and-transfer.md`：job 和受限文件传输。
- `docs/legacy-access-revocation.md`：legacy 管理员权限撤销。

## 9. 已知限制与风险

- 开发容器在没有 user namespace 时，容器 root 映射到宿主 root；当前安全性依赖
  Docker 隔离和 `nosuid` workspace。不得添加 host 挂载或 Docker socket。
  acceptance 5.6（容器 root 对宿主设备节点的影响）是对成员开放的硬性前置：
  审计只是检测/阻断后续分配的补偿手段，不能替代底层权限隔离。
- Docker 存储条件是 fail-closed 的：amax 的 Docker Root Dir 若不是
  overlay2 + XFS + prjquota，development profile 会被闸门直接拒绝而不是自动
  退化——这需要容量规划与维护窗口整改，不得绕过。存储预检证明的是"配额可
  执行 + 当前容量充足 + 预算与磁盘相容"，不能保证运行中的容器、既有镜像或
  非 Citespec 容器未来不耗尽磁盘；宿主机容量规划与实测仍是上线前提。
- 生产启用清单（当前示例全部为关）：个人账号旧权限回收（audit_legacy 通过）、
  文件系统条件满足、Docker GPU 隔离正常、容器内 sudo 不破坏宿主机（5.6）、
  租期结束可靠清理容器与连接。全部有证据后按序打开
  deployment_allowed → vscode_enabled → file_transfer_enabled。
- `install_server.sh` 仅对非默认 `workspace_root` 或 `state_db` 发出警告；自定义
  路径必须手工更新 systemd `ReadWritePaths`。开户执行器的写路径固定为
  `/etc`、`/home`、`/data1/citespec-users` 和 spool。
- **D2 已在<server-instance> active 模式执行（2026-10-26）**：真实 SSH 中央密钥/撤权、
  intake/provision、浏览器本地路径、restricted profile、GPU 审计和 active
  acceptance 已通过；仍未覆盖外部 443 onboarding、VS Code、文件传输、容器 root
  对宿主设备 5.6、reboot/driver reload 后的复验。本地测试不能替代这些剩余项。
- 旧 `create_user.sh`/home 公钥流程已不再适用于成员；已有账号必须显式 legacy
  import。迁移前直接切换 sshd 可能锁定旧成员，部署步骤要求先 dry-run/导入。
- 应用 udev 规则可能改变正在运行的训练容器的设备属主/权限；必须在训练结束后获得
  批准再执行。
- 本地 Windows 测试不能验证 Linux UDS、Docker、NVIDIA、quota、sshd-in-container
  和 systemd hardening。
- 容器内 UID 1000 passwd 重写已在代码中修复，但仍需 Linux 目标机验证。
- `vscode_enabled` 和 `file_transfer_enabled` 在示例配置中保持 `false`；任何启用
  都必须以目标机验收证据为依据。
- 示例配置的 `ssh_user = "admin"` 是管理员示例，不是成员客户端配置；成员应运行
  `setup --user <自己的Linux用户名>`，不要复制服务器 TOML。
- `setup` 尚未提供图形化密钥选择/自动生成，未配置 GUI 首屏也还没有直接进入
  setup 的入口；当前入口是 CLI。按目标 §5.4 这是非阻断 UX 缺口，D2 前可继续
  使用已有默认 SSH 密钥完成主机验证。
