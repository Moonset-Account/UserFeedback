# 事件顺序裁决模块

在两台设备时钟不可信时，本模块只依据：

- 各来源内部已经确认的事件次序；
- 能指向具体“发起事件”和“接收事件”的观测；
- 证据编号与来源；
- 证据撤回/更正记录；

构造有向证据图，回答两个事件的先后关系、展示证据链，并分析结论是否依赖某个独立来源。

仅使用 Python 3.11 标准库，不需要安装依赖，不访问网络，也不读取真实目标材料。所有演示和测试数据均为本地合成数据。

## 运行方法

在题目工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

运行端到端合成示例：

```bash
python3 examples/synthetic_demo.py
```

## 公开 API

主要类和函数由 `event_order` 包导出；根目录的 `adjudicator.py` 还提供
`EventOrderResolver`、`resolve_order` 等别名入口。

```python
from event_order import (
    OrderAdjudicator,          # 也可用 EventOrderAdjudicator / Adjudicator
    OrderVerdict,
    EvidenceChain,
    ChainStep,
    Conflict,
    Retraction,
    SourceRemovalTest,
    build_adjudicator,
    adjudicate_order,
)
```

### 1. 建立证据

```python
from event_order import OrderAdjudicator

adj = OrderAdjudicator()

# 来源内部次序；一个序列证据可以支撑多段相邻次序。
adj.add_sequence(
    "phone-log",
    ["boot", "unlock", "send"],
    evidence_id="E-PHONE",
)

# 观测明确指向发起事件和接收事件：发起事件 -> 接收事件。
adj.add_observation(
    "OBS-1",
    source="packet-capture",
    initiation_event="send",
    receipt_event="server_recv",
)

# 也可以直接加入一条先后证据。
adj.add_order_evidence(
    "E-SERVER",
    "server-log",
    before_event="server_recv",
    after_event="db_write",
)
```

常用字段别名也可用于字典输入，例如 `send_event/sent_event/origin_event`、
`receive_event/received_event/receipt_event`、`before/earlier_event`、
`after/later_event`。

### 2. 查询与证据链

```python
verdict = adj.query("boot", "db_write")

print(verdict.outcome)          # first_before_second
print(verdict.chain.render())   # 可读证据链
print(verdict.evidence_ids)     # ['E-PHONE', 'OBS-1', 'E-SERVER']
print(verdict.source_ids)       # ['phone-log', 'packet-capture', 'server-log']
```

`outcome` 的取值：

| 值 | 含义 |
| --- | --- |
| `first_before_second` | 存在从第一个事件到第二个事件的有效证据链 |
| `second_before_first` | 存在反向证据链 |
| `conflicting_evidence` | 两个方向都有有效证据链；同时查看 `conflicts` 与两条方向链 |
| `unknown` | 没有可连接二者的有效证据链；不能据此判断同时发生 |
| `same_event` | 两个参数是同一个事件 ID，空链且不依赖来源 |

`verdict.to_dict()` 会生成 JSON 友好的完整结果，包括链、冲突和来源撤掉测试。

### 3. 独立来源移除测试

查询会临时排除每一个已登记来源，并重新搜索证据链。该分析是只读的，不会删除或修改原证据。

```python
verdict = adj.query("send", "recv")

verdict.critical_sources
# 例如 ['unique-bridge-source']；空列表表示撤掉任意一个来源后仍有证据。

test = verdict.removal_tests["source-id"]
test.still_has_evidence                 # 撤掉该来源后是否仍有任一方向的链
test.remaining_chain                    # 剩余链
test.outcome_without_source             # 撤掉来源后的判定
test.critical_for_adjudicated_conclusion
```

判定规则：如果原始结论是确定的某一方向，而排除某来源后该方向不再有任何链，则该来源是关键依赖，进入 `critical_sources`。

当结果为 `conflicting_evidence` 时，不强行选出一个“胜诉”方向；可分别查看：

- `verdict.forward_chain` / `verdict.forward_critical_sources`
- `verdict.reverse_chain` / `verdict.reverse_critical_sources`
- 每个来源的 `SourceRemovalTest.forward_chain` 与 `reverse_chain`

这里的移除测试是“一次撤掉一个来源”，不模拟同时撤掉多个来源的组合情况。

### 4. 同范围矛盾

两条未撤回证据若在同一个 `scope` 内对同一对事件给出相反方向，会形成 `Conflict`：

```python
conflicts = adj.conflicts()
for conflict in conflicts:
    print(conflict.conflict_id)
    print(conflict.scope)
    print(conflict.evidence_ids)  # 涉及的两个证据编号
    print(conflict.source_ids)
```

查询结果中的 `verdict.conflicts` 只包含与该查询事件对或其证据链相关的冲突；
`adj.conflicts(scope="...")` 列出全部或指定范围的冲突。

矛盾不会导致整个模块不可用：冲突只作为独立对象列出，其他独立来源、其他事件对仍可正常查询。模块不会在没有可信度规则时擅自在两条有效矛盾证据中选择一条。

不同 `scope` 中的相反记录不会被记为“同范围冲突”。显式指定 `scope="name"` 时，只在该范围中组链和检测冲突；默认查询包含所有范围，并要求一条链处于同一范围，避免把互不相干的范围拼接成结论。

### 5. 撤回与更正

```python
adj.retract_evidence(
    "OBS-1",
    retraction_id="R-2026-001",
    reason="观测关联到了错误的请求",
    recorded_by="investigator",
)

updated = adj.query("send", "recv")
```

撤回后重新查询时，图和冲突均只使用未撤回的有效证据。撤回记录保留在
`adj.retractions` 中。默认情况下，撤回一个尚未见过的证据编号会先记录为
`orphan_retractions`，若该证据之后加入，则它一加入就是已撤回状态；构造
`OrderAdjudicator(strict_unknown_retractions=True)` 可改为抛出
`EvidenceNotFoundError`。

### 6. 字典输入

```python
case = {
    "sources": ["phone", "server", "capture"],
    "sequences": [
        {"id": "E1", "source": "phone", "events": ["boot", "send"]},
        {"evidence_id": "E2", "source": "server", "event_order": ["recv", "done"]},
    ],
    "observations": [
        {
            "id": "O1",
            "source": "capture",
            "initiation_event": "send",
            "receipt_event": "recv",
        }
    ],
    "retractions": [
        {"retraction_id": "R1", "target_evidence_id": "O1", "reason": "correction"}
    ],
}

adj = OrderAdjudicator.from_dict(case)
verdict_dict = adjudicate_order("boot", "done", case)
```

`sources` 也支持映射形式，例如 `{"phone": ["boot", "send"]}`；这种简写会生成
`AUTO-SEQ-<source>` 证据编号。除 `sequences` / `observations` 外，也可把带
`type: "sequence" | "observation" | "order"` 的记录放入 `evidence`。

## 验证范围

当前包含 23 个 `unittest` 用例，分为：

- `tests/test_chains_and_sources.py`
  - 来源内部次序生成证据链；
  - 观测连接具体发起/接收事件；
  - 多来源事件交错并通过观测跨来源组链；
  - 两个独立来源冗余时，撤掉任一来源仍有证据；
  - 唯一桥接来源被正确列为关键依赖；
  - 未知事件、无路径、同一事件等边界。
- `tests/test_conflicts_retractions.py`
  - 同范围相反证据生成冲突并给出双方编号、来源和方向；
  - 冲突事件对之外的独立来源仍可查询；
  - 撤回错误观测后用剩余证据重新回答；
  - 动态“加入—矛盾—撤回—再加入”交错过程；
  - 不同范围不判为同范围冲突；
  - 传递环不会使搜索死循环，并保留两个方向的链和关键来源。
- `tests/test_ingestion_and_boundaries.py`
  - 字典和函数式 API；
  - 多种字段别名；
  - 字符串或字典形式的撤回记录；
  - 撤回记录先于证据到达；
  - 未知撤回的宽松/严格模式；
  - 重复事件、自排序、缺字段、冲突性复用证据编号等非法输入；
  - 空序列、单事件序列；
  - 公开方法别名和根目录 `adjudicator.py` 兼容入口；
  - 来源移除分析不修改原案例。

最近一次实际执行：

```text
Ran 23 tests in 0.005s

OK
```

## 设计边界

- 不使用时钟时间戳推断先后，只使用明确的来源次序和发起/接收观测。
- `unknown` 只表示“证据不足”，不表示并发或同一时刻。
- 不内置来源可信度或少数服从多数规则；矛盾会保留并披露。
- 来源移除测试是单来源故障/不可用假设，不是多来源组合 cut 分析。
- 证据图采用确定性 BFS；同等长度的链按证据编号稳定选择，完整的来源移除替代链仍会在结果中给出。
