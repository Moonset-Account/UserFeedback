# 灾备恢复点选择（DR Recovery Point Planner）

为相互写入、相互引用的多个服务选择灾备恢复点：每个服务恰好选择一份候选备份，
在满足全部跨服务依赖与总恢复字节上限的前提下，使加权业务损失最小；
无解时区分“加预算可解决”与“结构性无解”。

- 运行环境：Python 3.11.6，仅使用标准库（`dataclasses`、`itertools`、`typing`）。
- 规模约束：最多 **6 个服务**、每个服务最多 **6 份候选备份**，
  组合空间至多 6⁶ = 46656，穷举即可保证全局最优。

## 1. 问题形式化

设服务集合为 `S = {1..n}`，服务 `i` 的候选备份集合为 `B_i`，需选出
`x_i ∈ B_i`（每服务恰一份）。

**依赖约束**：每份备份 `x_i` 可声明若干 `(服务 j, 最低位置 p)` 要求，
当且仅当被选备份 `x_j` 的提交位置 `pos(x_j) ≥ p` 时该要求满足。
依赖允许指向自身、允许服务间循环引用——只比较各服务最终的恢复位置，
不要求引用图无环。指向规划集合之外服务的要求永远无法满足。

**字节约束**：`Σ bytes(x_i) ≤ byte_budget`（闭区间，恰好相等可行）。

**优化目标**（按优先级）：

1. 依赖全部满足；
2. 总字节不超过上限；
3. 加权损失最小：
   `loss = Σ (latest_pos(i) − pos(x_i)) × weight(i)`，
   其中 `latest_pos(i)` 是服务 `i` 全部候选备份中的最新提交位置；
4. 损失相同取总字节更少；再相同按各服务备份标识的字典序确定确定性结果。

**无解判定**：

| 情形 | `reason` | `budget_can_fix` | 含义 |
| --- | --- | --- | --- |
| 预算内无解、但存在依赖可行组合 | `budget_exceeded` | `True` | 把上限提到 `required_budget` 即可解，`selected` 给出该预算下的最优选择 |
| 不存在任何依赖可行组合 | `infeasible_dependencies` | `False` | 依赖位置无法达到，加多少字节都无解 |
| 某服务没有候选备份 | `empty_backup_set` | `False` | 无法做到“每服务恰选一份”，与字节无关 |

`required_budget` 的语义是**所有依赖可行组合中的最小总字节**，
因此“增加可恢复字节数能否解决”等价于 `budget_can_fix`。

## 2. 公开 API

包入口 `dr_planner`：

### 数据模型（`dr_planner/model.py`，均为不可变 dataclass）

- `Backup(backup_id: str, position: int, bytes_required: int, requirements: FrozenSet[Tuple[str, int]] = frozenset())`
  - `position`：本备份提交位置（非负整数，越大越新）。
  - `bytes_required`：恢复字节数（非负整数）。
  - `requirements`：`(被引用服务标识, 该服务最低提交位置)` 的集合；
    同一服务出现多条时自动合并为最严格（最大）的位置要求。
- `Service(service_id: str, weight: int, backups: Tuple[Backup, ...])`
  - `weight`：业务权重（非负整数，0 表示该服务位置差不贡献损失）。
  - 只读属性 `latest_position`：候选备份中的最新位置（无备份时为 0）。
- `RecoveryResult`：结果对象，字段如下。
  - `feasible: bool` —— 当前预算下是否有解。
  - `selected: Optional[Tuple[Backup, ...]]` —— 各服务选中的备份，顺序与入参一致；结构性无解时为 `None`。
  - `total_bytes: Optional[int]` —— 选中组合的总字节。
  - `loss: Optional[int]` —— 加权损失。
  - `budget_can_fix: bool` —— 增加字节上限能否解决。
  - `required_budget: Optional[int]` —— 可解所需的最低字节上限。
  - `reason: Optional[str]` —— 无解原因，取值为常量 `REASON_BUDGET` / `REASON_INFEASIBLE_DEPS` / `REASON_EMPTY_SERVICE`。
- 规模常量：`MAX_SERVICES = 6`、`MAX_BACKUPS_PER_SERVICE = 6`。

### 主入口（`dr_planner/planner.py`）

```python
plan_recovery(services: Sequence[Service], byte_budget: int) -> RecoveryResult
```

- 服务顺序即结果顺序；服务标识必须唯一，备份标识在所属服务内唯一。
- 非法入参（空服务集合、超规模、重复标识、负数位置/字节/权重/预算等）抛出 `ValueError`。

### 字典解析助手（`dr_planner/parsing.py`）

- `service_from_dict(mapping) -> Service`
- `backup_from_dict(mapping) -> Backup`

接受 JSON 风格数据，`requirements` 为 `{服务标识: 最低位置}` 映射，
`weight` 缺省为 1。

## 3. 使用示例

```python
from dr_planner import Backup, Service, plan_recovery

orders = Service("orders", 3, (
    Backup("o-old", 100, 200, frozenset((("inventory", 80),))),
    Backup("o-new", 120, 600, frozenset((("inventory", 110),))),
))
inventory = Service("inventory", 2, (
    Backup("i-old", 80, 150),
    Backup("i-new", 110, 500),
))

result = plan_recovery([orders, inventory], byte_budget=700)
print(result.feasible)                        # True
print([b.backup_id for b in result.selected])  # ['o-old', 'i-new']
print(result.total_bytes, result.loss)       # 700 60
```

`scripts/smoke_example.py` 是同一例子的可运行脚本，覆盖预算从不足到充裕的四档输出。

## 4. 复现步骤

在工作区根目录执行（无需安装任何依赖）：

```bash
# 版本确认（要求 3.11.6）
python3 --version

# 运行全部 unittest 测试（推荐，隔离模式，避免工作区污染）
python3 -I -m unittest discover -s tests -t . -v

# 普通模式等价命令
python3 -m unittest discover -s tests -t . -v

# 人工冒烟脚本（真实调用公开 API，非测试桩）
python3 -I scripts/smoke_example.py
```

说明：`-I` 隔离模式会把当前目录移出 `sys.path`，因此测试必须用
`discover -s tests -t .`（`-t .` 把工作区根显式设为 top-level 目录）。
直接 `python3 -m unittest tests.test_planner` 的包式调用在普通模式下同样可用，
但在 `-I` 下需要额外设置 `PYTHONPATH`，不属于推荐路径。

## 5. 验证范围

测试位于 `tests/`，共 **46 个用例**，全部从工作区根目录可发现。
测试内另写了一份不调用被测目标函数的独立枚举/重算参考实现做交叉校验。

正常与交错场景：

- 无依赖多服务的预算分级选择、预算恰好等于边界（闭区间）。
- **“某服务最新备份迫使另一服务选大副本”**：新备份携带跨服务高位置要求，
  预算受限时规划器主动放弃该服务的最新备份（`ForcingTradeoffTest`）。
- 服务间**循环引用**：环上服务只能联合升级，单边升级依赖不满足；
  验证低预算选联合旧档、高预算选联合新档、中间预算的行为。
- 自引用依赖（要求不超过/超过自身位置两种结果）。
- 权重翻转：同一可行集合下，改变业务权重使最优解在两个服务间切换。
- 上限规模 6 服务 × 6 备份（46656 组合），与独立参考实现逐预算对比全局最优。
- JSON 风格字典输入的端到端流程（`tests/test_parsing.py`）。

边界与防御场景：

- 零字节预算、零字节备份、零业务权重（含“零权重服务升级不减少损失”的语义对照）。
- 预算内无解时 `budget_can_fix=True` 且 `required_budget` 等于全局最小可行字节。
- 依赖位置不可达、依赖指向不存在的服务：`budget_can_fix=False`。
- 服务无候选备份：`empty_backup_set`，与字节预算无关。
- 平局的确定性：先比损失、再比字节、最后比备份标识字典序。
- 入参校验：空服务集合、超 6/6 上限、重复服务/备份标识、负数、非法类型、
  重复依赖键合并取最严格值、结果对象不可变。

## 6. 算法与复杂度

对笛卡尔积 `B_1 × … × B_n` 做一次穷举（`itertools.product`），
每个组合做 O(依赖条目总数) 的可行性校验，并同时维护两个最优：

1. **预算内最优**：键 `(loss, total_bytes, backup_ids 字典序)`；
2. **不限预算的最小字节可行组合**：键 `(total_bytes, loss, backup_ids 字典序)`，
   一次遍历即可得到无解时的 `required_budget` 与对应选择，无需二次枚举。

时间复杂度 O(6ⁿ × D)（n ≤ 6，D 为组合内依赖条目数），最坏约 4.7 万组合，
实测整套 46 个用例（含多个全量枚举）耗时约 0.7 秒；空间复杂度 O(n)。

## 7. 项目结构

```
dr_planner/
    __init__.py    # 公开 API 导出
    model.py       # Backup / Service / RecoveryResult 不可变模型与校验
    planner.py     # plan_recovery 主算法、原因常量、规模上限
    parsing.py     # 字典/JSON 风格数据解析助手
tests/
    __init__.py
    test_planner.py  # 主算法测试（含独立参考实现交叉校验）
    test_parsing.py  # 解析助手与端到端测试
scripts/
    smoke_example.py # 人工冒烟脚本，输出真实计算结果
```
