# 实现报告：成员自助接入、友好工作流与管理员管理闭环

- 目标文档：`docs/goals/member-onboarding-and-ux.md`（v1.0）
- 基线：`0a99bfd`（干净工作区 + 本目标文档）；结束版本见 git log（本报告随最终实现提交）
- 完成等级：**D1 实现完成，可进入隔离验收；D2 待执行；D3 未授权（不在本轮范围）**

## 1. 实际基线与结束状态

- 基线提交 `0a99bfd`：323 passed / 21 skipped，ruff/mypy 干净。
- 结束状态：全部新增模块与测试通过（见第 4 节），工作区干净，逐阶段提交
  （0e7a9f0 → e8b1f98 → ab25e78 → 83591e4 → 3456ce3 → ed6975e → 本提交）。

## 2. M0 差距核对（目标文档 §1.1 → 实现状态）

| 目标条目 | 基线状态 | 本轮结果 |
|---|---|---|
| `ensure_user` 被误用为成员资格 | 部分（仅记录调用者） | 职责分离：`membership/` 权威记录 + `membership_required` 闸门；ensure_user 仍只做身份记录（AC-16 测试） |
| 成员权威状态/有效期/版本 | 缺失 | `members` 表（provisioning→active→suspended/…单调状态机 + access_version + valid_until），每次授权即时核对（AC-21） |
| 审批闭环（邀请→申请→批准） | 缺失 | intake 存储 + HTTP 服务 + admin invitations/requests CLI + GUI 面板（AC-08..12/15） |
| 开户执行器 | 缺失 | 窄执行器 `deploy/membership/provision_executor.py`：固定动作白名单、复用 create_user.sh、中央密钥文件、拒绝系统账号（决策记录见 §3） |
| 撤权覆盖旧连接 | 部分（隧道有取消） | 状态变更即取消该成员排队请求与隧道；中央密钥撤销 + usermod -L；SSH transport 级清理待目标机（见 §7） |
| 友好拒绝/中文引导 | 缺失（英文白名单报错） | login_wrapper 分类拒绝（bash/docker/nvidia-smi/scp/vscode）+ [onboarding] 配置 + 无 TTY 短提示（AC-01/04） |
| 状态感知菜单 | 部分（英文静态菜单） | 中文化 + 空状态引导（AC-02） |
| 快捷创建 `start --gpu-count` | 缺失 | 服务端自动选卡（不在前端认领）；幂等键防重复（AC-06） |
| 申请页/回执隐私 | 缺失 | 邀请/回执仅存哈希；单次邀请；猜测 ID 无效（AC-09） |
| 公钥管理 | 缺失 | member_keys 表 + `me keys list/add/revoke` + 最后一把密钥锁定保护 + 容器投影取权威集合（AC-22 部分，见 §7） |
| 数据迁移 | 无框架 | 版本化迁移 + schema_migrations + 迁移前备份 + 事务回滚（AC-25 部分：无旧库升级实测，见 §7） |

明确不改动（保持既有取舍）：GPU 自助申请未加逐次审批；容器内 sudo / 临时 HOME /
持久 workspace 取舍不变；`apply --gpus` 语义不变；既有 GPU 隔离、预算、
`cleanup_pending` 语义未退化（回归全绿）。

## 3. 与目标文档不同的设计决策

1. **执行器通信采用请求/结果文件 spool + systemd path 单元**，而不是常驻
   worker 或 DB 队列：与现有 SQLite/文件风格一致、无新依赖；执行器幂等
   （结果文件存在即跳过）。决策记录写入执行器 docstring 与报告本节。
2. **成员资格闸门用 `policy.membership_required`（默认 true）** 而非隐式开关：
   新安装默认 fail-closed；测试/过渡环境可显式关闭。不为兼容永久关闭约束
   （§10.2），关闭必须落在配置里、可见可审计。
3. **管理员豁免成员闸门**：沿用 admin 组的服务端可信校验（§6.1），不要求
   管理员也建 member 记录。
4. **中央密钥文件采用"执行器写文件 + sshd 配置注释模板"**：未默认启用
   AuthorizedKeysCommand/AuthorizedKeysFile 变更，避免在目标机验证前改动认证
   链路；启用步骤写入 drop-in 注释与管理员指南（需 `sshd -T` 验证）。
5. **`start --gpu-count` 在无足够空闲 GPU 时直接报错而非入队**：按张数排队需要
   队列表达"任意 N 张"语义（schema 变更），本轮按文档 §3.2 的"申请页默认"
   走现有 `apply --gpus` 排队；记录为已知简化。
6. **intake 状态机简化**：needs_info 状态在存储层支持并由管理员 CLI 预留，
   GUI 第一版仅展示 pending/已处理（文档已注明）。

## 4. 测试命令与结果

环境：Windows 11 + Python 3.13（本地开发机）；Linux-only 路径由 CI 的
ubuntu job 覆盖。所有数字来自实际运行。

```text
python -m pytest -q            → 360 passed, 21 skipped (0 failed)
python -m ruff check .         → All checks passed
python -m ruff format --check  → 全部格式化
python -m mypy src             → Success (34 source files)
node --check app.js            → OK
py_compile provision_executor  → OK
bash -n deploy/*.sh            → OK（目标文档要求；本轮新增执行器为 Python）
```

新增测试（约 60 个断言组）：

- `tests/test_membership.py`：门控（apply/shell/tunnel/file）、管理员豁免、
  到期即时拒绝、expires_at 钳制、排队准入成员校验、幂等 apply、
  ensure_user 不产生成员资格（AC-16/17/21）。
- `tests/test_membership_service.py`：批准开户、双审批收敛（AC-11）、拒绝零
  创建（AC-15）、失败可重试 + provision_failed 重入（AC-14）、迟到开户不复活
  撤销成员（§13）、intake 隐私/私钥拒绝（AC-09/10）、执行器请求白名单。
- `tests/test_intake_server.py`：HTTP 入口邀请校验、回执隐私、私钥干净拒绝、
  大请求拒绝、取消（AC-08/09/10）。
- `tests/test_provision_executor.py`：未知动作/系统账号拒绝（AC-24 部分）、
  spool 幂等。
- `tests/test_onboarding.py`：托管片段不覆盖既有配置/私钥（AC-05）。
- `tests/test_gui_server.py` 扩展：成员管理端点与原因必填。
- `tests/test_login_wrapper.py` 扩展：分类、脱敏、无 URL 诚实提示、ANSI 剥离。

SKIP（21 个）全部为既有 Linux-only 用例（UDS/O_NOFOLLOW/描述符相对 IO），
原因与基线一致，未新增跳过。

## 5. 主路径证据（应用层实测）

- 路径 B（新成员）：`test_intake_server`（提交→回执→查询→取消）+
  `test_membership_service`（批准→开户→active；拒绝→零创建）+ CLI 命令
  （join/admin requests/operations）。**真实 Linux useradd/usermod/配额与
  浏览器点击未执行**——需要 D2 隔离环境（见 §7）。
- 路径 A（老成员）：`test_login_wrapper`（分类拒绝/协议纯净/无 TTY）+
  `test_cli`（菜单/命令）；真实 sshd Banner 显示需目标机。
- 路径 C（管理员）：`test_membership`（暂停成员的排队在准入时失败）+
  service 测试（suspend/resume/revoke 转换与凭据撤销投影）；真实 ControlMaster
  断开与 usermod -L 需隔离环境。

## 6. 数据迁移、恢复与回滚

- `db.py`：`schema_migrations` 表 + 逐版本迁移；迁移 3 新增 member/member_keys/
  member_operations/membership_applications 四表与 allocations.idempotency_key
  （部分唯一索引）。迁移前对数据库文件做时间戳备份；失败整体回滚并在错误中
  给出备份路径。既有 allocation/claim/queue/audit 数据不被触碰。
- 回滚：恢复 .bak 文件即可（成员功能表丢弃，资源数据完整）。
- 升级实测覆盖：旧 v2 库（无成员表）升级路径与全新库路径均有测试。

## 7. 未解决问题 / 未验证项 / 所需授权

| 项 | 等级 | 说明 |
|---|---|---|
| 隔离环境集成验收（D2） | 阻断 D2 | 真实 sshd + useradd/usermod -L + 中央密钥 + Banner 显示 + ControlMaster 断开；需可销毁 Linux VM。已提供 executor spool 测试与 systemd 模板作为可执行入口。 |
| 浏览器端到端（AC-26） | 阻断 D2 | GUI/申请页真实点击；本地未安装 Playwright，测试文件与端点已就绪。 |
| 旧库升级实测（AC-25 完整） | 阻断 D2 | 升级路径有测试，但未在含真实历史数据的库上演练（备份/回滚机制已备）。 |
| SSH transport 撤权（§8.4 步骤 4） | 阻断 D2/D3 | sshd 会话级终止需要 sshd 控制或 root 会话枚举，当前实现覆盖：状态拒绝 + 队列取消 + 隧道关闭 + 中央密钥撤销 + 账号锁定；实时 transport 终止需在隔离环境补齐。 |
| 目标机上线验收（D3/P5） | 需单独授权 | GPU 设备节点（acceptance §5.6）、overlay2/XFS 存储、旧权限回收、生产 VS Code。 |
| 邀请投递渠道 | 非阻断 | 邀请通过实验室既有渠道人工发送（目标文档 §5.1 明确不接邮件/短信）。 |

## 8. D1/D2/D3 结论

- **D1（实现完成、可进入隔离验收）：达成。** 第一版范围有真实代码，无占位
  按钮/未实现命令；三条主路径应用层完整；授权覆盖普通 RPC 与特殊流入口；
  幂等、竞态、失败恢复均有测试；全部本地回归通过；未执行项已显式列出。
- **D2（隔离环境验证）：未执行。** 需可销毁 Linux VM（真实 sshd/账号/
  usermod/配额 + 浏览器）。测试入口与模板已备好，待环境授权。
- **D3（目标服务器开放）：未授权、未执行。** 依赖 D2 与既有 P5 验收
  （GPU 设备节点、存储、旧权限回收），且须管理员单独批准。
