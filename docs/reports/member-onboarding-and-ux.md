# 实现报告：成员自助接入、友好工作流与管理员管理闭环

- 目标文档：`docs/goals/member-onboarding-and-ux.md`（v1.0）
- 本轮起点：`01bc2bf`（工作区干净）
- 本轮 cutover 结束：`d0294be`（功能/cutover 提交 `8772763`、`d0294be`；CI
  `40308426242` 通过）；文档校准见本次提交
- 结论：**D1 完成；D2 核心实机验收完成；D3 对外生产开放未完成**

## 1. 实际基线与结束状态

| 项目 | 本轮开始 | 本轮结束 |
|---|---|---|
| 提交 | `01bc2bf` | `d0294be`（cutover）；文档校准见本次提交 |
| 测试 | 360 passed / 21 skipped | **392 passed / 21 skipped** |
| Ruff check/format | 通过 | 通过 |
| mypy | 通过 | **34 个源文件通过** |
| `node --check` | 通过 | 通过 |
| `bash -n`（部署脚本） | 通过 | 通过 |
| 真实主机部署 | 未执行 | **active 核心验收完成** |
| GitHub Actions | 未运行 | **运行 `40308426242` 通过** |

21 个 skip 是环境限制：Linux/Docker/descriptor-relative 项由 Ubuntu CI 覆盖，
Playwright 浏览器任务待接入 CI。本轮新增的本地浏览器路径已在实机完成。

开发环境：Windows，Python 3.13.5（`.venv`），Git Bash
`C:\Program Files\Git\bin\bash.exe`，Node.js。本地没有 Docker/WSL；真实
Linux/SSH/浏览器验收在实机完成。

本轮没有改动 amax，没有重启其 Docker/NVIDIA 服务，也没有修改其容器或资源分配。
期间一次旧 CLI 自动中继逻辑把 `admin ... --help` 误判为远端命令并尝试解析
`amax`，连接未建立；该缺陷已修复并加入回归测试。

## 2. 需求 → 实现模块 → 验收状态

| 目标条目 | 实现模块 | 本地证据 | 实机状态 |
|---|---|---|---|
| AC-01/02/04 旧入口与友好拒绝 | `login_wrapper.py`、`banner` 模板、`member-shell.sh`、菜单 | 白名单/拒绝和菜单回归测试 | 核心 SSH 路径完成 |
| AC-03 JSON/协议 | `cli.py`、`broker/server.py` | JSON envelope、非交互路径测试 | 真实 SSH 流完成 |
| AC-05 首次配置 | `onboarding.py`、`config.py` | 严格主机密钥、托管片段、客户端配置、私钥不覆盖测试 | 本地路径完成；跨工作站待补 |
| AC-06/07 快捷创建与多环境 | `cli.py`、`store.py`、`broker/server.py` | `--gpu-count`、预设按张数、幂等 apply、队列冲突测试 | 真实并发请求完成 |
| AC-08..12 申请/隐私/审批 | `enrollment/`、`membership/`、`broker/server.py`、GUI | 邀请、回执 POST、限流、版本绑定、并发批准测试 | 本地浏览器完成；外部 HTTPS 阻塞 |
| AC-13/14 开户与失败恢复 | `membership/service.py`、`provisioning.py`、`deploy/membership/provision_executor.py`、systemd spool | 固定动作执行器、阶段状态、重试/恢复、失败回滚测试 | 真实 useradd/quota 核心路径完成 |
| AC-15 拒绝 | `membership/service.py`、intake | 拒绝不建成员/账号链测试 | 完成 |
| AC-16/17/21 权威状态 | `membership/{models,store}.py`、`broker/server.py` | 状态单调、即时有效期、队列准入复核测试 | 核心路径完成 |
| AC-18/19/20 撤权/暂停/恢复 | `broker/server.py`、`provision_executor.py` | 按 UID 关闭流/隧道、pkill 受管会话、暂停/恢复测试 | 核心路径完成；ControlMaster 待补 |
| AC-22 公钥轮换 | `membership/service.py`、中央密钥写入、GUI/CLI | 增删密钥、宿主同步失败回滚、撤销旧 key 测试 | 中央密钥路径完成 |
| AC-23/24 管理员与执行器边界 | `broker/server.py`、`provision_executor.py` | 普通成员直调拒绝、执行器动作/字段白名单测试 | 核心路径完成 |
| AC-25 迁移旧数据 | `db.py`、`membership/service.py`、legacy manifest | v2→v4、迁移幂等、manifest dry-run/冲突/显式 apply 测试 | 生产数据库副本待补 |
| AC-26 真实浏览器 | `enrollment/static`、`gui/static`、`tests/test_intake_browser.py` | 浏览器测试代码；本地手动路径完成 | 外部入口阻塞；Playwright CI 待接入 |
| AC-27 兼容回归 | 全仓库 | **392 passed**；队列/到期/文件/GPU 预算回归通过 | 核心回归完成 |
| AC-28 服务失败 | CLI/GUI/Broker | 重试幂等、连接失败不伪装成拒绝、操作恢复测试 | 真实 systemd/网络故障待补 |
| AC-29 生命周期 | 既有 allocation/cleanup 逻辑 | 原有回归通过 | active 验收完成 |
| AC-30 权限投影收敛 | service/executor/broker | DB 变更失败回滚、主机密钥读回、inspect 测试 | 核心路径完成 |

## 3. 与目标文档不同的设计决策

1. **中央公钥是唯一权威来源。** sshd 配置为
   `AuthorizedKeysFile /etc/citespec-resource/authorized-keys.d/%u`，刻意不保留
   `~/.ssh/authorized_keys` 回退。成员被撤销后不能在自己的 home 重建登录凭据。
   管理员仍使用普通管理员账号的 home 公钥。代价是必须先把旧成员密钥显式导入
   中央目录，否则切换 sshd 后会锁定旧成员。
2. **同名账号绝不自动采用。** 只有管理员 manifest + `--apply` 的
   `admin members import-legacy` 才能把已有账号变成成员。默认 dry-run，报告
   UID/组/账号形状冲突；这比“按用户名自动接管”更保守，也更符合 AC-25。
3. **审批绑定申请内容版本。** `approve` 接受 `--expected-version`；预览后内容
   变化会拒绝批准；新增 `needs_info`，避免把“信息不足”混成拒绝。并发批准只有
   一个决定生效。
4. **开户执行器是固定动作的 root 进程，不是通用 root SSH。** Broker 只写 spool
   JSON；执行器只接受 `provision_member`、`set_keys`、`disable_member`、
   `enable_member`、`inspect_member` 等动作和固定字段。失败保留阶段和可重试
   状态；快照/回读避免“数据库成功、sshd 不认”。
5. **会话撤权不只靠新连接检查。** Broker 按 peer UID 跟踪已认证流；暂停/撤销/
   密钥同步失败会关闭该成员的流和隧道，执行器另用 `pkill -u <member>` 终止
   宿主登录进程。只作用于目标成员，避免跨成员 DoS。
6. **首次主机信任必须显式确认。** 删除 `StrictHostKeyChecking=accept-new` 的
   便捷路径；`setup` 展示 SHA256 指纹，用户核对后才写入 known_hosts，并支持
   `--fingerprint SHA256:...` 与管理员公布值精确比对（不匹配则失败且不写
   known_hosts）。托管 SSH 片段和独立 known_hosts 不覆盖用户已有配置/私钥。
7. **成员工作站不复制服务器 TOML。** `setup --server ... --user ...` 可在没有
   本地配置时运行，并生成 `~/.config/citespec-resource/client.toml`，其中只有连接
   目标、用户名、端口、known_hosts 等客户端信息。服务器硬件预算和管理员策略
   不落到工作站。
8. **队列/apply 幂等和 GPU 张数分离。** 队列请求保存请求的 GPU 张数和幂等键；
   apply 重试不重复建环境；`start --preset` 中的 GPU 值按“张数”解释，而不是
   卡号索引。
9. **intake 只绑定回环地址。** 公共 HTTPS 入口由实验室既有反向代理提供；本仓库
   不安装/配置代理。申请页使用真实 HTML 表单、POST-only 回执、CSP、安全头和
   按 IP 限流，intake 账号没有 Broker socket、成员库、Docker 或 GPU 访问。
10. **浏览器测试是可选依赖。** 提供 `tests/test_intake_browser.py` 和
    `.[browser]` extra；Playwright 未安装时该测试 SKIP，AC-26 的实机/CI 证据
    见 §5。

## 4. 测试命令、环境与结果

实际执行的命令：

```bash
.venv\Scripts\python -m pytest -q
# 392 passed, 21 skipped

.venv\Scripts\python -m ruff check .
# All checks passed!
.venv\Scripts\python -m ruff format --check src tests
# 通过

.venv\Scripts\python -m mypy src
# Success: no issues found in 34 source files

node --check src\citespec_resource\gui\static\app.js
# 通过

"C:\Program Files\Git\bin\bash.exe" -n deploy/*.sh
# 通过
```

| 结果 | 内容 |
|---|---|
| PASS | 392 个本地测试：成员状态机、审批版本/并发、操作重试、初始化/迁移、队列/apply 幂等、GPU 张数、Broker 闸门/流关闭、密钥同步回滚、legacy import、setup 主机信任/配置、CLI/JSON、intake HTTP、GUI 路由、执行器固定动作 |
| SKIP | 21 个环境限制项：Linux/Docker/descriptor-relative 和 Playwright 浏览器测试 |
| FAIL | 无本地失败 |
| 已完成 | 真实 SSH 核心路径、useradd/quota、intake/provision、active 验收、GPU 审计、restricted profile |
| 未覆盖 | 外部 443 onboarding、VS Code、文件传输、5.6、reboot/driver reload 复验、Playwright CI |
| amax | 未改动；未执行 Docker/NVIDIA 操作 |

### 4.1 本轮新增/加强的主要测试

- `tests/test_db.py`：迁移 3/4 的版本顺序、备份、幂等、唯一索引、全新 v4。
- `tests/test_membership_service.py`：版本绑定审批、并发决定、`needs_info`、
  暂停/恢复/撤销、重试、密钥同步/回滚、legacy import dry-run/冲突/apply。
- `tests/test_provision_executor.py`：固定动作/字段、只读 inspect、中央密钥
  原子写/读回、home 密钥清理、非托管账号拒绝。
- `tests/test_broker.py`：成员 RPC 闸门、按 UID 流关闭、启动恢复、队列幂等/
  GPU 张数、管理员密钥、legacy import RPC。
- `tests/test_gui_server.py`：申请/成员/操作详情、`needs_info`、版本绑定、
  密钥增删、apply 幂等键保持。
- `tests/test_cli.py`：`join` 无本地配置、`me` 组/位置参数、`setup --server/
  --user`、`--help` 不中继、客户端 profile 加载、幂等参数。
- `tests/test_onboarding.py`：严格主机密钥、托管片段、指纹确认、不覆盖用户
  配置/私钥。
- `tests/test_config.py`：客户端最小配置、部分服务器 TOML 不被误当客户端配置。
- `tests/test_intake_browser.py`：真实 Chromium 提交/回执/查询路径（可选依赖）。

## 5. 验证结果

本地自动化已验证：

1. **邀请 → 申请 → 审批**：API/CLI/GUI 路由层提交、回执私有查询、版本变化拒绝、
   并发批准唯一决定、拒绝不创建成员。
2. **批准 → 操作**：Broker 写入 spool，假执行器按阶段返回；失败/中断后可重试；
   密钥同步失败会回滚数据库状态。
3. **权威状态**：`ensure_user` 或重新登录不能把 suspended/revoked 变回 active；
   过期即时拒绝；撤权关闭该 UID 的 Broker 流/隧道。
4. **队列/apply**：重复幂等键不重复创建；`gpu_count` 由服务端选卡；预设张数
   不被当成卡号。
5. **旧成员导入**：默认 dry-run；同名但未显式导入的账号不被采用；冲突时 apply
   拒绝。
6. **工作站配置**：无服务器 TOML 时用 `--server/--user` 生成托管片段和
   `client.toml`；首次信任要求确认指纹；不覆盖用户 SSH 配置/私钥。
7. **GUI/申请页静态与 HTTP 路径**：HTML 表单字段、POST-only 回执、CSP/安全头、
   限流、详情/密钥/生命周期控件存在并通过路由测试。

实机核心路径已验证：

- 一次性成员 intake 申请、审批、开户、撤权和清理。
- 真实 SSH 白名单/拒绝、中央公钥、restricted profile、workspace 配额。
- intake/provision systemd 单元、GPU 审计、active acceptance、到期/重启和
  workspace 保留。

未覆盖：

- 外部 443/TLS onboarding、VS Code、文件传输、容器 root/宿主 GPU 5.6。
- 真实 ControlMaster 撤销、跨 Windows/Linux 工作站完整路径。
- Playwright 浏览器 CI、生产数据库迁移副本、reboot/driver reload 复验。
- `create_user.sh` 中央密钥 helper 的独立 Linux 执行验证；成员开户主路径走
  `provision_executor.py`。

## 6. 剩余工作与优先级

| 优先级 | 项目 | 下一步 |
|---|---|---|
| P0 | 容器 root/宿主 GPU 5.6 | 在目标机执行 acceptance 5.6，保留前后 stat/权限证据 |
| P0 | 备份、恢复和告警 | 定期备份、恢复演练、失败告警 |
| P0 | SSH/管理员/审计 | 分类 27 条 warning，确认无 critical/high |
| P1 | 安装器可复现性 | 固定镜像 digest，干净主机 dry-run/回滚 |
| P1 | CI 覆盖 | Playwright 浏览器 job、必要 Linux 集成项 |
| P1 | GPU 验收脚本 | 自动发现实际 GPU 数，避免默认 8 卡假设 |
| P1 | Docker 存储路线 | 保持 ext4 + restricted，或迁移到 XFS + prjquota |
| P2 | VS Code / 文件传输 | 依赖 5.6、容器 sshd、隧道、配额和 TOCTOU 验收 |
| P2 | `setup` 图形化密钥配置 | GUI 首屏入口和密钥选择/生成 |
| P2 | 历史容器/备份/扫描噪声 | 资源标注、归档和扫描输出清理 |
| Blocked | 外部 443/DNS/TLS | 等待网络/系统管理员确认 NAT、DNS、TLS |

运维约束：

- 当前 Docker root 为 ext4，development profile 被存储闸门拒绝。
- 旧成员必须显式 legacy import；直接切换 sshd 会锁定未导入成员。
- 自定义 `workspace_root`/`state_db` 需要手工同步 systemd 写路径；执行器写路径固定为
  `/etc`、`/home`、`/data1/citespec-users` 和 spool。
- 外部反向代理的请求体限制、TLS 和网络 ACL 由实验室运维提供。

## 7. 数据迁移、恢复和回滚

- `BASE_SCHEMA_VERSION=2`，目标 `SCHEMA_VERSION=4`；迁移 3/4 有序、事务化、
  幂等，建成员/申请/操作、队列 GPU 张数/幂等键和唯一索引。迁移前用 SQLite
  backup API 备份；全新数据库直接建 v4。
- 开户操作持久化阶段；执行器或 Broker 崩溃后先收敛/标记失败，再允许管理员
  重试。主机写入失败会回滚数据库声明，不把失败投影显示为 active。
- 撤销/删除中央密钥是即时收敛路径；`disable_member` 先禁账号/清密钥/终止会话，
  再记录状态。恢复时重新投影当前有效 keyset，不重放旧队列。
- `import-legacy` 默认 dry-run，只有显式 `--apply` 才修改主机；冲突时不执行。
- 本轮没有执行生产数据库迁移副本或部署回滚演练。部署侧使用
  `install_server.sh` 的时间戳备份、EXIT trap 和 SQLite 快照；执行前确认维护窗口。

## 8. 实机验收与未开放范围

D2 核心已完成：一次性成员、真实 SSH、`useradd`/quota、systemd intake/provision、
本地浏览器路径、restricted profile、GPU 审计和 active acceptance 均已验证。

D3 尚未开放，剩余项包括：

- 外部 443/DNS/TLS onboarding；
- VS Code、文件传输和容器 root/宿主 GPU 5.6；
- 备份恢复、监控告警、安装器可复现性和浏览器 CI；
- reboot/NVIDIA driver reload 后的复验。

amax 未改动，未重启 Docker/NVIDIA，未修改 udev，未创建/停止容器或修改真实成员账号。

**最终分级：D1 完成；D2 核心实机验收完成；D3 未开放。**
