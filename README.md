# 再认证裁决模块

一个零第三方依赖的 Python 3.11 模块，用于控制室单终端轮流处理多个身份可见现场操作时，裁决哪些操作可以一起批准。

模块接收：

- 已验证的在场证明（presence proof）；
- 设备接近证明（device proximity proof）；
- 操作申请及其证明组合、最迟确认时间；
- 进入共享控制流程的定向接力图。

它会精确搜索可共同批准的最大操作集合，返回每个操作实际使用的证明、接力路径、授权消耗，并诊断余下申请缺少或被占用的能力。

## 运行与验证

无需安装依赖，使用 Python 3.11.6：

```bash
python3 -m unittest discover -s tests -v
python3 examples/synthetic_demo.py
```

最近一次实测结果为 23 个 unittest 全部通过；合成数据示例会输出最大集合、证明组合、缺失能力和“失败—回滚—重试成功”的交错过程。

## 公开 API

最常用入口：

```python
from readjudicator import (
    AdjudicationSession,
    OperationRequest,
    Proof,
    RelayEdge,
    RelayGraph,
    Requirement,
    adjudicate,
    adjudicate_payload,
)
```

### `adjudicate(...)`

```python
result = adjudicate(
    operations,
    proofs=all_proofs,                 # Proof/dict 的列表，也接受 {id: dict}
    presence_proofs=presence,          # 单独传入在场证明
    device_proofs=device_proximity,    # 单独传入设备接近证明
    graph=relay_graph,                 # RelayGraph、dict 或边列表；也可为 None
    now=100.0,                         # 共享数值时钟
    terminal_id="control-1",
)
```

返回 `AdjudicationResult`，常用字段/属性：

- `maximum_size`：可一起批准的最大操作数；
- `approved`：`ApprovedOperation` 元组，包含确认时间和分配明细；
- `approved_ids`：按终端调度顺序排列的已批准 ID；
- `proof_combinations`：`{operation_id: {requirement: proof_id 或 [proof_id, ...]}}`；
- `rejected`：`RejectedOperation` 元组；
- `missing_capabilities`：`{operation_id: (MissingCapability, ...)}`；
- `to_dict()` / `to_json()`：JSON 友好的结果。

当多个集合大小相同，默认选择请求 ID 字典序最小的集合，保证结果可复现。

### `adjudicate_payload(payload)`

接受 JSON 风格字典，键可以是：

```python
{
  "now": 100,
  "terminal_id": "control-room-1",
  "presence_proofs": [...],
  "device_proofs": [...],
  "proofs": [...],
  "graph": {"edges": [...]},
  "operations": [...],       # 或 "requests" / "pending"
}
```

### `AdjudicationSession`

用于模拟同一个终端上的实时交错处理：

```python
session = AdjudicationSession(
    proofs,
    graph=graph,
    now=100,
)

decision = session.review(operation)   # ApprovedOperation 或 RejectedOperation
session.now                            # 若 operation.duration > 0，会向后推进
session.ledger.uses_left               # 剩余证明次数
session.history                        # 所有处理决定
```

成功决定才提交资源消耗；失败分配会恢复到该操作开始前的账本快照。此前已经成功确认的证明不会被回滚。

## 输入模型

### `Proof`

```python
Proof(
    proof_id="p1",
    kind="presence",                   # 常用："presence" / "device"，也允许自定义类型
    identity="alice",
    capabilities={"operator", "dual-control"},
    uses=1,                            # 1 = 一次性证明；None = 可重复使用
    authorizations={"presence_authorization": 1},
    node="floor",                      # 证明进入接力图的位置
    valid_from=0,
    expires_at=200,                    # 边界包含 expires_at
    attributes={"near": "terminal-1"},
)
```

便捷构造器：

```python
Proof.presence("p1", identity="alice", roles=["operator"], node="floor")
Proof.device_proximity(
    "d1", device_id="tab-1", near="terminal-1", node="gateway"
)
```

字典输入可用 `"one_time": true/false`；`false` 映射为 `uses=None`。

### `Requirement`

```python
Requirement(
    capability="dual-control",
    count=1,
    proof_kind="presence",             # 可选类型约束
    identity_matches_request=True,     # 证明身份必须等于 operation.identity
    allowed_identities={"supervisor-7"},
    identity_must_differ_from_request=False,
    attributes={"near": "terminal-1"}, # 精确属性匹配
)
```

字符串 `"dual-control"` 也会被解析为 `Requirement("dual-control")`。

### `OperationRequest`

```python
OperationRequest(
    request_id="O1",
    requirements=[...],
    deadline=200,                      # 最迟确认时间，边界包含 deadline
    identity="alice",
    entry_node="control",              # 证明必须接力到达的操作节点
    duration=5,                        # 可选终端占用/确认耗时，默认 0
    priority=0,
)
```

时间使用同一数值时钟（秒或任意可比较数值）。若 `duration=0`，所有确认时间均为 `now`。若提供正的 `duration`，模块按截止时间最早优先（EDF）调度；对于共同释放的单终端任务，这是标准的截止时间可行性顺序。若证明带生效/过期窗口，EDF 分配失败时会补充检查具体终端顺序，避免“先处理紧急任务导致早过期证明失效”的顺序边界。

## 接力图语义

`RelayGraph` 是有向多重图。证明从 `Proof.node` 出发，必须找到一条到达 `OperationRequest.entry_node` 的简单路径。

```python
graph = RelayGraph([
    RelayEdge.device_only("gateway", "control", "dev-edge"),
    RelayEdge.presence_handoff("floor", "control", "pres-edge"),
    RelayEdge("shared", "control", "open-edge"),
])
```

- `RelayEdge.device_only(...)`：只允许 `kind="device"` 的证明通过，不消耗在场授权。
- `RelayEdge.presence_handoff(..., amount=1)`：默认只允许在场证明通过，并从该证明绑定的 `presence_authorization` 余额中消耗一次授权。
- 普通 `RelayEdge(..., allowed_kinds=None, authorization_costs={...})`：可表达开放边或自定义资源消耗。
- `capacity=N` 可额外限制某条边在整个批准集合中的使用次数，默认不限制。
- 同一节点间为空路径。

路径枚举按边 ID 稳定排序；可传 `max_routes` 限制每个起点/终点/类型的路径数，默认不限制。

## 最大集合与原子分配

1. 按集合大小从全部操作向下枚举。
2. 对每个集合先检查单终端截止时间调度。
3. 将集合中所有操作的全部要求槽位放入同一个回溯约束求解，而不是贪心批准一个操作后不再重选其证明。
4. 每一步用账本快照预占：
   - 证明剩余使用次数；
   - 证明绑定的授权余额；
   - 接力边容量。
5. 若后续槽位失败，恢复该候选快照并尝试其他证明/路径。
6. 整个集合成功后才形成结果；每个大小的第一个可行集合即最大且 ID 字典序最小的集合。

因此，一个失败的部分分配不会消耗一次性证明；平行操作对同一证明的竞争由全局容量约束裁决。

## 缺失能力诊断

未入选操作会在最大集合批准后的剩余账本上做克隆诊断，不会污染正式结果。常见 `reason_code`：

- `capability_absent`：没有任何证明提供该能力；
- `capability_shortage`：全局可分配数量本身不足；
- `exhausted_by_approval`：全局有候选，但已被选中的批准集合占用；
- `shortage_and_exhaustion`：原本不足，且部分还被批准集合消耗；
- `joint_proof_contention`：每个要求单独都有候选，但相同一次性证明无法共同满足整个组合；
- `identity_mismatch` / `identity_not_allowed` / `request_identity_unknown`；
- `proof_kind_mismatch` / `attribute_mismatch`；
- `no_relay_route` / `insufficient_authorization`；
- `proof_expired` / `proof_not_yet_valid`；
- `deadline_exceeded` / `deadline_and_capabilities_missing`。

`MissingCapability.consumed_by` 记录占用候选证明的已批准操作 ID。`available_*` 表示最多可满足多少个相同要求槽位，达到 `required` 后按 `required` 封顶，足以区分“不足”和“仍够”。

## 已验证范围

`tests/` 全部使用本地合成数据，不访问网络、真实目标或凭据。覆盖：

- 正常在场证明 + 设备接近证明组合；
- JSON 字典输入与 JSON 序列化；
- 最大集合而非贪心集合；
- 跨操作证明选择的联合回溯；
- 两个平行操作竞争同一一次性证明；
- 一个证明同时提供两种能力时的联合竞争诊断；
- 绝对缺失能力、数量不足和批准后耗尽；
- 身份绑定、身份不匹配、指定监督者身份；
- 重复要求返回证明 ID 列表；
- 截止时间和证明过期时间的包含边界；
- `duration` 完成时刻边界与 EDF 调度；
- 早过期证明所需的非 EDF 终端顺序；
- 设备专用边拒绝在场证明；
- 在场接力边消耗一次授权；
- 平行图分支竞争共享证明；
- 失败双占用后的快照回滚；
- 成功—失败—重试成功的交错序列；
- 已确认证明在后续失败分配后仍保持消耗；
- 空申请批次。

## 限制

- 模块只做裁决，不验证证明的数字签名、证书链或真实设备遥测；调用方必须先完成“已验证”前提。
- 精确集合/分配搜索是指数级问题，带时间窗口时还会检查具体顺序；它适合控制室常见的小型待批批次。很大批次应加入更高级的求解器或分区策略。
- 时间为调用方提供的共享数值时钟；如需 `datetime`，请先转换为统一时间戳或单调时钟数值。
- 本仓库只实现题面要求的防御性判断、资源裁决和审计输出，不包含任何利用、入侵、攻击或规避载荷。
