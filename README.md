# 惰性参数服务（Lazy Parameter Service）

稀疏动量 SGD 的惰性行更新实现。训练每步只触碰少量嵌入行，但调用方可以在
任意一步完成后读取任一行的当前参数 `w` 和动量 `v`。

- 运行环境：Python 3.11.6，仅标准库，无第三方依赖。
- 代码：`lazy_param_service.py`；测试：`tests/test_lazy_param_service.py`。

## 更新规则

每步提交学习率 `eta`、衰减系数 `d` 和稀疏梯度 `g`（未上报的行视为 `g = 0`），
`mu` 固定，`mu` 与 `d` 均在 `[0, 1]` 内。逐行参考规则：

```
v' = mu * v + g
w' = d * w - eta * v'
```

## 惰性化原理

对零梯度行，单步演化是线性变换：

```
[w']   [d   -eta*mu] [w]
[v'] = [0      mu  ] [v]
```

任意零梯度区间的效果可预先汇总为三元组 `(D, B, m)`（即矩阵 `[[D, B], [0, m]]`），
作用为 `w' = D*w + B*v, v' = m*v`。提交时每步追加一个叶子摘要，并按
“二进制计数器”方式在线合并相邻等长区间，形成持久化区间森林（节点总数 ≤ 2T，
合并后的旧节点保留为子节点，供任意后缀区间查询复用）。

- `commit` 只更新当步有梯度的行，其余行不动；
- `read` 一个久未出现的行时，把它上次同步步到当前步之间的区间分解为
  已存摘要并依次复合，一次性补齐所有中间零梯度步；
- 跨 N 个空步的单行查询至多读取 `2*ceil(log2(N)) + 2` 个区间摘要
  （森林根数 ≤ popcount(T)，唯一被切开的根内每层至多取一个节点），
  实际读取次数由 `last_summary_reads` 提供。实测 N=1000 时仅读取 6 个。

## 公开 API

```python
from lazy_param_service import LazyParamService, StepByStepParams
```

### `LazyParamService(initial_weights, mu)`

- `initial_weights`：每行初始参数 `w`（行标识为任意可哈希对象）；动量 `v` 初值为 0。
- `mu`：动量系数，须在 `[0, 1]` 内，否则 `ValueError`。

| 成员 | 说明 |
| --- | --- |
| `commit(eta, d, grads=None) -> int` | 提交一步。`eta >= 0`，`d ∈ [0,1]`（否则 `ValueError`）；`grads` 为稀疏梯度映射，未知行抛 `KeyError`。只更新当步有梯度的行，返回新步号（从 1 起）。 |
| `read(row) -> (w, v)` | 读取某行当前参数与动量；必要时补齐中间零梯度步。未知行抛 `KeyError`。 |
| `step` | 当前已提交步数。 |
| `rows` | 全部行标识。 |
| `last_summary_reads` | 最近一次 `read`（或提交时的行补齐）实际读取的区间摘要数量。 |

### `StepByStepParams(initial_weights, mu)`

逐步更新的参考实现：每步对全部行应用规则，用于比对验证。
接口为 `commit(eta, d, grads=None)` / `read(row)` / `w` / `v` / `step`。

### 使用示例

```python
svc = LazyParamService({"row0": 0.5, "row1": -1.0}, mu=0.9)
svc.commit(eta=0.1, d=0.99, grads={"row0": 0.3})   # 只更新 row0
for _ in range(999):
    svc.commit(0.1, 0.99, {})                       # row0/row1 均不上报
w, v = svc.read("row1")                             # 一次性补齐 1000 步
print(svc.last_summary_reads)                       # 实际读取的摘要数（对数级）
```

## 复现步骤

```bash
cd <工作区根目录>
python3 -m unittest discover -v
```

## 验证范围

测试均从题面场景选取，与逐步更新参考实现 `StepByStepParams` 在同一个小表上
比对 `w`、`v`，判定标准为绝对或相对误差在 `1e-9` 内：

- 正常场景：6 行 × 300 步随机稀疏梯度、每步随机交错读取逐行比对
  （`test_randomized_sparse_matches_reference`）；每行每步均有梯度的稠密情形
  （`test_dense_gradient_every_step`）。
- 边界场景：`mu=0`、`mu=1 且 d=1`、`d=0`、`eta=0`、单步、提交前读取初值、
  从未上报的行跨 500 步一次性补齐。
- 对数级摘要：跨 1000 个空步的单行查询，断言实际读取摘要数
  `<= 2*ceil(log2(N)) + 2` 并打印实际次数（实测 6 个）；已同步行重复读取
  摘要数为 0。
- 交错场景：A 行每 3 步、B 行每 5 步上报、C 行从不上报，读取与提交交错。
- 参数校验：`mu`/`d` 越界、负 `eta`、未知行均抛异常。

所有列出的测试均已实际执行通过（12 个用例，`OK`）。
