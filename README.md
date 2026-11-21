# incremental_reach — 增量可达算子

面向“递归查询”式可达性维护的纯标准库实现（Python 3.11.6）。维护一组可增删的
**种子**（递归查询的起点）与一张有向**边**集，每批变更后只输出**净**增量：
新获得可达的节点与失去可达的节点，并附带本次被重新展开出边的节点。

## 公开 API

```python
from incremental_reach import IncrementalReachability, Delta

op = IncrementalReachability(seeds={"s"}, edges=[("s", "a"), ("a", "b")])
```

### `IncrementalReachability(seeds=(), edges=())`

构造算子。`seeds` 为任意可哈希对象；`edges` 为 `(source, target)` 二元组序列。
重复边被忽略（简单有向图），允许自环。种子即使没有任何边也始终可达。

### `op.apply(seeds_add=(), seeds_remove=(), edges_add=(), edges_remove=()) -> Delta`

以**一个原子批次**应用变更并返回该批次的净增量。

- 批次内语义为 **先删后补**：先应用移除，再应用新增。
- 同一元素同时出现在 add 与 remove 中：删除先执行、新增后执行，因此**结果保留该
  元素**（重新加入）。对“批量替换边/种子”天然安全。
- 对不存在的种子/边执行移除是无操作；重复新增是无操作。
- 增量是“批前状态”与“整批完成后状态”的差，**不存在中间态**：同一批里为
  被删边补上的替代路径会即时生效，因此不会产生闪断（不会先把节点报成删除再报新增）。

便捷封装：`add_seeds`、`remove_seeds`、`add_edges`、`remove_edges`。

### `Delta`

| 字段 | 含义 |
| --- | --- |
| `added` | 本批由不可达变为可达的节点（之前不可达） |
| `removed` | 本批由可达变为不可达的节点（之前可达） |
| `reexpanded` | 本批**重新展开出边**的节点；均为批后仍可达的节点；不受影响的分量绝不出现在这里 |

- `added` 与 `removed` 恒不相交。
- 仅改路而不改变可达集合时，`added`/`removed` 为空（`delta.is_empty` 为 `True`），
  即便有节点被重新展开。
- `delta.replay_onto(state)`：把增量重放到调用方持有的可达集合上并返回新集合
  （`state -= removed; state |= added`）。参见下文“重放一致性”。

### 只读查询

`op.reachable`（`frozenset`）、`op.is_reachable(node)`、`op.seeds`、`op.nodes`、
`op.edges`、`op.successors(node)`、`op.predecessors(node)`。

## 设计要点

- **环安全**：不靠入度计数。在被影响区域内，只有“区域内种子”和“区域外仍可达、
  且有边指向区域内的边界节点”会被当作可达源；随后仅在区域内做正向传播。因此
  当某个环唯一的外部入口被移除时——即便环内每个点仍有环内前驱——整环被撤回。
- **其他种子守恒**：只要还有任一进化种子能进入，相应节点就保持可达；只有当所有
  来源都消失时才撤回。
- **局部性**：受影响的“候选入口”仅包括：被删的种子、被删且**源与目标在批前均
  可达**的边的目标、被新增的种子、以及由**批前已可达**源点出发且目标**批前不可达**
  的新增边的目标。其余一切通过对新边做正向闭包得到。与原变更完全分离、未改动的
  分量不会被遍历（见测试与 `bench_localized.py`）。
- **重放一致性**：调用方从算子初始 `reachable` 出发，逐批 `state -= delta.removed;
  state |= delta.added`，始终能拼回“从全部种子出发”的可达集合。

## 复现与运行

无需安装任何依赖，标准库即可。在工作区根目录：

```bash
python3 --version          # 期望 Python 3.11.6
python3 -m unittest discover -s tests -t . -v
```

可选的局部性基准（自建合成图，非单元测试）：

```bash
python3 bench_localized.py
```

## 已验证范围（真实执行）

`tests/` 下共 **39** 个 unittest，全部通过（`Ran 39 tests ... OK`）。覆盖：

- 正常场景：链、空图、无种子、种子即边目标、重复边、自环、非法边报错。
- 边界场景：空批次、删除不存在的边、重复新增、删除全部种子、
  同一批“先删后补”一条原本不存在的边（回归测试）、
  单批次内替换边/种子时的净增量。
- 交错/原子场景：同批删除唯一环入口并补上替代入口，环不闪断；
  对照用例证明拆成两批时中间态确实会报告撤回（说明批处理的价值）。
- 环：移除唯一外部入口后整环撤回（并断言环内前驱仍存在）；两条入口删一条保留；
  环内新增种子、批内新增环内种子、第二个种子从外部供流的各种保留情形。
- 局部性：用可观测子类记录被展开节点，验证完全分离分量从不被遍历；
  2000 条边死岛下的单链改动不触碰死岛；`reexpanded ⊆ 批后可达集`。
- 随机差分：`random` 生成大批次（多随机种子、含环、增删种子与边混合），
  每一步把 `apply` 的 `added`/`removed`/`reachable` 与独立的全量 BFS 参考实现
  （`tests/reference.py`）逐项比对，并逐批验证重放拼回、`added ∩ removed = ∅`。
  测试内核含 3 个固定种子 × 300 步；另以一次性脚本做过 300 组 × 400 步的更强
  随机差分（**该扩展跑法未纳入 unittest 套件**，仅作为开发期验证记录）。

## 边界与限制

- 图全部驻留内存；类非线程安全；无持久化。
- 当节点既不是种子、也不是任何存活边的端点时会从 `op.nodes` 中消失（不影响可达集）。
- 节点须可哈希；边必须是长度 2 的可解包对象，否则 `ValueError`。
- 复杂度随**被影响区域**规模变化，而非全图规模；但单批次最坏情况（如删掉一个超大
  分量的唯一入口）仍与该分量大小成正比，这是撤回集合本身的规模决定的。

## 文件结构

```
incremental_reach/
  __init__.py      导出 Delta、IncrementalReachability
  engine.py        算子实现
tests/
  __init__.py
  reference.py     独立 BFS 参考实现（仅供测试比对）
  test_incremental_reach.py   39 个 unittest
bench_localized.py 局部性基准脚本（合成数据，非单元测试）
```
