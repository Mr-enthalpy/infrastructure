# 资源排队

当 GPU、全局 CPU/内存或每用户预算不足时，`udc-resource apply` 默认创建排队请求，
而不是直接失败。排队请求不会占用 GPU claim，因此不会阻塞其他用户；只有 Broker
真正准入时才创建 allocation 并写入 claim。

## 状态

| 状态 | 含义 |
| --- | --- |
| `queued` | 等待资源；可按位置、取消或等待到期。 |
| `admitted` | 已准入并写入 allocation；`allocation_id` 指向新分配。 |
| `cancelled` | 用户或管理员取消。 |
| `expired` | 超过 `max_queue_hours` 仍未准入。 |
| `failed` | 请求中的 GPU 已不存在或数据损坏。 |

只有 `queued` 请求计入队列长度和用户队列配额。准入后 allocation 的状态
（`pending`/`active`/`failed`）通过 `queue get` 或 `status` 查看。

## 准入规则

Broker 只在 `active` 模式调度队列，且必须满足以下所有条件：

- live `nvidia-smi` 检测成功；检测失败时暂停调度，避免把未知 GPU 分出去；
- 请求的每个 GPU 在合并后的实时状态中为 `free`，且数据库中没有 claim；
- 全局 CPU/内存预算、每用户 CPU/内存预算、每用户 GPU 数和并发分配数均满足；
- requested GPU 索引在数据库 inventory 中存在。

顺序为 `priority DESC, created_at ASC, id`。准入后立即创建容器并激活 allocation；
容器创建或 workspace 准备失败会把 allocation 标为 `failed` 并释放 claim。

调度触发点：Broker 启动、`release`、到期 worker，以及切换到 `active` 模式。准入
成功后继续尝试下一条，直到没有符合条件的请求。`monitor` 或 `maintenance` 模式不会
自动准入。

## CLI

```text
udc-resource apply --gpus 7 --cpus 8 --memory 32 --hours 12
udc-resource apply --gpus 7 --cpus 8 --memory 32 --no-queue
udc-resource apply --gpus 7 --cpus 8 --memory 32 --wait --wait-timeout 3600
udc-resource queue list
udc-resource queue list --history
udc-resource queue list --all --history          # 仅管理员
udc-resource queue wait <queue-id> --timeout 3600
udc-resource queue cancel <queue-id> --reason "不再需要"
```

`apply --wait` 和 `queue wait` 每 2 秒查询一次，直到请求变为 `admitted` 并返回
allocation，或变为终态/超时。普通成员只能查看、取消和等待自己的请求；管理员可加
`--all` 查看所有用户。

## 配置

```toml
[policy]
queue_enabled = true
max_queue_length = 100
max_queued_allocations_per_user = 4
max_queue_hours = 24
```

`max_queue_hours` 从请求创建时开始计时，与 allocation 的 `hours` 租期独立。
workspace 在申请阶段创建；请求取消或过期后不会自动删除，便于用户复用已有文件。
