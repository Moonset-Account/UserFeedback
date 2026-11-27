# 合规查询：可增量订阅的关系除法算子

用 Python 3.11.6（仅标准库）实现题面要求的增量关系除法。

## 题面语义

给定：

* **设备全集**：若干型号（model）。
* **能力事实**：`(来源号 source_id, 型号, 能力, 版本区间 [lo, hi])`，
  来源号全局唯一，同一载荷（同型号 / 同能力 / 同区间）可以有多个不同来源号。
* **要求行**：`(要求号 requirement_id, 能力, 版本区间 [lo, hi])`，可动态增删。

选出结果集：一个型号入选，当且仅当 **每一条要求** 都满足下面条件——
存在 **同一条、同型号、同能力** 的事实，其版本区间 **完整覆盖** 要求的
整个版本区间（`fact.lo <= req.lo` 且 `req.hi <= fact.hi`）。

要点：

* **空要求**时，所有型号（包括没有任何事实的型号）都入选。
* 覆盖必须由 **单条事实** 完成，两条相邻或重叠的事实不能拼接起来覆盖；
  覆盖判定只关心区间包含，`[lo, hi]` 按闭区间比较（含端点）。
* **重复上报不增加覆盖**：同载荷多来源号是“多重集”，只用于引用计数；
  新增重复事实不产生任何通知，撤回其中一条也不会过早把型号移出结果集，
  只有最后一条支撑覆盖的事实被撤回、且没有别的事实能覆盖时，型号才离开。
* 订阅端只接收 **实际进出结果集** 的型号：已经在结果集里的型号不会再次
  `enter`，已经在结果集外的不会再次 `leave`，新要求被其它事实覆盖、
  或不影响任何要求的事实增删，均不产生事件。
* 可以为 **未入选型号** 查询一条仍未被覆盖的要求
  （`first_uncovered_requirement` 按要求插入顺序返回第一条；入选型号返回 `None`）。

## 公开 API

```python
from compliance_division import DivisionEngine, Fact, Requirement, SelectionChange, ENTER, LEAVE
```

### 数据类

| 类 | 字段 |
| --- | --- |
| `Requirement` | `requirement_id: str`, `capability: str`, `version_from: int`, `version_to: int` |
| `Fact` | `source_id: str`, `model: str`, `capability: str`, `version_from: int`, `version_to: int` |
| `SelectionChange` | `kind: str`（`ENTER`/`LEAVE`）, `model: str`, `cause: str` |

`Requirement` / `Fact` 构造时会校验 `version_from <= version_to`，否则抛 `ValueError`（版本可负数、可为单点区间）。

### `DivisionEngine`

```python
DivisionEngine(models, requirements=(), facts=())
```

* `models`：设备全集（可迭代，重复自动合并，顺序用于确定的通知顺序）。
* `requirements` / `facts`：初始行；在构造期应用，只影响初始 `current_result()`，不会回放事件。

查询：

| 方法 | 说明 |
| --- | --- |
| `current_result() -> frozenset[str]` | 当前结果集 |
| `is_selected(model) -> bool` | 单个型号是否入选 |
| `models -> tuple[str, ...]` | 设备全集（去重后顺序） |
| `requirements_in_order() -> tuple[Requirement, ...]` | 当前要求行（插入顺序） |
| `uncovered_requirements(model) -> tuple[Requirement, ...]` | 未入选型号仍未被覆盖的要求（插入顺序） |
| `first_uncovered_requirement(model) -> Requirement \| None` | 第一条仍未覆盖的要求；入选型号返回 `None` |
| `facts() -> tuple[Fact, ...]` | 现存事实（插入顺序） |
| `has_fact(source_id) -> bool` | 来源号是否存在 |
| `requirement_count` / `fact_count` / `listener_count` | 计数属性 |

变更：

| 方法 | 说明 |
| --- | --- |
| `add_requirement(Requirement)` 或 `add_requirement(id, capability, from, to)` | 增要求；返回 `Requirement` |
| `remove_requirement(requirement_id)` | 删要求；未知 id 抛 `KeyError` |
| `add_fact(Fact)` 或 `add_fact(source_id, model, capability, from, to)` | 加事实；来源号重复抛 `ValueError`，型号不在全集抛 `ValueError` |
| `remove_fact(source_id)` | 撤回一条事实；未知来源号抛 `KeyError` |

订阅：

```python
sub = engine.subscribe(listener)   # listener(change: SelectionChange) -> None
sub.close()                        # 或 with engine.subscribe(...) as sub: ...
```

* 每个实际进入 / 离开结果集的型号各触发一次。
* 回调内可以安全地再次调用引擎（再入）；这类变更在 **当前批次投递完之后**
  按顺序投递，不丢事件、不乱序。
* 若回调抛异常，本次批次中止并清空未投递事件，异常继续向上抛出；
  订阅端可用 `current_result()` 重新对齐（不重放旧事件）。

## 运行测试

```bash
cd <题目工作区>
python3 -m unittest discover -s tests -v
```

预期：`Ran 39 tests ... OK`。

## 验证范围（均为本地合成数据，无网络、无外部依赖）

`tests/test_division_engine.py`：

* 空要求 → 全选（含零事实型号）；全集去重。
* 单条事实必须完整覆盖：过窄左端、右端、两条相邻事实不能拼接；端点相等可覆盖；
  更宽事实可覆盖；负数版本；单点区间。
* 同一型号不同能力由不同事实分别覆盖；别的型号的事实无助益。
* 一条宽事实同时满足多条重叠要求；构造器 `requirements` 先于 `facts` 应用。
* 校验：区间倒置、重复要求号、重复来源号、未知来源 / 要求 / 型号。
* 通知：只报实际进出；加要求仅报真正离开的型号；删要求按顺序报进入；
  重复事实零通知；撤回一条重复事实不会过早撤走型号（另一同载荷事实或更宽事实仍在）；
  无关能力事实静默；冗余覆盖事实静默；已移出型号再删部分事实静默；
  同一 id 二次撤回报 `KeyError` 且不重复通知。
* 未入选型号查询：按插入顺序返回第一条未覆盖要求；入选返回 `None`。
* 订阅生命周期：`close()` 幂等、上下文管理器、多订阅者同时收到。
* 再入：回调内改要求，事件按“先当前批次、后新批次”顺序投递；回调抛异常后
  队列清空、新订阅者不会收到泄漏的旧事件。

`tests/test_reference_model.py`：

* 与一份独立、非增量的朴素除法实现做 **随机差分测试**：
  * 400 步混合增删要求 / 事实，逐步比对结果集、每型号 `is_selected`、
    以及每一步实际 `ENTER`/`LEAVE` 事件集合。
  * 300 步偏向 **同载荷重复上报** 的序列，专门压测引用计数与“撤回不早退”。

以上全部断言均在本地实际执行通过；未覆盖项：并发 / 多线程、持久化、
真实查询接入——题面未要求，也未做实现或实测。
