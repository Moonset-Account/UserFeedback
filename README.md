# 地区计数一致化模块（隐私统计公开端后处理）

差分隐私把各层地区人数独立加噪后，会出现"区县之和大于全市"这类层级不一致。
本模块在**不重新接触原始数据**的前提下，对一棵地区树上的带噪整数计数做公开端
一致化：为每个节点选出非负整数，使父节点计数恒等于孩子之和、根节点等于固定
公开总数，并使按置信权重加权的平方改动总和最小。

## 数学定义

- 输入：地区树；每个节点带
  - `noisy`：可公开的带噪整数（允许为负）；
  - `weight`：正整数置信权重；
  - 叶子可声明 `structural_zero=True`（结构性零，校正后恒为 0）；
  - 根节点的 `noisy` 即固定公开总数 `T`。
- 决策：内部节点计数由叶子唯一确定（父 = 子之和），只需为可调节叶子选
  非负整数 `y_j`，满足总和为 `T`。
- 目标：

  ```
  minimize  Σ_v weight_v · (x_v − noisy_v)²
  ```

  其中 `x_v` 是节点 `v` 子树内的叶子计数之和（根 `x_root = T`）。
- 平局：两套解总代价相同时，取按叶编号（DFS 先序）排列后字典序更小的
  叶子计数向量。

## 算法

每个节点对应一个子树叶子集合，全部子树集合构成**层状族（laminar family）**，
因此目标函数是 **M-凸（离散凸）二次函数**。把一单位计数分给叶子 `j` 的边际代价为

```
m_j = Σ_{v 是 j 的祖先（含叶子自身、含根）} weight_v · (2·(x_v − noisy_v) + 1)
```

边际代价随该叶子已分得的数量单调不减，故反复把下一单位分给当前边际最小的叶子
即得到全局最优整数解。最小堆维护各叶子边际，过期条目在堆顶惰性重算替换。
边际持平时优先选择叶编号更大者（堆键取负编号），把计数尽量"压后"分配，
得到字典序最小的最优向量。复杂度 O((T+L) log L) 次堆操作、每次边际重算
O(树高)，可支撑总数一百万、数百叶子。

小树上另提供穷举求解器 `brute_force_optimal`，枚举所有非负整数组合并按同一
字典序规则选优，用于独立核对。

## 公开 API

```python
from consistency import (
    Node, RegionTree,        # 树数据结构与构建/校验
    Reconciliation,          # 结果 dataclass
    ConsistencyError,        # 不可行/非法输入异常
    reconcile,               # 主求解入口
    brute_force_optimal,     # 小树穷举核对
)
```

### `Node(node_id, noisy, weight, structural_zero=False, children=[])`

- `node_id`：非空字符串，树内唯一；
- `noisy`：整数（根节点上表示固定公开总数，必须非负）；
- `weight`：正整数；
- `structural_zero`：只能在叶子上为 `True`；
- `children`：`Node` 列表。

### `RegionTree.build(root) -> RegionTree`

校验并构建树，检查编号唯一、权重为正整数、`noisy` 为整数、结构性零只在叶子、
无重复/环等。提供 `leaves`（DFS 先序，即叶编号顺序）、`parent`、`depth`、
`nodes` 等属性。

### `reconcile(tree) -> Reconciliation`

返回：

- `counts: dict[str, int]`：每个节点校正后的非负整数，满足父=子之和、根=`T`；
- `total_cost: int`：加权平方改动总和；
- `leaf_order: list[str]`：叶编号顺序；
- `structural_zeros: frozenset[str]`：结构性零叶子编号。

抛出 `ConsistencyError` 的情形：根总数为负；全部叶子为结构性零而总数大于 0。

### `brute_force_optimal(tree, *, max_combinations=200_000) -> Reconciliation`

枚举所有可行叶子组合（星条法），与 `reconcile` 使用相同平局规则；组合数超过
上限时抛 `ValueError`，仅供小树核对。

## 使用示例

```python
from consistency import Node, RegionTree, reconcile

c1 = Node("county-1", 60, 1)
c2 = Node("county-2", 70, 1)
root = Node("city", 100, 2, children=[c1, c2])   # 全市固定总数 100

tree = RegionTree.build(root)
result = reconcile(tree)
print(result.counts)        # {'city': 100, 'county-1': .., 'county-2': ..}
print(result.total_cost)    # 加权平方改动总和
assert result.counts["county-1"] + result.counts["county-2"] == 100
```

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

仅使用 Python 3.11 标准库，无第三方依赖，无需联网或安装任何包。

## 验证范围

测试全部基于本地合成数据，共 34 个 unittest 用例（`tests/`）：

- `test_basic.py`：树构建与输入校验（重复编号、非正权重、布尔冒充整数、
  结构性零位置等）；手工算例（对称平局的字典序结果、权重倾斜、结构性零、
  题面"区县之和大于全市"场景、T=0 全零、单叶子）；不可行情形与穷举保护上限。
- `test_exhaustive.py`：80 组随机小树（2–6 叶子、T≤8、随机权重/带噪/结构性零、
  随机树形）上 `reconcile` 与 `brute_force_optimal` 的**完整结果逐例相等**
  （同时核对最优代价与字典序平局）；重复调用确定性；非负、父=子之和、根=总数、
  代价独立重算等不变量；保证结构性零分支被实际覆盖。
- `test_large.py`：总数 1,000,000 / 300 叶子（含约 10% 结构性零）计时与不变量；
  257 叶子 / 50 万、200 层深链树、100 叶子半数结构性零、极端权重倾斜、T=0 等
  交错与边界场景。

另有独立脚本 `scratch_verify.py`（不属于 unittest 套件）用独立随机种子再做
150 组小树穷举等价核对（实测 0 组不一致）并记录百万规模计时：

| 叶子数 | 总数 | 实测耗时（本机） |
| --- | --- | --- |
| 100 | 1,000,000 | 约 7 秒 |
| 300 | 1,000,000 | 约 12–15 秒 |
| 500 | 1,000,000 | 约 16–20 秒 |

已知边界与说明：

- 带噪值允许为负，校正结果始终非负；
- 根节点的带噪值被视为固定公开总数，根项代价恒为 0，但根权重仍参与各叶子
  边际计算；
- 结构性零叶子不参与分配，但其带噪值与最终 0 之间的加权平方差仍计入总代价；
- 穷举器有组合数上限，仅用于小树核对，生产规模请用 `reconcile`。
