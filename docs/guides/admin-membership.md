# 管理员指南：成员审批与管理

## 1. 邀请与申请

```bash
# 生成邀请（密钥只显示一次，通过实验室可信渠道发给申请人）
udc-resource admin invitations --create --validity-hours 168

# 查看申请（list）与详情（show，含自报资料、公钥指纹、版本摘要）
udc-resource admin requests list
udc-resource admin requests show --request-id <id>

# 核验：通过实验室既有渠道核对申请人身份；记录核验人与说明。
# 批准（可选 --valid-until 有效期、--quota-gb 工作区配额）
udc-resource admin requests approve --request-id <id> --review-note "已核验"

# 拒绝（必须给出申请人可见原因；不创建任何账号/密钥/资源）
udc-resource admin requests reject --request-id <id> --review-note "身份尚无法核验"
```

批准后系统自动执行开户操作（创建受限账号、安装中央公钥、配置工作区与项目
配额并验证）；操作分阶段留痕，失败可重试：

```bash
udc-resource admin operations show --operation-id <id>
udc-resource admin operations retry --operation-id <id>
```

GUI：管理员页新增"加入申请"面板（列表、批准、拒绝、重试）。

离线场景：申请人无法访问申请服务时，可离线生成申请 JSON 文件，管理员用
`udc-resource admin requests import --file <file>` 导入——走同一验证与审批链路。

## 2. 成员管理

```bash
udc-resource admin members list
udc-resource admin members show --member-id <id>   # 状态、有效期、密钥指纹、操作

# 暂停（禁止新申请/新连接；后台训练默认保留至租期结束）
udc-resource admin members suspend --member-id <id> --reason "暂时离组"
# 恢复（重新验证账号与密钥投影；不复活旧队列或旧连接）
udc-resource admin members resume --member-id <id> --reason "返组"
# 撤销（阻断访问并撤销凭据；workspace 数据保留，不自动删除）
udc-resource admin members revoke --member-id <id> --reason "成员资格结束"
```

语义边界：

- **暂停 ≠ 撤销 ≠ 删除**。删除 workspace 不属于成员操作；不提供一键删人。
- 暂停/撤销会立即取消该成员未准入的排队请求并关闭其 VS Code 隧道；
  凭据撤销失败时显示"凭据撤销待重试"，访问始终被状态阻断。
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
