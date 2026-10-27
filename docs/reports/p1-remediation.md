# P1 部署前安全修复报告

- 分支：`fix/p1-pre-deploy`，基于 `main@e987c7e`
- 范围：仅修复审计确认的 P1 项；未连接、未修改生产主机
- 本地验证：**543 passed / 26 skipped**；Ruff check/format、mypy、`bash -n deploy/*.sh` 通过
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
- `install.sh --verify <sha256>` 与 `install.ps1 -Verify <sha256>` 在安装前认证
  自身；不匹配直接退出。
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
- 新增 `--skip-nvidia`：完全跳过 `nvidia-smi` 查询和 compute-process 检查，其余
  静态审计继续执行。
- 两个 nginx 示例加入 `allow <vpn-source-cidr>; deny all;`，并保留
  `X-Citespec-Client-IP $remote_addr` 覆盖。
- 新增只读 `deploy/verify_portal_network.sh`：检查 vhost 是否绑定具体私有地址、
  是否有 `allow/deny all`、是否只反代 loopback、是否转发客户端可控的
  `X-Citespec-Client-IP`、上游端口是否有 wildcard listener，以及 Portal unit 状态。

**证据**：`tests/test_deploy_scripts.py` 覆盖 preflight 关键字/前后快照/wrapper 校验，
nginx 示例的 allow/deny 与 loopback 反代，以及 verify 脚本对合规 vhost 和
wildcard/缺 allow/公共上游/伪造头的拒绝行为。

**残余风险**：

- `--skip-nvidia` 不同时检查 compute process；使用后必须另找时点确认无训练。
- 未安装 wrapper 的主机上，preflight 的探测仍可能改权限，只是现在会明确告警；
  首次安装前应尽量避免在成员可登录时运行。
- `verify_portal_network.sh` 只审计配置和当前 listener，不替代防火墙/VPN；`allow`
  范围仍是占位符，必须由网络管理方填写。
- 路径按 `/usr/bin/nvidia-smi`、`/usr/local/lib/citespec-resource/` 固定；发行版
  路径不同会导致告警而非自动适配。

## 3. 验证汇总

| 检查 | 结果 |
|---|---|
| `python -m pytest -q` | 543 passed, 26 skipped（Windows 本地） |
| `python -m ruff check .` | 通过 |
| `python -m ruff format --check .` | 通过 |
| `python -m mypy src` | 通过（45 个源文件） |
| `bash -n deploy/*.sh` | 通过 |
| 真实 Linux/Docker/descriptor-relative 测试 | 本地跳过，由 Ubuntu CI 覆盖 |
| 真实主机修改 | 未执行 |

26 个 skip 主要是 Linux-only（descriptor-relative 文件操作、Unix socket、POSIX
执行位、stub 回滚）和 Docker 集成测试；需要在推送后的 Ubuntu CI 或目标机验证。

## 4. 部署前仍需完成

1. 推送分支并等待 Ubuntu CI；确认 Linux-only 回滚、WAL、执行位测试通过。
2. 在一次性 VM 上做安装器回滚演练（至少包含 unit/timer 与 NVIDIA 路径故障）。
3. 目标机维护窗口内先执行手动 SQLite `.backup` + checkpoint，再做变更前快照；
   安装使用 `--no-timers`，验收后再启用 timer。
4. 目标机首次安装后运行 `preflight.sh` 与 `verify_portal_network.sh`，确认
   `service_account`、NVIDIA wrapper/shim、设备权限和 Portal 网络边界。
5. Portal 仍保持禁用；nginx 模板的 `allow` 范围、证书、DNS、防火墙由网络管理方
   填写并复核。

## 5. 结论

七项 P1 均已在代码和自动化测试层面修复，未触碰生产主机，未改写 Git 历史，未替换
`v0.1.0`。P1-4 的静默备份和 P1-5 的真实 systemd 回滚仍需要在 Linux 环境做一次
部署前演练；P1-6 的首次安装信任仍存在 HTTPS 通道 + 指纹比对的残余缺口，已在文档
中明确。
