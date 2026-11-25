# 统一身份服务：注销完成判定器

这是一个仅使用 Python 标准库的防御性证据判定组件。它接收可能乱序到达的会话派生、机房移交、移交回执、关闭事件，以及各签发端的连续事件水位，判断某个祖先会话的撤销是否已经满足完成回执条件。

不包含任何网络访问、真实目标、凭据读取、依赖安装或攻击/利用逻辑。

## 公开 API

推荐从 `identity_logout` 包导入：

```python
from identity_logout import (
    LogoutDecider,
    decide_logout_completion,
    Event,
    Watermark,
    RevocationBarrier,
)
```

为了简短导入，根目录的 `logout_completion.py` 还提供别名：

```python
from logout_completion import (
    LogoutCompletionDecider,       # 即 LogoutDecider
    evaluate_logout_completion,    # 即 decide_logout_completion
)
```

### 一次性判定

```python
decision = decide_logout_completion(
    "device-1",
    events=events,
    watermarks={"dc-source": 4, "dc-target": 10},
    barrier={"dc-source": 4, "dc-target": 9},
)

if decision.complete:
    receipt = decision.receipt
else:
    print(decision.missing_endpoints)
    print(decision.reason_codes)
    print([item.to_dict() for item in decision.missing])
```

### 有状态、可乱序追加证据

```python
decider = LogoutDecider(barrier={"dc-source": 4, "dc-target": 9})
decider.ingest(close_event)       # 目标端关闭先到也会先缓冲
decider.ingest(derivation_event)  # 源端派生后补，自动按 session/因果编号关联
decider.update_watermarks({"dc-source": 4, "dc-target": 9})

decision = decider.evaluate("device-1")
if decision.complete:
    receipt = decider.issue_completion_receipt("device-1")
else:
    print(decision.missing_endpoints)
```

如果在证据不足时直接调用 `issue_completion_receipt(...)`，会抛出 `CompletionNotReady`，其 `decision` 属性保留完整缺失证据。

## 输入格式

事件可以是 `Event` 对象，也可以是普通 `dict`。规范字段如下：

| 字段 | 说明 |
| --- | --- |
| `event_id` / `id` | 事件唯一编号；整数会统一转成字符串比较 |
| `type` / `kind` | `derivation`、`transfer`、`transfer_receipt`/`ack`/`receipt`、`close` |
| `session_id` | 事件相关会话；派生事件中表示新产生的子会话 |
| `parent_session_id` | 派生事件中的父/祖先会话 |
| `issuer` | 签发端或事件产生端，例如 `dc-source`、`dc-target` |
| `sequence` / `seq` | 该签发端日志中的连续序号 |
| `predecessor_id` | 因果前驱事件编号；`0`、`"0"`、`"none"` 表示无前驱 |
| `predecessor_endpoint` | 当前驱事件应由另一个端补证时，可显式指定欠证端 |
| `transfer_id` | 移交的幂等标识；缺省时使用移交事件 ID |
| `from_endpoint` / `to_endpoint` | 移交源端和目标端 |
| `receipt_id` | 移交回执的自然幂等编号 |
| `acknowledges_transfer_id` | 允许一个显式携带签收语义的关闭事件同时作为回执 |

常见别名字段也可识别，例如 `child_id`、`parent_id`、`target_endpoint`、`source`、`ack_endpoint` 等。

水位输入支持：

```python
{"dc-a": 12}
[("dc-a", 12), Watermark("dc-b", 8)]
[{"endpoint": "dc-a", "sequence": 12}]
```

撤销屏障支持：

- `{"dc-a": 10, "dc-b": 20}`：每端独立屏障序号；
- `10`：所有相关端共同使用的最低屏障序号；
- `RevocationBarrier(...)`：显式模型。

水位满足条件为 `watermark[endpoint] >= required_sequence`，即已经到达或越过该屏障坐标。水位更新只单调增大，旧水位不会回退新水位。

## 判定规则

`LogoutDecision.complete is True` 必须同时满足：

1. **递归后代全部关闭**：沿所有 `derivation -> parent_session_id` 边找到祖先的全部后代；每个后代都必须有关闭事件。
2. **移交不得在途**：每个相关 `transfer` 都必须有目标端签收/回执。移交本身不会创建新的后代节点。
3. **因果证据完整**：相关事件引用的 `predecessor_id` 必须已经入库；缺失时返回 `missing_causal_predecessor`，并尽可能给出欠证端。对于尚未能连接到当前根、但其前驱由本屏障相关端负责的早期事件，默认还会以 `unlinked_causal_predecessor` 失败关闭，避免在“目标关闭先到、源端派生未补”且水位数字看似很高时误发回执；确认不需要此保护时可传 `conservative_unlinked_predecessors=False`。
4. **相关签发端越过屏障**：屏障中声明的端，以及子树事件/移交中出现的端，都必须有达到屏障坐标的连续水位。
5. **没有因果环或冲突证据**：因果父链成环、同一事件 ID 被改成冲突事实、同一会话被声明两个冲突父节点时，不发回执。

普通目标端 `close` **不会**自动被当作移交签收；题面明确规定“尚未签收算在途”。如果关闭事件本身包含 `acknowledges_transfer_id`，则该显式签收会被认可。

### 乱序与幂等

- 关闭先于派生到达时，会先按 `session_id` 保存；派生事件后到时自动挂到父会话下，早先关闭立即成为该后代的关闭证据。
- 回执先于移交发出事件到达时，会按 `transfer_id` 或因果前驱事件 ID 暂存/建占位状态；移交事件后到时自动合并。
- 相同 `event_id` 的重复投递按幂等重复处理。
- 不同投递 ID 但相同 `receipt_id`，或同一目标端对同一 `transfer_id` 的重复签收，只保留一份有效回执，不会多造会话或后代。
- 所有 4 个主事件的 `4!` 种入库顺序都有测试，最终判定一致。

### 无关会话隔离

判定只遍历目标祖先的派生后代图，只检查该子树和屏障坐标中点名的签发端。无关会话即使有：

- 大量未关闭后代；
- 未签收移交；
- 很低或缺失的水位；

也不会阻挡当前祖先的完成回执。若同一个相关签发端已经越过本撤销屏障，其屏障之后的无关积压同样不影响本次判定。

## 缺失证据报告

`LogoutDecision.missing` 中每项都是 `MissingEvidence`，常用 `kind`：

| reason code | 含义 | 常见 `endpoint` |
| --- | --- | --- |
| `open_descendant` | 后代尚未关闭 | 最后移交目标端或派生来源端 |
| `open_root_session` | 仅在 `require_root_closed=True` 时，根会话未关闭 | 根会话当前托管端 |
| `in_flight_transfer` | 移交已发出但目标端未签收 | `to_endpoint` |
| `missing_causal_predecessor` | 已在当前子树中的事件缺少因果前驱 | 前驱事件的负责端/事件签发端 |
| `unlinked_causal_predecessor` | 早期事件尚无法连到当前根，且缺少由屏障相关端负责的前驱 | 该前驱的负责端 |
| `watermark_behind_barrier` | 签发端水位未越过屏障 | 该签发端 |
| `barrier_position_missing` | 发现相关端，但屏障没有该端坐标且无默认值 | 该相关端 |
| `barrier_not_configured` | 未提供任何撤销屏障 | 无 |
| `causal_cycle` | 派生父链存在环 | 无 |

`decision.missing_endpoints` 按稳定顺序去重列出“尚欠哪个端的证据”。

题面要求的交错场景由 `tests/test_out_of_order.py::OutOfOrderAndInterleavingTests.test_target_close_first_source_derivation_later_names_debtor` 验证：目标端关闭先入库、源端派生后补；源端证据补齐后，如果目标端水位仍差一个序号，报告会精确给出 `("dc-target",)` 以及 `required/actual`。

## 运行方法

要求 Python 3.11.6（或兼容的 3.11.x），无需安装依赖。

在仓库根目录运行：

```bash
python3 -m unittest discover -s tests -v
```

运行本地合成场景演示：

```bash
python3 examples/synthetic_demo.py
```

## 验证范围

当前测试全部使用进程内合成数据，覆盖：

- 正常派生、移交、签收、关闭、水位越障并生成确定性回执；
- 多层/多分支后代必须全部关闭；
- 兄弟会话和大量无关积压不阻挡当前回执；
- 移交未签收时报告在途及目标端；
- 普通关闭不替代签收，显式 `acknowledges_transfer_id` 可以；
- 相同事件 ID、相同 `receipt_id`、同端重复回执均不多造后代；
- 回执先于移交、关闭先于派生、整数因果编号等乱序/边界；
- 即使水位数字已提前达到屏障，缺少可归属的源端派生前驱时仍失败关闭并报告源端；
- 4 个主事件所有排列顺序的最终一致性；
- 缺失因果前驱时报告欠证端，并在补证后恢复；
- 源端/目标端水位缺失或落后时的端点级报告；
- 水位单调更新、整数屏障和逐端屏障；
- 因果环、冲突父节点、冲突事件 ID 等异常证据；
- `to_dict()`/回执 JSON 可序列化。

最近一次实际执行：

```text
python3 -m unittest discover -s tests -v
Ran 30 tests ... OK
```

具体逐条用例名称和结果以实际命令输出为准。
