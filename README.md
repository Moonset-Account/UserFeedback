# 案情数据组件：设备线索归并（CaseBoard）

调查员收集到设备登记、临时别名和人工确认记录，本组件把指向同一设备的
线索合并，同时保存明确的互斥证据，并对冲突与撤回提供可复核的轨迹。

## 实际入口

- 实现：`caseboard.py`，核心类 `CaseBoard`（纯标准库，Python 3.11.6）。
- 测试：`tests/test_caseboard.py`（unittest，真实断言）。

运行方式（工作区根目录）：

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

## 组件行为

| 操作 | 语义 |
| --- | --- |
| `confirm_link(a, b, source=...)` | 积累已核实关联（强），参与分组归并；与互斥证据直接冲突时抛 `ConflictError`，不写入 |
| `add_exclusion(a, b, source=...)` | 保存明确的互斥证据；若两端已被强关联路径连通，分组标记 `PENDING_REVIEW` 并记录冲突链 |
| `suggest_alias(a, b, source=...)` | 弱别名，仅进入 `candidates()` 候选提示，绝不触发合并 |
| `retract_link(id, reason=...)` | 撤回一条已确认关联；只重建受影响分组，旧状态存入快照 |
| `groups()` / `group_of(x)` | 当前分组（成员、版本、状态、冲突） |
| `pending_reviews()` | 待人工复核的 (分组, 冲突) 及冲突链（实体路径 + 构成路径的确认关联 id） |
| `snapshots([group_id])` | 历史快照：旧成员、状态、重建原因、当时组内全部线索（含来源） |
| `provenance(x)` / `evidence(id)` | 每条线索的来源记录；撤回的线索保留撤回原因 |

## 验证范围（tests/test_caseboard.py，10 个用例）

- **可接受结果**
  - `TestConfirmedMerge`：确认关联积累并归并为同一分组；重复确认幂等；来源可追溯。
  - `TestWeakAlias`：弱别名只提示候选，不产生分组；与互斥证据指向同一对设备时仍只提示。
- **应被阻止/拒绝的结果**
  - `TestDirectConflictRejected`：与互斥证据直接冲突的确认关联（含反向）被拒绝，
    不写入、不留分组；互斥证据本身被明确保存。
  - `TestRetract::test_retract_invalid_targets_rejected`：撤回不存在的 id、
    弱别名、互斥证据或重复撤回均被拒绝，且不误伤既有数据。
- **边界 / 交错场景**
  - `TestTransitiveConflictPending`：先建链后补互斥、先存互斥后补链两种交错顺序，
    均在强关联路径合拢时标记待人工，并给出冲突链（实体路径与确认关联 id 序列）。
  - `TestRetract`：撤回已确认关联只重建受影响分组——无关分组保持原 id/版本/对象不变；
    旧快照（含待人工状态快照）与每条线索来源保留；冲突链被拆断后待人工标记消除。
