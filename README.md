# 按轮推进的推理调度器（Round Scheduler）

本地基础设施组件：模拟长提示词进入模型服务时，**已经开始出词的会话不能被打断**、
必须继续逐轮推进的推理调度过程。纯 Python 标准库实现，无第三方依赖、无网络访问。

## 1. 目录结构

```
.
├── scheduler/
│   ├── __init__.py        # 公共 API 导出
│   └── scheduler.py       # RoundScheduler 及全部数据结构
├── tests/
│   └── test_scheduler.py  # 本地合成数据单元测试（unittest）
└── README.md
```

## 2. 资源模型

调度器管理两类相互独立的资源：

| 资源 | 构造参数 | 含义 |
| --- | --- | --- |
| 每轮 Token 算力槽 | `slot_budget`（B） | 一轮最多处理的 Token 步数；同时是并发会话数上限，活跃会话不会超过 B |
| KV 缓存容量 | `cache_capacity`（C） | 全部会话预留缓存的总和上限，单位为 Token |

会话在**接纳时**按 `prompt_len + max_new_tokens` **一次性预留**整条缓存；
会话终止（停止符 / 输出上限 / 取消）的**当轮轮末**才释放，释放出的空间**下一轮**可用。

## 3. 每轮的固定处理顺序

调用一次 `scheduler.step(token_provider)` 即推进一轮，轮内严格按以下顺序执行：

1. **接纳（admission）**：按入队序扫描等待队列。
   - 等待期间被取消的会话直接移除并以 `CANCELLED` 上报，不占缓存；
   - 单条预留需求 `prompt_len + max_new_tokens > cache_capacity`：**永久拒绝**，
     通过 `RoundResult.rejected` 返回；
   - 并发会话数已达 B，或当前剩余缓存放不下：**跳过**，保留其入队次序，
     队列后面的请求继续尝试接纳（队首阻塞不会拖死整条队列）；
   - 其余请求接纳进入提示词处理态（prefill），并立即预留缓存。
2. **解码（decode）**：先给每个已完成提示词处理的会话各排**一个解码步**，每步占一个槽；
   本步生成的 Token 由调用方通过 `token_provider` 注入。
3. **提示词处理（prefill）**：剩余算力槽按入队序分给尚未处理完提示词的会话，
   每槽消耗一个提示词 Token，可跨轮分段处理。**本轮刚处理完提示词的会话本轮不出词，
   从下一轮开始解码。**
4. **轮末收尾（finalize）**：命中停止符（`STOP`）、达到最大输出长度（`LENGTH`）、
   被取消（`CANCELLED`）的会话统一在轮末释放缓存预留，并通过 `RoundResult.finished` 上报。

该顺序保证了：只要会话已经开始出词，每一轮都会先拿到一个解码槽，新提示词只能使用
解码之后的剩余算力。

## 4. 公共 API

### 构造与入队

```python
from scheduler import RoundScheduler

sch = RoundScheduler(slot_budget=4, cache_capacity=100, stop_tokens=["<eos>"])
sch.add_request("req-1", prompt_len=10, max_new_tokens=8)
```

- `add_request(request_id, prompt_len, max_new_tokens)`：入队，返回只读 `SessionInfo`。
  超容量请求不在此处抛错，而是在下一轮接纳时拒绝，便于调用方在轮结果中统一观察。
- `cancel_request(request_id) -> bool`：
  - 等待中：下一轮接纳扫描时移除，以 `CANCELLED` 上报；
  - 活跃中：下一轮不再被排任何工作，轮末释放并以 `CANCELLED` 上报；
  - 已终止 / 未知 ID：返回 `False`。

### 推进轮次

```python
def token_provider(request_id: str, round_index: int) -> str:
    return model_generate_one_token(request_id)   # 由调用方对接真实/模拟模型

result = sch.step(token_provider)
```

`token_provider` 也可以直接传 `{request_id: token}` 映射；只有本轮真的存在解码会话时
才会被调用，纯提示词处理轮可以不传。缺少 provider 却存在解码会话时抛 `ValueError`。

### `RoundResult` 字段（调用方逐轮可见的信息）

| 字段 | 含义 |
| --- | --- |
| `round_index` / `round_number` | 0 基下标 / 1 基序号 |
| `decode_work` | 本轮谁在出词：会话 ID、注入的 Token、输出下标、是否终止及原因 |
| `prompt_work` | 本轮谁处理了哪段提示词：`[prompt_start, prompt_end)`、处理 Token 数、是否处理完 |
| `finished` | 轮末终止并释放预留的会话（停止符 / 上限 / 取消），含完整输出序列 |
| `rejected` | 本轮被拒绝的入队请求及拒绝原因 |
| `active_session_ids` / `waiting_session_ids` | 轮末仍活跃 / 仍等待的会话（入队序） |
| `decode_slots_used` / `prefill_slots_used` / `unused_slots` | 槽位核算，三者之和为 B |
| `cache_reserved_start` / `cache_reserved_end` | 本轮开始与轮末释放后的缓存预留量 |

其他辅助接口：`has_pending_work()`、`get_session(id)`（任意状态的只读快照）、
`cache_reserved`、`round_index` 等属性。

### 终止判定规则

- 注入的 Token 命中 `stop_tokens`：原因 `STOP`，**停止符本身保留在输出序列中**；
- 输出 Token 数达到 `max_new_tokens`：原因 `LENGTH`；
- 同一步同时命中两者时，**`STOP` 优先**；
- 取消原因 `CANCELLED` 在轮末统一落定。

## 5. 一个端到端小例子

B=3、C=10，停止符 `<eos>`：

```python
sch = RoundScheduler(3, 10, stop_tokens=["<eos>"])
sch.add_request("a", prompt_len=2, max_new_tokens=2)  # 预留 4
sch.add_request("b", prompt_len=4, max_new_tokens=2)  # 预留 6

r0 = sch.step()
# a 先拿 2 槽处理完提示词；剩余 1 槽给 b，b 只处理提示词 [0,1)
# r0.prompt_work -> [a: [0,2) completed=True, b: [0,1) completed=False]

r1 = sch.step()
# b 再拿 2 槽处理 [1,3)，仍剩 1 个提示词 Token；本轮无解码

r2 = sch.step()
# b 处理完最后 1 个提示词 Token，状态转 decoding，但本轮仍不出词

r3 = sch.step({"a": "x", "b": "x"})
# 两个会话各先占 1 个解码槽，剩余 1 槽闲置
```

## 6. 关键设计决策（语义对齐说明）

1. **解码优先于提示词处理**：解码槽在轮内先分配，已开始出词的会话每轮必走一个解码步，
   剩余槽才进入 prefill 分配，符合“已经开始出词的会话还要继续”的要求。
2. **prefill 完成与出词隔一轮**：prefill 轮内完成的会话仅把状态切到 `decoding`，
   解码遍历在状态切换之前已经结束，天然保证“从下一轮出词”。
3. **接纳跳过而非阻塞**：队首暂时放不下时放回队列原位置，继续尝试后续请求；
   只有“单条需求超过总容量”这种永远无法满足的请求才被拒绝。
4. **预留与释放的时机**：接纳即按最坏需求（提示词 + 最大输出）预留，所有终止路径
   （停止符 / 上限 / 取消）都只在轮末释放，因此一轮内的缓存账目不会中途变化，
   `cache_reserved_end` 即下一轮接纳的可用基线。
5. **取消在轮边界生效**：取消标记在轮首快照，被取消的活跃会话本轮跳过所有调度，
   与其他终止路径一起在轮末统一释放、统一上报。
6. **并发会话上限即 B**：题目约束“会话数上限不超过 B”，接纳时除缓存外再检查
   `len(active) < B`。
7. **确定性顺序**：等待队列严格 FIFO；`finished` 按入队序号排序输出，便于调用方对账。
8. **线程模型**：内部用 `RLock` 串行化所有公开方法；单轮调度是同步原子操作。

## 7. 验证状态（重要）

- `tests/test_scheduler.py` 共 18 个测试方法，全部使用**本地合成字符串 Token**，
  覆盖：prefill→decode 隔轮、解码槽优先、提示词跨轮分段、队首跳过与次序保留、
  超容量拒绝且不阻塞后续请求、等待/活跃两类取消、停止符/长度上限/停止符优先、
  轮末释放与下一轮复用、并发会话上限、入队序 prefill 分配、空轮、映射型 provider、
  参数校验与快照字段。
- **按本次任务约定，未在本环境执行任何代码、命令或测试**，因此这些测试**尚未运行**，
  本仓库不声明“测试通过”或“已验证”。后续在允许执行的环境中可运行：

  ```bash
  python -m unittest discover -s tests -v
  ```

  （`scheduler/` 与 `tests/` 位于仓库根目录，无需安装第三方包。）

## 8. 范围与边界

- 本组件只实现题面要求的**本地调度基础设施**：槽位/缓存记账、接纳次序、轮次推进与
  可观察结果；不包含模型推理本身、网络服务、持久化或分布式协调。
- Token 以不透明字符串表示，调度器不解释 Token 内容，停止符判定基于相等比较。
- 不包含任何漏洞利用、攻击载荷、目标探测或复现脚本。
