# 管理员指南：成员审批与管理

## 1. 邀请与申请

```bash
# 生成邀请（密钥只显示一次，通过实验室可信渠道发给申请人）
citespec-resource admin invitations --create --validity-hours 168

# 查看申请（list）与详情（show，含自报资料、公钥指纹、版本摘要）
citespec-resource admin requests list
citespec-resource admin requests show --request-id <id>

# 核验：通过实验室既有渠道核对申请人身份；记录核验人与说明。
# 批准必须绑定你实际预览过的申请版本；内容在预览后变化时拒绝批准。
citespec-resource admin requests approve --request-id <id> \
  --expected-version <show返回的version> --review-note "已核验"
# 可选 --valid-until ISO时间、--quota-gb 工作区配额

# 需要申请人补充材料：不创建账号，申请人可看到公开说明。
citespec-resource admin requests needs-info --request-id <id> \
  --review-note "请补充可核验的实验室联系人"

# 拒绝（必须给出申请人可见原因；不创建任何账号/密钥/资源）
citespec-resource admin requests reject --request-id <id> --review-note "身份尚无法核验"
```

> `admin invitations --create` 与 `admin requests import` 直接写 intake 数据库，
> 需要以能写 `/var/lib/citespec-resource-intake` 的身份执行；部署机上用
> `sudo citespec-resource ...`。其余命令（`admin requests approve`、
> `admin members ...` 等）经 broker 执行，**不要**加 sudo，否则会丢失管理员身份。

列表可按状态过滤：`citespec-resource admin requests list --status pending`。批准、
拒绝、补充材料、重试都要求管理员身份；所有变更写审计事件。

批准后系统自动执行开户操作（创建受限账号、安装中央公钥、配置工作区与项目
配额并验证）；操作分阶段留痕，失败可重试：

```bash
citespec-resource admin operations show --operation-id <id>
citespec-resource admin operations retry --operation-id <id>
```

GUI：管理员页新增"加入申请"面板（列表、批准、拒绝、重试）。

离线场景：申请人无法访问申请服务时，可离线生成申请 JSON 文件，管理员用
`citespec-resource admin requests import --file <file>` 导入——走同一验证与审批链路。
申请文件结构（邀请密钥只放在该文件中，导入后删除）：

```json
{
  "invite_id": "<邀请编号>",
  "invite_secret": "<邀请密钥>",
  "application": {
    "display_name": "张三",
    "username": "zhangsan",
    "affiliation": "某实验室 / 导师姓名",
    "purpose": "训练 X 模型",
    "contact": "用于核验的邮箱或电话",
    "nickname": "",
    "public_contact": "",
    "agree_terms": "1",
    "public_keys": ["ssh-ed25519 AAAA... user@host"]
  }
}
```

导入成功后按 `admin requests show --request-id <id>` 预览，再带
`--expected-version` 批准。

## 2. 成员管理

```bash
citespec-resource admin members list
citespec-resource admin members show --member-id <id>   # 状态、有效期、密钥指纹、操作

# 暂停（禁止新申请/新连接；后台训练默认保留至租期结束）
citespec-resource admin members suspend --member-id <id> --reason "暂时离组"
# 恢复（重新验证账号与密钥投影；不复活旧队列或旧连接）
citespec-resource admin members resume --member-id <id> --reason "返组"
# 撤销（阻断访问并撤销凭据；workspace 数据保留，不自动删除）
citespec-resource admin members revoke --member-id <id> --reason "成员资格结束"
```

语义边界：

- **暂停 ≠ 撤销 ≠ 删除**。删除 workspace 不属于成员操作；不提供一键删人。
- 暂停/撤销会立即取消该成员未准入的排队请求，关闭其全部受管 Broker 流/隧道，
  并由执行器终止宿主登录进程；凭据撤销失败时显示"凭据撤销待重试"，访问始终被
  状态阻断。
- 怀疑凭据泄露时应选择撤销（或紧急停训），而不是普通暂停。
- 状态变更全部写入审计事件（操作者、原因、版本号）。

## 3. 有效期与配额

- 成员资格可带 `--valid-until`（ISO 时间）；到期后每次授权即时拒绝（不依赖
  周期任务），新工作环境的到期时间不会晚于资格到期。
- 暂停/恢复/撤销都会递增成员的 access_version，用于竞态防护（排队准入时
  重新核对）。

## 4. 故障处理

- **开户部分失败**（如配额验证未完成）：成员处于 provision_failed，尚未授予
  资源资格；用 `admin operations retry` 重试，阶段日志显示卡在哪一步。
- **重复批准/并发审批**：同一申请只有一个决策生效；重复批准返回同一成员与
  操作，不会创建第二个账号。
- **成员丢失全部公钥**：走管理员恢复（重新安装中央密钥），没有凭姓名/邮箱
  直接重置的后门。

## 5. 公钥与 legacy import

```bash
# 管理员给成员增/撤登录公钥；增删会同步写 root 中央公钥文件。
# 同步失败时数据库变更回滚，不会出现“界面成功但 sshd 不认”的状态。
citespec-resource admin members key-add --member-id <id> --public-key-file alice.pub
citespec-resource admin members key-revoke --member-id <id> --key-id <key-id>

# 已有 Linux 账号：manifest 驱动、默认只做 dry-run；不会自动采用同名账号。
citespec-resource admin members import-legacy --file legacy-members.json
citespec-resource admin members import-legacy --file legacy-members.json --apply
```

`legacy-members.json` 示例：

```json
{
  "username": "alice",
  "display_name": "Alice",
  "valid_until": "2027-06-30T00:00:00+08:00",
  "quota_gb": 200,
  "public_keys": ["ssh-ed25519 AAAA... alice@laptop"]
}
```

dry-run 会检查账号是否存在、UID 是否已被其他成员占用、组是否越权、home 与
workspace 是否安全；任何冲突都拒绝 apply。apply 会把账号 shell、组、中央公钥、
workspace 配额收敛到受管状态。`create_user.sh` 只作为审计过的 break-glass 工具：
成员密钥写入中央目录，管理员账号仍使用自己的 home 公钥。

## 6. VPN Portal 账号与管理员角色

Portal 的审批入口上线后，普通成员的 Portal 账号由审批自动创建为 `member`，不需要
手工建号。管理员角色**没有 Web 修改入口**；首个管理员和后续角色变更使用：

```bash
# 首个管理员：生成的一次性激活码只显示一次，通过可信渠道发送
citespec-resource admin portal create --username labadmin --role admin \
  --member-id <可选成员ID> --display-name "Lab Admin" --contact admin@example.invalid

# 查看本地 Portal 账号（不显示密码、TOTP、激活码）
citespec-resource admin portal list

# 授权/降权；目标账号的现有浏览器会话会被注销
citespec-resource admin portal set-role --username labadmin --role admin
citespec-resource admin portal set-role --username labadmin --role member
```

约束与语义：

- 这些命令只接受 `citespec-admins` 成员，Broker 会重新校验 Linux 组；Portal 服务
  账号不能调用。
- 不能降权最后一个 `active` 管理员；需要先创建、提升并激活另一个管理员。
- 同名 `pending_activation` 账号再次 `create` 会重签一次性激活码；已激活账号只能
  用 `set-role` 调整角色。
- 审批只创建 `member`；普通成员不能通过注册或 Web 表单把自己变成管理员。
- 角色变更、创建账号与审批/暂停等操作都会进入 Portal 和 Broker 审计；激活码、
  密码和 TOTP 不会写入审计。
- Portal 的 `/admin` 页面负责审批和成员状态，不负责角色；资源申请、释放、队列、
  Shell、VS Code、文件传输仍只用 CLI/SSH。
