# 窗口物理规划库（Window Physical Planning）

纯 Python 标准库实现（Python 3.11.6，无第三方依赖）。给定一批窗口声明，库会：

1. 校验规格并推导窗口间依赖（窗口的分区/排序键可以引用**另一个窗口已生成的输出列**）；
2. 在最多 8 个窗口的前提下，用精确的子集 DP 找出**满足依赖且总排序费用最小**的执行序，
   并给出每一步的物理排序键与复用依据；
3. 重放计划、物化三种窗口函数，并把结果按**原行号**归位；
4. 用独立的参考实现重算每个窗口的逻辑值、独立重放并实测每一步的真实费用。

## 概念模型

- 每个窗口声明：分区键 `partition_by`、带方向（ASC/DESC）和空值位置
  （NULLS FIRST/LAST）的排序键 `order_by`、并列裁决策略与窗口函数。
- 并列裁决策略：
  - `TiePolicy.ROW_NUMBER`：以物理行号尾键打破并列，得到确定性全序；
  - `TiePolicy.GROUP`：排序键相等的行构成一个逻辑同值组。
- 支持的函数：`ROW_NUMBER`、`DENSE_RANK`、`CUMULATIVE_COUNT`
  （按同值组累计计数：当前组及其之前所有同值组在本分区内的行数）。
- 物理键由三类部件组成：
  - 分区部件（规范为 ASC NULLS LAST，**可任意排列**，只需同值连续）；
  - 排序部件（保留声明的方向与空值语义）；
  - 可选行号尾键（`__rowno__`，始终在最后，只能物理打破并列，
    **不能拆散逻辑同值组**）。

## 三种物理转移

| 转移 | 含义 | 费用 |
| --- | --- | --- |
| `REUSE` | 现有物理次序已满足分区前缀 + 排序后缀（GROUP 窗口忽略多余尾键，ROW_NUMBER 窗口必须带行号尾键） | 0 |
| `PARTIAL` | 分区列已是连续前缀，在每个前缀组内分别稳定排序，组位置不变 | 各分组费用之和 |
| `FULL` | 在当前排列上做全量稳定排序 | 整表费用 |

### 费用模型（给定小表，真实计费）

- 比较次数不是估算值：通过 `functools.cmp_to_key` 包装比较器，
  **实测 Timsort 每一次比较**。
- `FeeSchedule` 包含两张小表：
  - `length_fee`：按组长**精确**查表（单元素组不排序、不计费）；
  - `comparison_brackets`：按比较次数取**最小够用的上界分档**。
- 表中覆盖不到的组长或比较次数会抛出 `MissingFeeError`，绝不静默编造数字。

## 目录结构

```
window_planning/
  spec.py         # 窗口/排序键/策略/函数的不可变声明
  physical.py     # 物理键、比较器、实测稳定排序、复用谓词、候选键枚举
  cost.py         # 两张费用小表与定价
  validation.py   # 规格校验、行加载、依赖推导与环检测、拓扑序
  logical.py      # 与物理路径无关的逻辑值物化（规划与验证共用的规范计算）
  planner.py      # 子集 DP（状态含具体排列，保证增量排序费用精确）
  executor.py     # 计划重放、三种函数物化、按原行号归位
  verification.py # 独立逻辑值重算 + 独立费用重审计
  demo.py         # 本地合成数据演示入口
tests/            # 70 个 unittest 用例，可从工作区根目录发现
```

## 公开 API

```python
from window_planning import (
    # 规格
    WindowSpec, OrderKey, Direction, Nulls, TiePolicy, WindowFunction,
    ROWNO_COLUMN, MAX_WINDOWS,
    # 数据与费用
    load_rows, FeeSchedule, default_fee_schedule, MissingFeeError,
    # 物理层
    PhysKey, PhysKeyPart, KeyRole, INITIAL_KEY,
    candidate_keys, can_reuse, partial_sort_key, partition_prefix_match,
    measured_stable_sort, measured_partial_sort,
    # 规划 / 执行 / 验证
    plan_windows, WindowPlan, PlanStep, StepKind,
    execute_plan, ExecutionResult,
    verify, reference_values, reaudit_plan_fees, VerificationReport,
    # 依赖与错误
    validate_specs, topo_order, SpecError, DependencyError, VerificationError,
)
```

最小用法：

```python
from window_planning import *

rows = load_rows([{"dept": "A", "salary": 10}, {"dept": "A", "salary": 10}])
rn = WindowSpec("rn", ("dept",),
                (OrderKey("salary", Direction.ASC, Nulls.LAST),),
                TiePolicy.ROW_NUMBER, WindowFunction.ROW_NUMBER)
dr = WindowSpec("dr", ("dept",),
                (OrderKey("salary", Direction.ASC, Nulls.LAST),),
                TiePolicy.GROUP, WindowFunction.DENSE_RANK)

fee = default_fee_schedule()
plan = plan_windows(rows, [rn, dr], fee)      # 最小费用计划
result = execute_plan(rows, [rn, dr], plan)  # 物化并归位
report = verify(rows, [rn, dr], plan, result, fee)  # 独立验证
print(plan.describe())
print(report.describe())
```

## 复现步骤

要求：Python 3.11.6，仅标准库，无需安装任何依赖。

```bash
# 1) 运行全部测试（在工作区根目录，测试可被自动发现）
python3 -m unittest discover -v

# 2) 运行合成数据演示（含两个逻辑并列语义不同、却共用同一物理次序的窗口）
python3 -m window_planning.demo
```

演示场景覆盖：

- `row_num`（ROW_NUMBER，带行号尾键）与 `dense_rank_salary` /
  `cum_count_salary`（GROUP 策略）键完全相同但**逻辑并列语义不同**，
  后两者以 `REUSE` 零费用共用同一物理次序；
- `rank_by_row_num` 以生成列 `row_num` 为排序键，形成真实依赖边；
- `row_num_salary_desc`（DESC + NULLS FIRST）与 `row_num_by_bonus`
  无法复用现有次序，但分区前缀连续，走 `PARTIAL` 组内稳定排序。

## 验证范围（70 个测试，均实际执行）

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_spec_validation.py` | 行号注入、保留列、schema 一致性、策略/函数匹配、重复列、8 窗口上限、未知列、自依赖、依赖环、拓扑序确定性 |
| `test_physical_cost.py` | 实测比较计数、稳定性、ASC/DESC、NULLS FIRST/LAST 四种组合、行号尾键、分组独立排序、候选键分区排列、复用谓词、分档定价、费用缺失报错 |
| `test_logical_functions.py` | 三种函数的手工预期：并列、多排序键复合同值组、NULL 同值组、DESC、空表、单行 |
| `test_planner.py` | **独立暴力枚举**（排列 × 转移全枚举）在 12 组随机小实例与依赖实例上交叉验证 DP 最优性；共享物理次序、方向变更禁止复用、空窗口零费用、8 窗口规划、归位检查 |
| `test_integration.py` | 端到端交错依赖链、三方费用一致性（规划器/执行器/独立审计）、篡改逻辑值/费用/排序键均被验证拦截、计划与窗口集不匹配拦截、空表流水线 |
| `test_interleaving.py` | 三种行到达交错（成组/交替/逆序）下逻辑值不变、部分排序保持分区块不交叉、费用随交错变化且仍等于暴力最优、NULL 交错同值组 |
| `test_rowno_tail_invariant.py` | 行号尾键恒为最后一个部件；物理上分开的并列行在 GROUP 窗口中仍属同一逻辑组（硬约束） |

验证入口 `verify(...)` 做两件相互独立的事：

1. **逻辑值**：在剥离执行器输出的表副本上，用 `logical.py` 自己的
   decorate-sort 与同值组逻辑重算全部窗口，逐行比对；
2. **费用**：从零重放计划的每一次排序，重新实测比较次数与组长、重新查费表，
   并同时与规划器和执行器记录的数字比对；最后检查输出行按原行号归位。

任何一项不一致都会抛出 `VerificationError`。所有测试结论均来自真实执行，
演示输出不替代测试验证。

## 边界与限制

- 窗口数量硬上限 8（超出抛 `SpecError`）。
- 同列不得同时用于分区与排序；排序键、分区键内部不得重复。
- 函数与并列策略必须匹配（ROW_NUMBER ↔ ROW_NUMBER；
  DENSE_RANK / CUMULATIVE_COUNT ↔ GROUP）。
- 非 NULL 的异构类型排序会明确报错，不依赖 Python 隐式跨类型比较。
- 仅操作本地合成数据；不访问网络、不读取凭据、不安装依赖。
