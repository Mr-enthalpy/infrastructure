# P1 部署前安全修复报告

- 分支：`fix/p1-pre-deploy`，基于 `main@e987c7e`
- 范围：仅修复审计确认的 P1 项；未连接、未修改生产主机
- 本地验证：**563 passed / 27 skipped**；Ruff check/format、mypy、`bash -n deploy/*.sh` 通过
- 客户端版本：`0.1.1`（部署候选）；`v0.1.0` 已发布内容不变，未替换、未重发
- 真实主机验证：**未执行**（按本轮要求，P1 阶段不触碰目标机）
- Git 历史：未改写；未替换或重新发布 `v0.1.0`

## 1. 修复清单

| 编号 | 问题 | 提交 | 主要落点 |
|---|---|---|---|
| P1-1 | Portal 能力仅按同名用户判断，未绑定服务账号 UID | `1520d75` | `identity.py`、`broker/server.py`、`config.py`、安装器/开户执行器 |
| P1-2 | Linux 自动更新解包后丢失可执行位 | `66f4f9a` | `update.py` |
| P1-3 | SQLite restore 在主库/WAL 之间不是原子组，残留 WAL 可回滚数据 | `79d48d0` | `backup.py` |
| P1-4 | 备份未静默写入者，跨库不一致；恢复后非终态操作未收敛 | `fe3d866` | `backup.py`、`membership/service.py` |
| P1-5 | 安装失败回滚只恢复文件，遗留 unit/timer enable/active 状态和 NVIDIA 路径漂移 | `3f4d8eb` | `install_server.sh` |
| P1-6 | 首次安装没有可验证的签名信任链 | `f036d66`、`bf4ab88` | `packaging/`、release workflow、客户端安装文档 |
| P1-7 | preflight 的 `nvidia-smi` 探测会改设备权限却未报告；Portal/apply vhost 缺少源限制审计 | `8f5d185` | `preflight.sh`、`verify_portal_network.sh`、nginx 示例 |

## 2. 各项修复与证据

### P1-1 Portal 能力绑定服务账号 UID

**问题**：Portal 通过 Unix socket 调用 Broker 时，原先只按用户名匹配，无法区分
同名的其他身份或被错误配置的服务账号；成员组漂移也不会被拦截。

**修复**：

- `IdentityResolver` 同时校验服务账号名和 UID，并以配置项
  `[portal].service_account` 指定唯一允许的 Portal 账号。
- 安装器创建/校验 Portal 账号，发现其属于 `sudo`、`docker`、`citespec-gpu`、
  `citespec-admins` 等禁止组时 fail closed。
- 开户执行器拒绝把成员加入 Portal/管理员/GPU 等禁止组；组漂移产生告警。
- Broker 只在身份解析结果与配置一致时开放 Portal 的窄能力。

**证据**：`tests/test_identity.py` 覆盖 UID 不符、同名不同 UID、账号名不符和组漂移；
`tests/test_config.py`、`tests/test_membership_service.py`、`tests/test_deploy_scripts.py`
覆盖配置解析、执行器拒绝组和安装器检查。

**残余风险**：若攻击者已能以 root 修改配置或 systemd unit，则所有本机身份绑定都可被
绕过；本修复的边界是“非 root 进程不能冒充 Portal 服务账号”。目标机首次安装后仍需
确认 `/etc/citespec-resource/config.toml` 的 `service_account` 与实际账号一致。

### P1-2 Linux 更新安装后恢复可执行位

**问题**：自动更新把 payload 解压到版本目录后未恢复 POSIX 可执行位，启动器可能
无法执行新版本。

**修复**：`update.py` 在解压完成后对 payload 设置 `0755`，并在启动前修复已安装
payload 的执行位；非 POSIX 平台不受影响。

**证据**：`tests/test_update.py` 断言解包后的 mode 为 `0755`，并在 Linux CI 上实际
执行该 payload。

**残余风险**：Windows 端依赖文件扩展名，不涉及 mode；若文件系统以 `noexec` 挂载，
设置执行位仍无法运行，需要在目标机确认安装目录所在挂载选项。

### P1-3 SQLite 主库/WAL/SHM 原子组恢复

**问题**：restore 先替换主库再处理 WAL/SHM，崩溃或失败时可能留下旧 WAL，使已经
回滚的主库再次回放新事务；checkpoint 与文件移动也不是一个整体。

**修复**：

- restore 前执行 `wal_checkpoint(TRUNCATE)`；checkpoint 失败或数据库忙时在移动任何
  文件前中止。
- 主库、`-wal`、`-shm` 作为一个组备份、替换、回滚和清理；失败时恢复整组旧文件。
- busy 的 checkpoint 允许推进 WAL 帧，但不移动在线文件。

**证据**：`tests/test_backup.py` 新增 WAL 故障注入，覆盖 checkpoint 失败不移动文件、
组替换失败回滚、残留 WAL 清理，以及“数据库忙时不 swap 且行数据完整”。

**残余风险**：SQLite 组替换仍依赖同一文件系统上的原子 `rename`/复制语义；跨文件系统
恢复目录不在支持范围内。恢复后必须运行一次完整性校验，并在维护窗口内演练。

### P1-4 备份静默写入者与恢复后操作收敛

**问题**：仅使用 SQLite online backup 无法保证 `state.db`、intake DB、spool 和文件
之间的一致时点；恢复旧库后，原先 `pending`/`running` 的开户操作可能永久悬挂。

**修复**：

- 默认按 `WRITER_UNITS` 依次停止写入者，记录每个 unit 的 active 快照，再执行
  checkpoint、复制和重启；重启顺序为 provision path → intake → portal → broker。
- 增加 `--no-quiesce` 显式逃生口；默认路径不会静默跳过一致性处理。
- 使用 `flock`（POSIX）/`msvcrt`（Windows）防止并发备份重叠。
- restore 后调用 `reconcile_restored_operations`：把非终态操作标记为可重试，
  将 `provisioning` 成员置为 `provision_failed`，避免数据库继续声称访问已生效。

**证据**：`tests/test_backup.py` 覆盖 unit 停止/重启顺序、active 快照、并发锁、
`--no-quiesce`、checkpoint 与组替换；`tests/test_membership_service.py` 覆盖恢复后
操作收敛。

**残余风险**：

- 静默依赖 systemd unit 名称和当前权限；如果目标机的 unit 名不同，必须在部署前
  调整 `WRITER_UNITS` 或用 `--no-quiesce` 并人工确认一致性。
- 静默窗口内 Broker/intake/Portal 不可用，必须在维护窗口执行。
- 备份仍与主机同信任域，未做异地副本；workspace 不在备份范围内。
- 恢复后的操作只会变为可重试，不会自动重放；需要管理员审核后重试。

### P1-5 安装器回滚完整性

**问题**：安装失败时只恢复文件，未恢复 unit/timer 的 enable/active 状态；NVIDIA
wrapper、`.real` helper、shim 和权限可能在失败后处于半安装状态。

**修复**：

- 安装前捕获所有受管 unit/timer 的 active 和 enabled 状态；回滚先停 timer，
  再按入口→执行器顺序停服务，删除本次新增的 enablement，恢复文件后
  `daemon-reload`，再按原状态恢复 enable/start。
- 记录 NVIDIA 路径的存在性、mode、owner 和 SHA-256（包括 `.real` 不存在的情况）；
  回滚后逐项校验，出现漂移时明确告警。
- 捕获 `EXIT`、`HUP`、`INT`、`TERM`；中断安装同样走回滚。
- 备份 intake/portal DB 时同时备份 `-wal`/`-shm`；提供
  `CITESPEC_INSTALL_LIB_ONLY=1` 供测试加载回滚函数。

**证据**：`tests/test_deploy_scripts.py` 使用 stub `systemctl` 注入安装器故障，
断言 timer 先停、服务后停、新增 enablement 被删除、原 unit/timer 恢复、NVIDIA
wrapper 恢复且 `.real` 被移除，以及备份被篡改时报告 mismatch。

**残余风险**：stub 测试不能替代真实 systemd；首次在目标机执行前应在一次性 VM 上
做 fault-injection 演练。Broker 仍在运行时，回滚不会替换 SQLite 状态，只告警。

### P1-6 首次安装签名与验证

**问题**：自动更新有签名清单，但首次安装没有已安装的启动器，用户无法验证 full zip
和安装脚本。

**修复**：

- 清单新增 `first_install.<platform>`，签名覆盖 full zip 的 URL、SHA-256、大小，
  以及 zip 内 `install.sh`/`install.ps1` 的 SHA-256 和大小。
- 新增独立 `packaging/verify_release.py`、`.sh`、`.ps1`：校验 Ed25519 签名、
  full zip 摘要、zip 内安装脚本摘要，并可选校验已解压脚本。
- `install.sh --verify <sha256>` 与 `install.ps1 -Verify <sha256>` 是强制参数；
  缺失、格式错误或摘要不匹配都会在写入任何文件前退出。
- release workflow 用签名清单自检两个平台的 full zip，并把验证器作为 Release
  资产发布；缺少 full zip 或安装脚本时发布失败。
- 客户端文档说明首次安装步骤和残余信任缺口。

**证据**：`tests/test_packaging.py` 覆盖有效签名、篡改包、清单指向不同安装脚本、
缺少 `first_install`、签名被改、manifest 生成和安装脚本自校验；另用本地忽略的
签名密钥做过端到端生成/验证：有效包通过，篡改包被拒绝。

**残余风险**：

- 验证器、清单和 zip 可能来自同一 HTTPS 源；若下载通道被完全控制，三者可被同时
  替换。发布页应通过独立渠道公布 `verify_release.py` 的 SHA-256 或公钥指纹，首次
  安装时人工比对。
- `v0.1.0` 已发布且未重新签名；该机制从后续版本开始生效。不得替换或重发 `v0.1.0`。
- Release workflow 需要 `CITESPEC_UPDATE_SIGNING_KEY` secret；私钥不得进入仓库。

### P1-7 NVIDIA/preflight 准确性与 Portal 网络模板

**问题**：

- root 运行 `nvidia-smi` 会把 `/dev/nvidia*` 重置为 `0666 root:root`；旧 preflight
  在调用前检查权限，可能把探测后的状态误报为安全。
- preflight 未验证 `/usr/bin/nvidia-smi` 和 `/usr/bin/nvidia-modprobe` 是否仍是
  Citespec wrapper、`.real` 是否存在且无 setuid/setgid、shim 是否安装。
- nginx 示例只有防火墙假设，没有 vhost 级 `allow/deny`，也没有只读审计脚本。

**修复**：

- preflight 调用 `nvidia-smi` 前后分别记录每个 `/dev/nvidia*` 的 mode/owner；未安装
  wrapper 时告警“探测本身可能改权限”，wrapper 已安装但仍变化时失败。
- 校验 `nvidia-smi` 和 `nvidia-modprobe` wrapper 的 `.real` 目标、`.real` 存在且无
  setuid/setgid、shim 可读；`.real` 已存在但 wrapper/shim 缺失时失败。
- preflight 默认**不再调用** `nvidia-smi`：未安装 wrapper 时探测本身可能把
  `/dev/nvidia*` 重置为 `0666 root:root`。显式 `--probe-nvidia` 才执行前后
  mode/owner 快照；wrapper 未安装或 `.real` 缺失时拒绝探测。
- 保留 `--skip-nvidia` 作为显式跳过（与默认行为一致），并新增 `--nvidia-only`
  只跑 NVIDIA helper/设备检查。
- 两个 nginx 示例加入 `allow <vpn-source-cidr>; deny all;`，并保留
  `X-Citespec-Client-IP $remote_addr` 覆盖。
- 新增只读 `deploy/verify_portal_network.sh`：检查 vhost 是否绑定具体私有地址、
  是否有 `allow/deny all`、是否只反代 loopback、是否转发客户端可控的
  `X-Citespec-Client-IP`、上游端口是否有 wildcard listener，以及 Portal unit 状态。

**证据**：`tests/test_deploy_scripts.py` 覆盖 preflight 关键字/前后快照/wrapper 校验，
nginx 示例的 allow/deny 与 loopback 反代，以及 verify 脚本对合规 vhost 和
wildcard/缺 allow/公共上游/伪造头的拒绝行为。

**残余风险**：

- 默认不做 NVIDIA 探测，因此也不检查 compute process；需要探测时必须先安装
  wrapper，再显式使用 `--probe-nvidia`。
- 未安装 wrapper 的主机上，任何 root `nvidia-smi` 调用仍可能改权限；首次安装前
  应尽量避免在成员可登录时运行，安装器只在 wrapper/权限修复完成后做一次
  `--probe-nvidia --nvidia-only`。
- `verify_portal_network.sh` 只审计配置和当前 listener，不替代防火墙/VPN；`allow`
  范围仍是占位符，必须由网络管理方填写。
- 路径按 `/usr/bin/nvidia-smi`、`/usr/local/lib/citespec-resource/` 固定；发行版
  路径不同会导致告警而非自动适配。

### 追加修复：单机测试服务器部署阻断项

在不改写 Git 历史、不替换 `v0.1.0` 的前提下，本轮把测试服务器部署前发现的阻断项
并入同一 PR：

**B-1 安装事务、版本化 venv 与一致快照**

- 新环境先安装到 `venvs/.staging-<version>-<timestamp>-<pid>`，通过
  `import citespec_resource`、两个入口点和 `version` smoke test 后，才提升为
  `venvs/venv-<version>-<timestamp>-<pid>`，再用临时符号链接原子切换
  `/opt/citespec-resource/.venv`。旧目录/旧符号链接保留，供回滚恢复。
- 已存在 active unit/timer 时，安装器拒绝继续，除非显式
  `--maintenance-window`；该选项先停止受管写入者，再取快照。
- 快照复用 `backup.py backup --no-quiesce --keep 0`，覆盖 state/intake/portal
  SQLite、配置和 spool，并校验 `manifest.json`；不再在写入者运行时直接复制
  数据库文件。
- 回滚先恢复旧 venv，再恢复快照；失败时保留旧环境、数据库和 spool 的一致状态。

**B-2 备份 unit 的可写路径**

- `citespec-resource-backup.service` 增加 `/var/lib/citespec-resource`、
  `/var/lib/citespec-resource-intake`、`/var/lib/citespec-resource-portal` 的
  `ReadWritePaths`，使 `ProtectSystem=strict` 下的
  `wal_checkpoint(TRUNCATE)` 和 `-wal/-shm` 截断能够完成。

**B-3 客户端安装自校验改为强制**

- `packaging/install.sh` 必须提供 `--verify SHA256`，`install.ps1` 必须提供
  `-Verify SHA256`；缺失、格式错误或摘要不匹配都在写入任何文件前退出。
- 版本号提升为 `0.1.1`；`v0.1.0` 的已发布资产和 tag 不变。

**B-4 非默认 workspace_root**

- 安装器不再因为 `server.workspace_root` 与静态 unit 的 amax 路径不同而硬失败；
  改为生成四份 `10-workspace.conf` drop-in（broker、logrotate、disk-check、
  provision），分别覆盖 `ReadWritePaths`/`ReadOnlyPaths`、`ExecStart` 和
  `CITESPEC_RESOURCE_WORKSPACE_ROOT`，确保所有引用该路径的 unit 与配置一致；旧的
  drop-in 在路径重新匹配静态值时删除，并纳入备份/回滚。

**B-5 Portal 网络验收门**

- 安装器**永不 enable** Portal unit；unit 增加
  `ConditionPathExists=/etc/citespec-resource/portal-network-accepted`，即使被
  手工 enable，没有验收标记也不会在启动时拉起。
- 新增 `deploy/portal_network_audit.py`：解析 `nginx -T` 的**生效配置**，要求
  唯一 Portal vhost、私网地址绑定、TLS 端口、批准 CIDR 内的 `allow`、末尾
  `deny all`、只反代 loopback、`X-Citespec-Client-IP` 由 `$remote_addr` 覆盖、
  证书/私钥匹配且未过期、TLS 1.2/1.3、上游只监听 loopback，并拒绝在没有标记时
  已经 enable/active 的 unit。
- `deploy/verify_portal_network.sh --acceptance` 是该审计的 fail-closed 入口；
  `deploy/enable_portal.sh --yes --approved-cidr ...` 是唯一 enable 入口：审计
  通过后才写标记并 `systemctl enable --now`，任一步失败会删除标记并回滚启用。

**B-6 GPU 设备节点隔离门**

- 新增 `deploy/verify_gpu_isolation.sh`：记录 `/dev/nvidia*` 的 inode/mode/owner，
  以成员身份经容器 sudo 尝试 `stat`/`chmod 0666`/`chown 1000:1000`，再次 diff；
  失败时自动恢复原 mode/owner 并保留
  `/var/log/citespec-resource/gpu-isolation-<UTC>/` 证据。
- 只有同时提供 `--bystander` 且旁路成员无法读写设备、完整 PASS 时，
  `--write-marker` 才会写 `/etc/citespec-resource/gpu-isolation.ok`。
- `config.py` 的 `validate_active_mode_policy`、preflight 和 Broker 启动/切换
  共用该门：active + development + container sudo 在标记缺失时一律拒绝。
- preflight 默认不探测 NVIDIA；安装器在 wrapper 和权限修复完成后执行
  `--probe-nvidia --nvidia-only`。若真机测试失败，优先尝试 user namespace
  remap；不可接受时关闭 development profile / container sudo，不写标记。

**证据**：`tests/test_deploy_scripts.py` 覆盖版本化 venv/原子切换、维护窗口与
`backup.py` 快照、workspace drop-in、Portal 审计与 enable 顺序、GPU 隔离脚本
fail-closed；`tests/test_config.py` 覆盖 active dev+sudo 在标记缺失/存在时的
行为；`tests/test_packaging.py` 覆盖强制自校验；`tests/test_backup.py` 及既有
stub 回滚测试继续覆盖备份/恢复与回滚状态。

**残余风险**：

- Portal 审计读取的是主机当前生效的 nginx 配置；DNS、证书签发、VPN 路由和
  防火墙仍由网络管理方负责，本仓库不修改。
- GPU 隔离结论只能在真实 NVIDIA 主机上得出；标记是验收记录，不是隔离机制本身。
- 版本化 venv 会保留旧环境；需要后续保留/清理策略，磁盘不足应在维护窗口前检查。

### 复审追加硬化

**R-1 已有 Portal 账号 fail-closed 校验**

- 安装器在复用同名 `citespec-portal` 账号前校验：UID 属于系统范围（1–999）、
  主组必须是 `citespec-portal`、登录 shell 必须是 nologin/false、home 必须是
  `/var/lib/citespec-resource-portal`、密码必须锁定、无 sudo 规则，且除主组和
  `citespec-cli` 外不得属于任何其他组（`sudo`、`docker`、`adm`、`wheel`、
  `citespec-admins`、`citespec-members`、`citespec-gpu` 等一律拒绝）。
- 任一不符即停止安装，要求管理员删除或重建账号；不会把普通成员或交互式账号
  收编为 Portal 服务身份。
- Broker 启动时用同一套规则复核服务账号（`portal_account_issues`），账号形态
  漂移时 Portal 能力保持禁用并在日志中给出具体原因。

**R-2 维护窗口检查前置**

- `--maintenance-window` 检查移到备份目录创建、回滚 trap、所有组/账号/组成员
  变更之前；拒绝时不会停止任何 unit、不写备份目录，并明确提示
  “no host changes were made”。
- 只读校验（Portal 账号形态、`citespec-gpu` 空成员、成员/管理员禁止组、
  Portal 组人类成员）全部先于写入执行，校验失败不会留下新建账号或组。

**R-3 GPU 隔离脚本中断恢复**

- 在第一次可能修改设备节点的 `chmod` 之前设置 `MUTATION_STARTED=1`；任何
  非正常退出（包括 INT/TERM/HUP）都会按预录状态恢复 `/dev/nvidia*`，并用
  `restore_device_state_verified` 重新比对 mode/owner/inode。
- 恢复失败时明确打印 CRITICAL 并保留 `after-restored-interrupted.txt` 证据；
  正常 PASS/FAIL 路径同样复核恢复结果。

**P2 残余边界：loopback 代理信任**

- 应用仍把来自 loopback 的 `X-Citespec-Client-IP` 视为可信代理头；宿主机上的
  其他本地用户可直接连接 127.0.0.1:8770/8765 伪造审计 IP 并影响按 IP 的限流。
- 这不会绕过 Portal 管理员权限，也不是本 PR 引入的回归；已记录为开放浏览器
  入口前的门禁：需要改为权限受限的 Unix socket，或引入可验证的代理身份。
  Portal 当前保持禁用，因此不阻塞 monitor 升级。

## 3. 验证汇总

| 检查 | 结果 |
|---|---|
| `python -m pytest -q` | 563 passed, 27 skipped（Windows 本地） |
| `python -m ruff check .` | 通过 |
| `python -m ruff format --check .` | 通过 |
| `python -m mypy src` | 通过（45 个源文件） |
| `bash -n deploy/*.sh` | 通过 |
| Ubuntu CI `ci`（3.11/3.12/3.13 × Ubuntu/Windows） | 通过（`38a00bc`） |
| Ubuntu CI `client-build`（linux-x64/windows-x64，含强制安装校验） | 通过（`38a00bc`） |
| 真实 Linux/Docker/descriptor-relative 测试 | 本地跳过，Ubuntu CI 已覆盖并通过 |
| 真实主机修改 | 未执行 |

27 个 skip 主要是 Linux-only（descriptor-relative 文件操作、Unix socket、POSIX
执行位、stub 回滚、stub nginx/openssl 验收）和 Docker 集成测试；已在 Ubuntu CI
覆盖并通过。

## 4. 部署前仍需完成

1. 在一次性 VM 上做安装器回滚演练（至少包含 unit/timer、版本化 venv 切换、
   一致快照和 NVIDIA 路径故障）。
2. 目标机维护窗口内先记录现状；安装时使用 `--maintenance-window` 让安装器自动
   停止写入者并取一致快照。`--no-timers` 仍可用于 monitor 部署，验收后再启用。
3. 首次安装后运行 `preflight.sh`（默认不探测 NVIDIA）与
   `verify_portal_network.sh`；确认 `service_account`、NVIDIA wrapper/shim 和
   设备权限。需要探测时先安装 wrapper，再用 `--probe-nvidia`。
4. Portal 保持禁用；DNS/VPN/TLS/防火墙验收完成后，才运行
   `enable_portal.sh --yes --approved-cidr <CIDR>` 写标记并启用。
5. development profile + container sudo 保持关闭；先运行
   `verify_gpu_isolation.sh --write-marker` 取得真机 PASS，再评估是否开放。
6. 开放 Portal/apply 浏览器入口前，完成 loopback 代理信任边界改造（权限受限
   Unix socket 或可验证代理身份），见 `docs/acceptance.md` §5.7。
7. 向更多成员推广前，完成 `docs/acceptance.md` §6 的真实科研 Agent 端到端训练
   验收（C-4）：普通成员仅凭正式指南完成受控真实训练并提交体验报告；不阻塞
   本次 monitor 升级。

## 5. 结论

七项 P1 和本轮测试服务器部署阻断项均已在代码和自动化测试层面修复，未触碰生产
主机，未改写 Git 历史，未替换 `v0.1.0`。P1-4 的静默备份、P1-5 的真实 systemd
回滚、版本化 venv 切换和一致快照仍需在 Linux/一次性 VM 上做部署前演练；Portal
必须在网络验收后由 `enable_portal.sh` 启用；development profile/container sudo
必须在 `verify_gpu_isolation.sh` 真机 PASS 并写入标记后才可评估开放。P1-6 的
首次安装信任仍存在 HTTPS 通道 + 指纹比对的残余缺口，已在文档中明确。
