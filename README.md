# 双时态关系表：业务有效期 + 录入提交号

一张支持**双时态**建模的关系表。每个主键可以多次补录任意**半开业务区间**，
也可以在区间内撤销事实；所有录入按严格递增的**提交号**只追加、不可变。
查询时既可以按两个时间点取单个值，也可以在指定提交视图中取出一段业务
时间内的连续片段（含空档，并合并相邻等值段）。

仅使用 Python 3.11.6 标准库（`dataclasses`、`typing`），无第三方依赖。

## 时间模型与语义

| 维度 | 含义 | 形态 |
| --- | --- | --- |
| 业务有效期（valid time） | 事实在业务上生效的时间 | 半开区间 `[valid_begin, valid_end)`，左含右不含 |
| 录入提交号（commit / transaction time） | 录入进入系统的先后顺序 | 从 1 开始严格递增的整数，只追加 |

核心规则：

1. **同一主键可多次补录任意半开区间**，新录入不修改旧数据，只在其区间内
   成为“最近一笔”。
2. **区间内可撤销事实**：`retract` 写入一条墓碑记录。查询某业务时间点时，
   只看提交号 `<= as_of` 的录入，并取其中**提交号最大的一笔**；该笔是
   墓碑则该点为空档。
3. **左右未覆盖部分沿用原值**：补录或撤销都只作用于各自区间，区间之外
   最近的旧录入继续有效。
4. **视图不可变（时间旅行）**：给定 `as_of` 的结果只取决于当时已提交的
   录入，之后再补录/撤销不会改变历史视图。
5. 时间点类型只需支持比较与排序即可，整数、`datetime.date`、`datetime`
   等均适用（表本身不解释物理时钟）。

## 公开 API

均位于 `bitemporal` 包（实现见 `bitemporal/table.py`）：

```python
from bitemporal import BitemporalTable, Record, Segment
```

### `BitemporalTable()`

构造一张空表。

### 写入（返回新分配的提交号）

- `commit(key, valid_begin, valid_end, value) -> int`
  在新提交中录入事实：`key` 在半开区间 `[valid_begin, valid_end)` 上的值为
  `value`。`value` 不允许为 `None`（`None` 保留给空档语义）。
- `retract(key, valid_begin, valid_end) -> int`
  在新提交中撤销该主键在给定半开区间内的事实（逻辑删除/墓碑）。
- 属性 `head -> int`：当前最新提交号，空表为 `0`。

两者在 `valid_begin >= valid_end` 时抛出 `ValueError`。

### 查询

- `value_at(key, valid_time, as_of=None) -> Any`
  **按两个时间点查值**：在提交视图 `as_of`（`None` 表示最新视图）中，
  返回 `valid_time` 处最近一笔录入的值；无事实或事实已撤销时返回 `None`。
  命中判定为 `valid_begin <= valid_time < valid_end`。
- `slice(key, valid_begin, valid_end, as_of=None) -> list[Segment]`
  在提交视图 `as_of` 中，把业务查询区间 `[valid_begin, valid_end)` 切成
  **首尾相接的连续片段**：
  - 无事实/已撤销的位置返回空档片段（`value=None`、`gap=True`）；
  - 相邻且值相等的片段（含相邻空档）自动合并为一段；
  - 区间内完全无事实时返回单个空档片段。
- `records(key=None) -> tuple[Record, ...]`
  只读审计轨迹，按提交号顺序返回；墓碑记录的 `retracted=True`。

### 数据对象（均为不可变 `frozen=True` dataclass）

- `Record(key, valid_begin, valid_end, value, commit, retracted)`：一条录入。
- `Segment(begin, end, value, gap)`：业务时间轴上的一段；`gap=True` 时空档。

### 参数边界

- `as_of` 必须是非负整数或 `None`；负数抛 `ValueError`，非整数（含
  `bool`）抛 `TypeError`；大于 `head` 的提交号按 `head` 截断（未来视图）。
- 所有业务区间均要求严格半开（起点严格小于终点）。

## 题面叙事的建模方式

以业务周 2026-11-16（周一）至 2026-11-23（下周一）、半开
`[周一, 下周一)` 为例：

1. 周一：`commit("k", 周一, 下周一, "A")` → 提交号 1，覆盖整周；
2. 周五：`commit("k", 周二, 周四, "B")` → 提交号 2，更正区间
   `[周二, 周四)`；
3. 周五：`retract("k", 周二, 周三)` → 提交号 3，撤销更正区间的前一半
   `[周二, 周三)`。

于是：

- 最新视图下整周切片为 `A | 空档 | B | A` 四段；
- “周三的录入视图”对应 `as_of=1`（周三时只有周一的录入存在），整周
  仍是连续的旧值 `A`——这一历史视图在后续提交后保持不变；
- `as_of=2` 的视图可看到完整更正 `A | B | A`，用于区分“补录后、撤销前”
  的状态。

该叙事由 `tests/test_bitemporal.py` 中的 `NarrativeWeekScenarioTests`
以真实 `datetime.date` 数据驱动断言（非预置输出）。

## 复现步骤

要求：Python 3.11.6（本机 `python3 --version` 即为 3.11.6），无需安装
任何依赖，在工作区根目录执行：

```sh
# 方式一：默认发现（已验证可从根目录递归发现 tests/test_*.py）
python3 -m unittest discover

# 方式二：显式指定测试目录
python3 -m unittest discover -s tests

# 方式三：隔离模式（忽略环境变量与用户 site-packages）
python3 -I -m unittest discover

# 详细输出
python3 -m unittest discover -s tests -v
```

## 验证范围

`tests/test_bitemporal.py` 共 29 个测试，按场景分类：

- **正常场景**：提交号从 1 递增；多层区间更正时“最近一笔胜出”；区间左右
  未覆盖部分沿用原值；`slice` 输出连续、首尾相接的片段并合并相邻等值段。
- **边界场景**：半开区间端点命中规则（含起点、不含终点）；空表/未知主键
  返回 `None` 与单个空档片段；`as_of=0`、未来提交号、查询窗口裁剪；
  空区间/反转区间、`None` 值、非法 `as_of` 的异常校验。
- **撤销场景**：撤销产生空档且只作用于自身区间；撤销后重新录入可覆盖
  墓碑；对空历史撤销；相邻空档合并。
- **交错场景**：多主键交错提交相互隔离；三层嵌套更正后部分撤销；提交后
  旧视图快照保持稳定；只追加审计轨迹。
- **题面叙事**：`NarrativeWeekScenarioTests` 6 个测试，用真实日期验证
  “周一整周值 → 周五更正 → 周五撤销一半 → 切回周三视图仍是整段旧值”。

未覆盖/刻意不做的范围：并发写入与锁、物理时钟/提交墙钟时间、持久化与
序列化、跨主键查询、非半开（闭区间/开区间）语法——均不在题目要求内。
