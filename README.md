# 去标识日期映射器（de-identified date mapper）

有亲缘关系的研究对象各自整体平移事件日期后，可能出现“孩子的采样早于出生”
之类的时间矛盾。本工具在**每人所有事件使用同一个整数天偏移**的前提下，求解
一份满足全部发布间隔约束的日期映射，并按题面规定的两级目标择优：

1. 先最小化 `max_p |x_p - proposed_p|`（任何一人的最大偏移改动）；
2. 在所有一级最优解中，按人号顺序取**字典序最小**的偏移向量。

无解时不返回强行映射，而是返回一组可独立复核的矛盾不等式（差分约束负环）。
同一定稿计划可序列化后用于分批导出，保证相同事件无论分多少批、跨多少进程，
发布日期始终相同。

仅依赖 Python 3.11 标准库，无第三方依赖。

## 公开 API

### 数据结构

| 名称 | 说明 |
| --- | --- |
| `GapConstraint(a_event, b_event, min_gap_days=None, max_gap_days=None)` | 事件对间隔约束：`min ≤ 发布日(b) − 发布日(a) ≤ max`（天，闭区间）。事件用 `(人号, 事件号)` 引用；任一侧为 `None` 表示不限制。 |
| `InfeasibleError` | 无解异常，属性 `certificate` 为矛盾证书。 |
| `ContradictionCertificate` | `.inequalities`（`Inequality` 列表）、`.contradiction_sum`、`.verify()`（独立复核）、`.to_text()`（可读文本）。 |
| `Inequality` | 形如 `x[left] − x[right] ≤ rhs`；`left/right` 为 `None` 表示常量 0（来自偏移上下界）。 |
| `FinalizedPlan` | 定稿计划只读视图（偏移、最大改动、事件序数）。 |

### `DateMapper`

```python
from datemapper import DateMapper, GapConstraint, InfeasibleError

mapper = DateMapper(
    event_dates={1: {"birth": date(1990, 3, 1)}, 2: {"sample": date(1990, 2, 25)}},
    offset_bounds={1: (-30, 30), 2: (-30, 30)},       # 每人允许的整数天偏移闭区间
    proposed_offsets={1: 0, 2: 0},                    # 原先拟用的偏移
    gap_constraints=[
        GapConstraint((1, "birth"), (2, "sample"), min_gap_days=0),
    ],
)
try:
    mapper.finalize()
except InfeasibleError as exc:
    print(exc.certificate.to_text())   # 逐条不等式相加坍缩为 0 <= 负数
    assert exc.certificate.verify()    # 独立复核：变量全部抵消且右端和为负
```

| 成员 | 行为 |
| --- | --- |
| `finalize()` | 求解并定稿，幂等；无解抛 `InfeasibleError`，此时映射器保持未定稿状态。 |
| `map_event(person, event_id)` | 返回发布日期；输入是 `datetime.date` 就返回 `date`，输入是整数序数就返回整数。 |
| `map_event_ordinal(person, event_id)` | 始终返回整数序数日。 |
| `map_all()` | 全部事件的发布日期。 |
| `offsets` / `max_change` | 定稿偏移向量与第一级目标值。 |
| `to_plan_dict()` / `DateMapper.from_plan_dict(data)` | 定稿计划的 JSON 兼容导出与装载；装载后无需重新求解即可继续分批导出。 |

日期支持两种输入：`datetime.date`（按 `toordinal()` 转序数日计算）或直接给
整数序数日。人号、事件号可以是任意可哈希类型（整数、字符串等）；字典序按
人号的自然排序，与输入字典的插入顺序无关。

### 低层求解 API

`datemapper.solver.solve_offsets(...)` 直接在差分约束边上求解，
`Edge(u, v, weight, origin=None)` 表示 `x_v − x_u ≤ weight`，节点 0 为
恒等于 0 的锚点。常规使用不需要直接调用。

## 算法

1. 每人的偏移量是一个整数变量。偏移上下界与每条间隔约束统一改写为差分约束
   `x_v − x_u ≤ w`，对应图上 u→v、权 w 的边；锚点节点表示常量 0。
2. Bellman-Ford 判可行性：图含负权环当且仅当系统无解。负环上的不等式逐条
   相加，所有变量项抵消，得到 `0 ≤ 负数`，即矛盾证书。
3. 第一级目标对 `D = max_p |x_p − proposed_p|` 二分（可行性随 D 单调）。
4. 第二级按人号顺序逐个固定变量：用正/反图最短路求出当前变量的可行整数
   区间 `[−dist(i→0), dist(0→i)]`，再从区间最小端起找第一个可行整数固定，
   因此得到字典序最小向量。
5. 复杂度：人数 n、约束边 m 时，每次可行性检查为 O(n·m)；整体为
   O((log D_range + n) · n · m)。测试面向小规模合成数据。

## 运行方法

```bash
# 题面指定的入口（在工作区根目录执行）
python3 -m unittest discover -s tests -v

# 隔离模式（忽略环境变量与用户站点包）同样通过
python3 -I -m unittest discover -s tests
```

## 验证范围与实测证据

全部断言均来自本地合成数据，无网络、无第三方依赖。测试文件：

- `tests/test_narrative.py`（11 项）：孩子采样早于出生的修复、两级目标与
  字典序 tie-break（含人号顺序与字典插入顺序无关）、最小/最大间隔窗口、
  同一人事件整体平移、真实日历日期往返、定稿幂等。
- `tests/test_infeasible_and_bounds.py`（15 项）：单人自环矛盾与跨人矛盾的
  证书独立复核（不调用被测 `verify()`，按定义重新求和）、空输入、拟用偏移
  落在范围外、单点偏移区间、零宽间隔、不可行边界，以及 9 项输入校验。
- `tests/test_batch_export.py`（6 项）：同进程重复导出稳定、`to_plan_dict`
  JSON 往返后偏移与发布日期逐事件一致、跨“批次”查询一致、date/int 类型
  保持、未知事件报错、计划版本号校验。
- `tests/test_bruteforce_property.py`（3 项）：60 个固定种子随机实例与
  **独立暴力枚举**逐项对照（可行/不可行判定、最小 D、字典序向量、全部
  约束满足），并断言可行与不可行两类实例都被实际覆盖；另含交错约束固定
  实例与贴边界唯一可行解实例。

最近一次本地执行：`Ran 39 tests ... OK`（普通模式与 `python3 -I` 隔离模式
均通过）。此外 `scratch/stress.py` 用 5 个种子、200 个更大随机实例再次对照
暴力枚举，结果为 72 个可行、128 个不可行，全部一致且 128 份证书全部通过
独立复核；`scratch/` 为开发辅助脚本，不属于被测包。

## 边界与限制

- 面向小规模数据（窄偏移范围、少量人与事件）；未做大规模工程优化。
- 偏移量、间隔天数必须是真正的整数（拒绝 `bool`）；日期仅支持
  `datetime.date` 或整数序数。
- 矛盾证书给出的是 Bellman-Ford 找到的一个负环，保证可独立验证，但不保证
  是所有负环中最短的。
- 装载的计划字典是被信任的本机数据；不从任何外部来源拉取。
