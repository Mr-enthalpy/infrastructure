# 实现报告：成员自助接入、友好工作流与管理员管理闭环

- 目标文档：`docs/goals/member-onboarding-and-ux.md`（v1.0）
- 本轮起点：`01bc2bf`（工作区干净）
- 本轮结束：改动**尚未提交**；以 `git status`/`git diff` 为准
- 结论：**D1 实现完成（本地测试）；D2 待执行；D3 未授权**

## 1. 实际基线与结束状态

| 项目 | 本轮开始 | 本轮结束（未提交） |
|---|---|---|
| 提交 | `01bc2bf` | `01bc2bf` + 未提交改动 |
| 测试 | 360 passed / 21 skipped | **388 passed / 22 skipped** |
| Ruff check/format | 通过 | 通过 |
| mypy | 通过 | **34 个源文件通过** |
| `node --check` | 通过 | 通过 |
| `bash -n`（部署脚本） | 通过 | 通过 |
| 真实主机部署 | 未执行 | 未执行 |

22 个 skip 中都**没有**把本轮新增功能当作通过：21 个是原有的
Linux/Docker/descriptor-relative 限制，另 1 个是本轮新增的真实浏览器测试因
本机未安装 Playwright 而跳过。skip 不计入 D1/D2 验收。

开发环境：Windows，Python 3.13.5（`.venv`；不得使用系统 Python 3.8），
Git Bash `C:\Program Files\Git\bin\bash.exe`，Node.js。**没有**可用的
disposable Linux VM/WSL/Docker daemon 或浏览器自动化环境，因此没有执行真实
`sshd`、`useradd`、quota、systemd 或浏览器验收。

本轮没有访问或部署 amax，没有创建/修改真实 Linux 账号、SSH 密钥、容器或
GPU 状态。期间一次旧的 CLI 自动中继逻辑把 `admin ... --help` 误判为远端命令并
尝试解析 `amax`，连接未建立，未认证、未修改主机状态；`--help` 不再中继的修复
和回归测试已加入。

## 2. 需求 → 实现模块 → 验收用例

| 目标条目 | 实现模块 | 本地证据 | D2 待证 |
|---|---|---|---|
| AC-01/02/04 旧入口与友好拒绝 | `login_wrapper.py`、`banner` 模板、`member-shell.sh`、菜单 | 白名单/拒绝和菜单回归测试 | 真实 TTY/旧凭据 |
| AC-03 JSON/协议 | `cli.py`、`broker/server.py` | JSON envelope、非交互路径测试 | 真实 SSH 流 |
| AC-05 首次配置 | `onboarding.py`、`config.py` | 严格主机密钥、托管片段、客户端配置、私钥不覆盖测试 | 真实工作站 |
| AC-06/07 快捷创建与多环境 | `cli.py`、`store.py`、`broker/server.py` | `--gpu-count`、预设按张数、幂等 apply、队列冲突测试 | 并发真实请求 |
| AC-08..12 申请/隐私/审批 | `enrollment/`、`membership/`、`broker/server.py`、GUI | 邀请、回执 POST、限流、版本绑定、并发批准测试 | 真实浏览器/反代 |
| AC-13/14 开户与失败恢复 | `membership/service.py`、`provisioning.py`、`deploy/membership/provision_executor.py`、systemd spool | 固定动作执行器、阶段状态、重试/恢复、失败回滚测试 | 真实 useradd/quota |
| AC-15 拒绝 | `membership/service.py`、intake | 拒绝不建成员/账号链测试 | 真实主机 |
| AC-16/17/21 权威状态 | `membership/{models,store}.py`、`broker/server.py` | 状态单调、即时有效期、队列准入复核测试 | 真实时间边界 |
| AC-18/19/20 撤权/暂停/恢复 | `broker/server.py`、`provision_executor.py` | 按 UID 关闭流/隧道、pkill 受管会话、暂停/恢复测试 | 真实 SSH/ControlMaster |
| AC-22 公钥轮换 | `membership/service.py`、中央密钥写入、GUI/CLI | 增删密钥、宿主同步失败回滚、撤销旧 key 测试 | 真实 sshd |
| AC-23/24 管理员与执行器边界 | `broker/server.py`、`provision_executor.py` | 普通成员直调拒绝、执行器动作/字段白名单测试 | 真实 socket/root |
| AC-25 迁移旧数据 | `db.py`、`membership/service.py`、legacy manifest | v2→v4、迁移幂等、manifest dry-run/冲突/显式 apply 测试 | 生产数据库副本 |
| AC-26 真实浏览器 | `enrollment/static`、`gui/static`、`tests/test_intake_browser.py` | 真实浏览器测试代码已提供；本机因缺 Playwright **SKIP** | 必须执行并留截图 |
| AC-27 兼容回归 | 全仓库 | 388 passed；原队列/到期/文件/GPU 预算测试未退化 | 目标机回归 |
| AC-28 服务失败 | CLI/GUI/Broker | 重试幂等、连接失败不伪装成拒绝、操作恢复测试 | 真实 systemd/网络 |
| AC-29 生命周期 | 既有 allocation/cleanup 逻辑 | 原有回归通过 | 真实容器/workspace |
| AC-30 权限投影收敛 | service/executor/broker | DB 变更失败回滚、主机密钥读回、inspect 测试 | 真实主机 |

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
    `.[browser]` extra；本机未安装 Playwright 时 SKIP，不能当作 AC-26 通过。

## 4. 测试命令、环境与结果

实际执行的命令：

```bash
.venv\Scripts\python -m pytest -q
# 388 passed, 22 skipped

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
| PASS | 388 个本地测试：成员状态机、审批版本/并发、操作重试、初始化/迁移、队列/apply 幂等、GPU 张数、Broker 闸门/流关闭、密钥同步回滚、legacy import、setup 主机信任/配置、CLI/JSON、intake HTTP、GUI 路由、执行器固定动作 |
| SKIP | 21 个原有 Linux/Docker/descriptor-relative 测试；1 个新增 Playwright 浏览器测试（本机无 Playwright） |
| FAIL | 无本地失败 |
| BLOCKED | 所有真实 Linux/SSH/浏览器/systemd/quota 项（D2） |
| 未执行 | amax 部署、Docker/NVIDIA 操作、真实账号/密钥、生产数据库迁移、反向代理/TLS、真实 VS Code |

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

## 5. 实际验证过的主路径

本地自动化已经验证：

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

**没有**实际验证过、也没有截图：真实浏览器中的完整管理员审批/进入环境路径；
真实 `sshd` 中央公钥认证；真实 `useradd`/quota；intake/provision systemd 单元；
真实 ControlMaster 撤销；Windows/Linux 工作站真实 SSH。

## 6. 未解决问题、影响与是否阻断试运行

**阻断 D2/试运行的项：**

- 没有 disposable Linux 环境，真实 sshd 的 `authorizedkeysfile`、home-only key
  拒绝、中央密钥、`pkill`/ControlMaster 行为未验证。
- 真实账号/组/workspace/quota、执行器中断恢复和 spool 原子性未在目标发行版验证。
- intake/provision systemd 单元与权限隔离、TLS 反向代理、浏览器申请/审批/查询
  未执行。
- Playwright 浏览器测试本机 SKIP；AC-26 没有 PASS 证据。
- `create_user.sh` 的中央密钥 helper 使用 `os.chown`/`os.chmod`，只在 Windows
  上做过静态检查，未在 Linux 执行。

**不阻断 D1、但会影响运维的项：**

- 旧成员必须显式 legacy import；直接切换 sshd 会锁定未导入成员。部署文档已
  把 dry-run/apply 和“先迁移再切 active”写成前置条件。
- 自定义 `workspace_root`/`state_db` 仍需手工同步 systemd 写路径；执行器固定写
  `/etc`、`/home`、`/data1/citespec-users` 和 spool。
- 反向代理请求体限制、TLS、网络 ACL 由实验室运维提供，不在本仓库内。
- **非阻断 UX 缺口**：`setup` 使用本机已有的 OpenSSH 默认密钥/agent，不提供
  图形化选择或自动生成新密钥；未配置 GUI 首屏的直接 setup 入口尚未实现（入口
  仍是 CLI `setup`）。这不改变密钥/权限路径，但严格对照目标 §5.4 仍是未完成项，
  广泛开放前应补齐。

## 7. 数据迁移、恢复和回滚

- `BASE_SCHEMA_VERSION=2`，目标 `SCHEMA_VERSION=4`；迁移 3/4 有序、事务化、
  幂等，建成员/申请/操作、队列 GPU 张数/幂等键和唯一索引。迁移前用 SQLite
  backup API 备份；全新数据库直接建 v4。
- 开户操作持久化阶段；执行器或 Broker 崩溃后先收敛/标记失败，再允许管理员
  重试。主机写入失败会回滚数据库声明，不把失败投影显示为 active。
- 撤销/删除中央密钥是即时收敛路径；`disable_member` 先禁账号/清密钥/终止会话，
  再记录状态。恢复时重新投影当前有效 keyset，不重放旧队列。
- `import-legacy` 默认 dry-run，只有显式 `--apply` 才修改主机；冲突时不执行。
- 本轮没有执行生产数据库迁移或部署回滚。部署侧仍使用
  `install_server.sh` 的时间戳备份、EXIT trap 和 SQLite 快照；不要在训练/现有
  容器运行时执行。

## 8. 目标机未执行事项及授权

D2 需要：可销毁 Linux 主机/VM、真实 sshd、`useradd`、XFS/ext4 quota、systemd、
真实浏览器/Playwright，以及实验室 TLS 反向代理。执行清单见
`docs/acceptance.md` 5.8。

D3 需要：独立批准、旧权限回收、现有 P5、GPU/存储隔离、真实 VS Code 和恢复
通道验收。**本轮未获 D3 授权**，没有在 amax 部署、重启 Docker/NVIDIA、修改
udev、创建/停止容器或改真实成员账号。

**最终分级：D1 实现完成（本地测试通过；浏览器测试代码已提供但本机 SKIP；
§5.4 的 GUI 首屏/密钥选择 UX 缺口见 §6），D2 待执行，D3 未授权。不得把本报告
表述为 Goal 全部验收通过。**
