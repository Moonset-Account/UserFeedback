# 封包规划模块（packet_planner）

一个纯 Python 3.11 标准库实现的防御性流量填充规划器。它为同一公开类别下的多份保密资料选择**所有资料共同使用**的分段长度序列，使外部观察者只能看到相同的分段数量和每段长度，而不能直接从分段长度区分资料内容。

本模块只实现题面要求的规划与校验，不包含联网、凭据、依赖安装、利用生成或攻击逻辑。

## 公开 API

核心函数和数据类型可从包顶层导入：

```python
from packet_planner import (
    plan_packets,
    try_plan_packets,
    PacketPlanner,
    PaddingLimit,
    PacketPlan,
    MaterialPlan,
    SegmentPlan,
    PlanningError,
    InvalidInputError,
    InfeasiblePlanError,
    RecordDoesNotFitError,
    PaddingBudgetExceeded,
)
```

### `plan_packets(...)`

```python
plan_packets(
    record_lists,
    allowed_lengths,
    max_segments,
    padding_limit=None,
    *,
    max_padding_per_material=None,
    max_overhead_ratio=None,
    allow_empty_profile=True,
    raise_on_infeasible=True,
) -> PacketPlan | None
```

参数：

- `record_lists`：同一类别下的资料。
  - 推荐写法：`[[120, 300], [200, 220, 10]]`，每个内层列表是一份资料按原顺序排列的记录长度。
  - 也接受 `{"资料A": [...], "资料B": [...]}` 映射，以保留资料 ID。
  - 如果传入一层全为整数的扁平列表，例如 `[120, 300]`，会便捷地视为只有一份资料。
  - 空外层列表无效；若要表示“一份没有记录的资料”，使用 `[[]]`。
- `allowed_lengths`：通道允许的分段长度档位，正整数，例如 `[200, 500, 1000]`。重复值会去重。
- `max_segments`：共同序列允许的最大分段数，必须是正整数。
- `padding_limit`：填充开销限额：
  - `None`：不检查；
  - `int`：所有资料的填充字节总量上限；
  - `float`：总填充 / 全部记录字节数的总开销比例上限；
  - `PaddingLimit(max_total=..., max_per_material=..., max_overhead_ratio=..., max_padding_fraction=...)`；
  - 或对应键的字典，例如 `{"max_per_material": 100}`。
  - `max_overhead_ratio` 的分母是原始记录总字节；`max_padding_fraction` 的分母是填充后的总段长。
- `max_padding_per_material`、`max_overhead_ratio`：便于关键字方式补充限额。
- `allow_empty_profile`：为 `True`（默认）时，完全没有记录的资料可得到零段、零填充的空序列；为 `False` 时至少选择一个最便宜档位作为全填充段。
- `raise_on_infeasible`：为 `False` 时，找不到可行序列或超过填充限额会返回 `None`。参数格式错误 `InvalidInputError` 仍会抛出。

### 返回对象

`PacketPlan` 包含：

- `common_lengths: tuple[int, ...]`：所有资料共同的分段长度序列；
- `segment_count`：共同分段数；
- `materials: tuple[MaterialPlan, ...]`：每份资料的安排；
- `total_record_bytes`；
- `total_profile_bytes = 资料数 × sum(common_lengths)`；
- `total_padding`：所有资料所有段的填充总量；
- `padding_by_material`；
- `to_dict()`：可 JSON 序列化的字典表示。

每个 `MaterialPlan.segments` 中的 `SegmentPlan` 给出：

- `length`：该共同段长度；
- `record_indices` / `record_lengths`：按原顺序放入本段的记录；
- `padding`：本段填充字节数；
- `is_full_padding`：本段是否不含任何完整记录。

也可以使用有状态包装类：

```python
planner = PacketPlanner(
    allowed_lengths=[2, 5],
    max_segments=2,
    padding_limit=10,
)
plan = planner.plan([[5], [2, 4]])
```

## 规划语义

1. 记录严格保持输入顺序，连续地放入分段。
2. 一条记录不能跨段；若记录长度大于某一段档位，该记录不能放入此段。
3. 每个分段最终填充到所选档位长度，因此外部看到的是 `common_lengths` 中的档位。
4. 每份资料都有相同数量的分段和相同的逐段长度。
5. 较早结束的资料在后续共同段中使用全填充段。若下一条完整记录装不进当前较小档位，该段对当前资料也可以是全填充段，记录进入后面的共同档位。
6. 对固定共同序列 `L = (l_1, ..., l_k)`，资料 `d` 的填充量恒为：

   ```text
   padding_d = sum(L) - sum(record_lengths_d)
   ```

   因此总填充为：

   ```text
   total_padding = len(materials) * sum(L) - total_record_bytes
   ```

   最小化总填充等价于先找总长度 `sum(L)` 最小的可行共同序列。
7. 平局时采用确定性规则：更少分段，再按长度序列字典序更小者。
8. 即使某个长度组合的总容量足以覆盖总字节数，只要有序分组无法让每条完整记录落入某个档位，该组合仍会被拒绝，搜索会继续尝试其他序列。

## 算法与复杂度

实现使用精确状态向量动态规划，而不是只比较总字节数：

- 用前缀和与二分查找计算某份资料从第 `i` 条记录开始，在给定档位中最多能连续放入多少条记录。
- DP 状态是 `(每份资料已消费到的记录下标, ...)`。
- 每条边代表在共同序列后追加一个允许档位。
- 最多扩展 `max_segments` 层；相同深度、相同状态只保留费用最低且序列字典序最小的标签。
- 找到最优序列后，再为每份资料重建具体分段。

设第 `d` 份资料有 `n_d` 条记录，可达状态数为 `S`，档位数为 `q`，最大段数为 `K`，时间复杂度约为 `O(K * S * q * m)`（另有二分查找的对数因子，实现中已并入），空间复杂度约为 `O(S)`。题面说明记录列表为少量数据，适合该精确搜索。

## 使用示例

```python
from packet_planner import plan_packets

# 资料一只有一条长度 5 的记录；资料二依次有长度 2、4 的记录。
plan = plan_packets([[5], [2, 4]], allowed_lengths=[2, 5], max_segments=2)

print(plan.common_lengths)
# (2, 5)

for material in plan.materials:
    print(material.material_id, [
        (segment.length, segment.record_indices, segment.padding)
        for segment in material.segments
    ])

# 0 [(2, (), 2), (5, (0,), 0)]
# 1 [(2, (0,), 0), (5, (1,), 1)]

print(plan.total_padding)
# 3
```

`(2, 5)` 的第一段对资料 0 是全填充段，但对资料 1 装载长度 2 的记录；这正是需要检查逐记录、逐段匹配的交错情况。

另一个“总字节数凑得齐但单条记录装不下”的例子：

```python
plan = plan_packets([[600, 400]], [500, 600], max_segments=2)
print(plan.common_lengths)
# (600, 500)
```

`(500, 500)` 的总容量恰好为 1000，但长度 600 的记录无法完整放入任何 500 段，因此被拒绝。

## 运行测试

在题目工作区根目录运行：

```bash
python3 -m unittest discover -s tests -v
```

当前实测环境为 Python 3.11.6。测试只使用 Python 标准库。

## 验证范围

`tests/test_planner.py` 包含 17 个测试方法，覆盖：

- 基本顺序装箱、前缀记录保留和填充字节核算；
- 多份资料使用零填充共同序列；
- 总容量足够但单条完整记录装不下时拒绝错误组合并选择其他组合；
- 尾部全填充段；
- 中间全填充段及分段档位顺序导致的交错情况；
- 最小分段数与字典序平局规则；
- 映射型资料 ID；
- 零长度记录；
- 空资料和 `allow_empty_profile=False` 边界；
- 总字节、单资料、比例三类填充限额及超额异常；
- 单条记录超过所有档位；
- 最大段数边界；
- 非法输入；
- `PacketPlanner` 与 `try_plan_packets`；
- 计划对象不变量和 `to_dict()` 的 JSON 序列化；
- 一个固定随机种子的穷举对照测试：生成 160 个小型合成场景，用独立的递归枚举器枚举所有长度序列和所有切点，断言 DP 结果与穷举最优完全一致，并检查返回安排。

此外，开发复核时还临时运行过 3000 个额外固定种子的小型随机场景，与同一穷举 oracle 全部一致；该临时批量复核未作为长期测试文件保存。未运行覆盖率工具，因此不声明行覆盖率百分比。
