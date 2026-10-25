# 资源排队

## 1. 设计目标

当 GPU、全局 CPU/内存或每用户预算不足时，`udc-resource apply` 默认创建排队请求，
而不是直接失败。排队请求不占用 GPU claim，因此不会阻塞其他用户。Broker 仅在真正
准入时创建 allocation 并写入 claim。

队列必须保持严格的先来先服务顺序，并同时满足预算、GPU 占用和用户配额约束。任何
直接申请都必须尊重已有排队请求，不得通过 `--no-queue` 绕过队列。

## 2. 状态

| 状态 | 含义 |
| --- | --- |
| `queued` | 等待资源；可查询位置、取消或等待到期。 |
| `admitted` | 已准入并写入 allocation；`allocation_id` 指向新分配。 |
| `cancelled` | 用户或管理员取消。 |
| `expired` | 超过 `max_queue_hours` 仍未准入。 |
| `failed` | 请求中的 GPU 已不存在或请求数据损坏。 |

只有 `queued` 请求计入队列长度和用户队列配额。准入后，allocation 的状态
（`pending`、`active` 或 `failed`）通过 `queue get` 或 `status` 查询。

## 3. 准入规则

Broker 仅在 `active` 模式下调度队列，且必须同时满足以下条件：

- live `nvidia-smi` 检测成功；检测失败时暂停调度，避免分配未知 GPU；
- 请求的每个 GPU 在合并后的实时状态中为 `free`，且数据库中不存在 claim；
- 全局 CPU/内存预算、每用户 CPU/内存预算、每用户 GPU 数量和并发 allocation
  数量均满足；
- 请求的 GPU 索引存在于数据库 inventory 中。

调度顺序为 `priority DESC, created_at ASC, id`。准入后立即创建容器并激活
allocation；容器创建或 workspace 准备失败时，将 allocation 标记为 `failed` 并
释放 claim。

所有 `apply` 都必须尊重队列。存在未过期的 `queued` 请求时，必须先触发调度；若
队首仍无法准入，直接申请必须进入队列，或在 `--no-queue` 下被拒绝，不得越过队列。
只有队列为空且资源可用时，申请才可直接创建 allocation。

`--no-queue` 仅表示“不等待、不保留请求”。它不会绕过已存在的排队请求，也不会在
资源不足时创建 allocation。

GPU 权限审计、workspace 配额检查或 Docker 存储预检失败时，队列调度暂停并保留
请求。审计或预检恢复后，Broker 可继续按顺序准入。

## 4. 调度触发与模式

队列调度在以下事件发生时触发：

- Broker 启动；
- 用户释放 allocation；
- 到期 worker 处理过期 allocation；
- Broker 切换到 `active` 模式。

准入成功后，Broker 继续尝试下一条请求，直到没有符合条件的请求。`monitor` 和
`maintenance` 模式不会自动准入队列。

`monitor` 为只读模式。存在 active/pending lease 时，应使用 `maintenance`：阻止
新申请，但继续执行到期和资源对账。

## 5. CLI

```text
udc-resource apply --gpus 7 --cpus 8 --memory 32 --hours 12
udc-resource apply --gpus 7 --cpus 8 --memory 32 --no-queue
udc-resource apply --gpus 7 --cpus 8 --memory 32 --wait --wait-timeout 3600
udc-resource queue list
udc-resource queue list --history
udc-resource queue list --all --history
udc-resource queue wait <queue-id> --timeout 3600
udc-resource queue cancel <queue-id> --reason "不再需要"
```

`apply --wait` 和 `queue wait` 每 2 秒查询一次，直到请求变为 `admitted` 并返回
allocation，或进入终态/超时。普通成员只能查看、取消和等待自己的请求；管理员可使
用 `--all` 查看所有用户。

## 6. 配置

```toml
[policy]
queue_enabled = true
max_queue_length = 100
max_queued_allocations_per_user = 4
max_queue_hours = 24
```

`max_queue_hours` 从请求创建时开始计时，与 allocation 的 `hours` 租期相互独立。
workspace 仅在 allocation 真正创建时（直接申请或队列准入）创建。取消或过期的排队
请求不会留下空 workspace；已存在的 workspace 不会被自动删除，以便用户复用已有
文件。

`queue_enabled = false` 仅阻止创建新的排队请求，不会允许直接申请越过未过期的排队
请求。只要队列中存在未过期请求，直接申请仍必须遵守队列顺序。

## 7. 运维要求

- 不得手工修改 `queue_requests` 表来改变顺序或绕过预算检查。
- 管理员取消请求时，应记录原因并保留审计事件。
- 队列调度暂停时，应检查 GPU 权限审计、workspace 配额和 Docker 存储预检结果。
- 准入失败后，应确认 claim 已释放且 allocation 已标记为终态，再继续下一条请求。
- 不得在存在 active/pending lease 时将模式切换为 `monitor`；应使用 `maintenance`。
