# 群组密钥信封接收节点规划模块

为 LKH 风格分发树的下一期群组密钥挑选**信封接收节点**：在哪些节点钥上封装新群组密钥，
既能覆盖每一个在册成员叶子，又排除离组者与泄露节点钥的持有风险，并在可行方案中取
**总成本最低、平局取排序后节点编号序列字典序最小**的方案。

## 目录与实际入口

```
workspace/
├── src/envelope_planner/
│   ├── __init__.py        # 对外导出的公共 API
│   └── planner.py         # 树建模、树形 DP、不可行诊断、投递模拟
├── tests/
│   └── test_envelope_planner.py   # unittest 验收（含朴素枚举交叉校验）
└── README.md
```

实际调用入口（无第三方依赖，Python 3.11 标准库即可）：

```python
import sys
sys.path.insert(0, "src")

from envelope_planner import (
    DistributionTree,     # {节点: 父节点} 构造，根父节点为 None
    EnvelopePlan,         # 规划结果（feasible / envelopes / 诊断）
    plan_envelopes,       # 规划主入口
    simulate_delivery,    # 模拟每个信封能被哪些持钥叶子打开
    InputError,           # 输入结构不合法
)

tree = DistributionTree({0: None, 1: 0, 2: 0, 3: 1, 4: 1, 5: 2, 6: 2})
plan = plan_envelopes(
    tree,
    members=[3, 4, 5, 6],          # 本期在册成员叶子，必须全部被覆盖
    holders=[3, 4, 5, 6],          # 实际持钥叶子（离组未退钥者也可能在内）
    leaked=[1],                    # 已泄露节点钥对应节点
    costs={0: 5, 1: 10, 2: 10, 3: 1, 4: 1, 5: 1, 6: 1},
    leavers=None,                  # 可选；缺省 = holders - members
)
print(plan.to_dict())
```

## 需求语义与建模决策

1. **节点钥独立、成员持钥路径**：每个成员叶子持有根到自身叶子路径上的全部节点钥。
   在节点 `v` 放信封，凡位于 `v` 子树内且实际持钥的叶子都能打开它。
2. **选点安全约束（三者同时满足才可选）**：
   - 节点在 `costs` 中（未给成本视为不可用）；
   - 节点不在 `leaked` 中。泄露只禁用该节点自身，不连累其祖先或后代；
   - 节点的可打开集合（子树内持钥叶子）不含离组者。
3. **离组者**：缺省取 `holders - members`（离组但仍持钥者）；也可用 `leavers` 显式声明，
   显式离组者即使不在 `holders` 中也按风险阻断其全部祖先。`leavers` 与 `members` 重叠直接报错。
4. **覆盖要求**：每个在册成员叶子至少能被一个选中信封打开。成员若不在 `holders` 中，
   或其根到叶子路径上没有任何可选节点，方案即不可行。
5. **优化目标**：总成本最低；平局取排序后节点编号序列的字典序最小值（Python 元组比较：
   短序列若是长序列前缀则短者更小）。
6. **反链规范化**：成本非负时，含冗余信封（某选中节点是另一选中节点祖先）的方案删除冗余后
   成本不增、覆盖不变，因此最优解统一在**反链覆盖**（选中节点互不为祖先）中选取；
   零成本边界下同样输出最精简方案。

## 算法

可行性预检 + 后序树形 DP，整体 `O(n)`（`n` 为树节点数；排序开销与节点数同阶）。

- 预检逐叶判定：不持钥 → `missing_leaf_key`；路径上所有节点都不可选 → `no_safe_envelope`，
  并在 `detail` 中逐节点列出泄露 / 不可用 / 离组者可打开的具体原因。
- DP 状态 `dp[v] = (成本, 排序节点序列, 覆盖集)`，两种互斥选择：
  - 方案 A：直接在 `v` 放一个信封（仅当 `v` 可选），覆盖 `v` 子树内全部在册成员；
  - 方案 B：不在 `v` 放信封，合并各孩子子树的最优方案，并显式校验覆盖完整性；
  - 两种都不成立时状态为 `None`（子树不可行）。
- 比较键为 `(总成本, 排序节点序列)`，天然实现成本最优 + 字典序平局。

## 输出（可模拟的封装清单）

`EnvelopePlan`（`to_dict()` 可直接序列化）：

- 可行：`feasible=True`、`envelopes`（每个含 `node` / `cost` / `recipients`）、
  `total_cost`、`covered_leaves`；
- 不可行：`feasible=False`、空信封清单、`uncovered_leaves` 逐叶给出 `leaf`、
  `reason`（`missing_leaf_key` / `no_safe_envelope`）与中文 `detail`。

`simulate_delivery(plan, tree, holders)` 返回 `{信封节点: 能打开它的持钥叶子}`，
可据此断言“在册成员至少能打开一个、离组者一个都打不开”。

## 验证范围与运行方式

运行（真实断言，无 mock）：

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_envelope_planner.py` 共 29 个用例，分四组：

| 分组 | 覆盖内容 |
| --- | --- |
| `AcceptablePlanTests` | 根节点胜出、叶子信封胜出、离组者阻断祖先、显式离组者、泄露只影响单节点、两类字典序平局、零成本边界、空成员、接收者排除非持钥者、`to_dict` 序列化 |
| `RejectedAndInfeasibleTests` | 成员不持钥、路径被泄露+离组者全部封死、缺成本节点绕行、全部不可用逐叶诊断、重复输入去重 |
| `InputValidationTests` | 多根、父节点缺失、环、空树、未知成员/泄露/成本节点、内部节点冒充成员、负成本、非数值成本、离组者与成员重叠 |
| `CrossCheckAcrossScenariosTests` | 6 个交错场景 × 4 组成本向量，用独立实现的**朴素枚举（仅在同一反链方案空间内枚举全部子集）**交叉验证 DP 的可行性、总成本与字典序最优序列 |

每个可行用例同时通过 `simulate_delivery` 校验两条交叉约束：在册成员均可打开至少一个信封，
离组者打不开任何信封。

## 边界与限制

- 节点编号支持任意可哈希、可比较类型（整数 / 字符串等）；同方案内混合类型按 Python 默认顺序比较。
- 成本要求非负有限数值（`bool` 与非数值类型拒绝）。
- 本模块仅做本地防御性规划与验证，不包含任何真实目标探测、网络访问或攻击载荷。
