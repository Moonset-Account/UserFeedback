# 小张量训练协调模块

同一底层物理权重可同时被嵌入层视图与输出层视图（含转置视图）使用。本模块
负责把各逻辑算子按元素给出的梯度贡献映射回真实物理位置、对重叠视图的贡献
求和、按贡献 ID 去重，并在每个训练步对每个物理位置**只更新一次**，动量按
物理位置保存，未参与本步的权重与动量保持原样。

- 运行环境：Python **3.11.6**，仅标准库，无第三方依赖。
- 所有数值均为本地合成数据，不访问网络、不读取外部凭据。

## 目录结构

```
tensor_coord/
  __init__.py        # 公开 API 导出
  storage.py         # PhysicalStorage / View / InvalidViewError
  coordinator.py     # TrainingCoordinator / StepResult / InvalidContributionError
tests/
  test_handcalc.py   # 两份重叠切片 + 转置视图的完整手算核对
  test_validation.py # 故障恢复、边界、视图重绑
  test_concurrency.py# 多线程交错、回放去重、并发故障
```

## 公开 API

### `PhysicalStorage(data)`

扁平连续的物理权重缓冲。

- `data`：非空的 int/float 可迭代对象（拒绝 bool 元素与 NaN）。
- `.size` / `len(s)`：物理位置数；`.buffer`：权重副本；`s[i]`：读取单个权重。

### `View(storage, shape, strides, offset=0)`

由形状与步长定义在物理存储上的逻辑视图。逻辑位置 `idx` 映射到物理位置
`offset + sum(idx[d] * strides[d])`，梯度按行优先顺序与逻辑元素对齐。

- `.resolve()`：返回与逻辑元素行优先顺序对齐的物理下标元组；越界或出现
  别名（两个逻辑位置映射到同一物理位置）时抛 `InvalidViewError`。
- `.rebind(new_storage)`：把视图重绑到**等长**的另一个存储。已经被协调器
  固定（快照）的贡献不会因此改变。
- `.shape` / `.strides` / `.offset` / `.element_count` / `.storage`。

### `TrainingCoordinator(storage, momentum=0.9)`

- `add_contribution(view, gradient, contribution_id) -> bool`
  - `gradient`：与视图形状一致的嵌套 list/tuple（按行优先展平），元素为数值。
  - 接受时把逻辑梯度逐元素映射到物理下标并**快照固定**；重叠视图的贡献在
    同一物理位置相加。
  - 同一训练步内相同 `contribution_id` 只生效一次，重放（即使 payload 改变）
    返回 `False` 且不重复累加；跨步骤 ID 自动重置。
  - 视图绑定到别的存储、梯度元素数不匹配、含 NaN/非数值、视图映射非法时
    抛出对应异常并把**整步标记作废**。
- `commit_step(learning_rate=0.01) -> StepResult`
  - 作废步：权重与动量完全不变，自动打开下一步，`StepResult.aborted=True`。
  - 合法步：对每个聚合到的物理位置各更新一次：
    `v[p] = momentum*v[p] + g[p]`；`w[p] = w[p] - lr*v[p]`。
    未参与本步的位置保持原样。
- `begin_step() -> int`：显式丢弃当前步待提交内容并开新步。
- `pending_aggregation()`：当前步未提交的 `{物理下标: 聚合梯度}` 副本。
- `.weights` / `.momentum` / `.step_id` / `.storage` 只读快照。
- 所有修改方法内部加 `RLock`，支持多线程交错调用。

### `StepResult`（dataclass）

字段：`step_id, aborted, aborted_reason, touched_positions,
aggregated_gradients, momentum_after, weights_after,
accepted_contribution_ids, duplicate_contribution_ids,
rejected_contributions`；属性 `updated_count`。

### 异常

- `InvalidViewError`：视图定义/映射非法（越界、别名、维度不符、外存储等）。
- `InvalidContributionError`：贡献非法（形状不符、NaN、非数值、ID 不可哈希）。

## 复现步骤

在工作区根目录执行：

```bash
python3 --version            # 应为 Python 3.11.6
python3 -m unittest discover -s tests -p 'test_*.py' -v
# 隔离模式（忽略环境变量/用户站点包）：
python3 -I -m unittest discover -s tests -p 'test_*.py'
```

## 手算核对方案（`tests/test_handcalc.py`）

物理存储 12 个位置，初值 `W = [0..11]`，`momentum=0.5`，`lr=0.5`：

| 视图 | shape | strides | offset | 物理下标（行优先） |
| --- | --- | --- | --- | --- |
| A（切片） | (2,3) | (3,1) | 0 | `[0,1,2,3,4,5]` |
| B（切片，与 A 在 4、5 重叠） | (2,3) | (3,1) | 4 | `[4,5,6,7,8,9]` |
| T（转置视图） | (3,2) | (1,3) | 0 | `[0,3,1,4,2,5]` |

第一步梯度：A=`[[1,2,3],[4,5,6]]`、B=`[[10,20,30],[40,50,60]]`、
T=`[[-1,-2],[-3,-4],[-5,-6]]`。测试用字面量逐位置核对：

- 聚合梯度，例如位置 4 = A:5 + B:10 + T:(-4) = **11**，位置 5 = **20**；
- 第一步动量（首步即聚合梯度）与更新后权重，例如
  位置 4：`v=11`，`W'=4-0.5*11=-1.5`；
- 位置 10、11 未参与，权重与动量保持初值；
- 同一步内 A/B/T 同 ID 回放（payload 已改）被忽略，聚合值不变；
- 第二步只在位置 4 再得梯度 100：`v4=0.5*11+100=105.5`，
  `W''4=-1.5-0.5*105.5=-54.25`；收到 0 梯度的位置动量按系数衰减、权重不变；
  未参与第二步的位置 6–11 全部保持第一步结果。

## 验证范围

共 34 个 unittest 用例，全部实际执行通过（隔离模式与重复运行亦通过）：

1. **正常路径与手算（10 项）**：三个视图的物理映射、重叠求和、逐位置
   聚合梯度/动量/更新权重字面量核对、回放去重、跨步动量累积、未参与位置
   不变、形状不匹配时不产生部分更新。
2. **故障恢复与边界（20 项）**：offset/步长越界、视图别名、维度不符、
   负/布尔维度、NaN 权重与梯度、非数值梯度、元素数不符、外存储视图、
   连续多步作废后合法步恢复、`begin_step` 丢弃、空步 no-op、单元素视图、
   三路重叠、`lr=0`、负学习率拒绝、momentum 系数校验、嵌套梯度行优先展平、
   跨步骤 ID 重置、视图重绑后已固定贡献不被替换、重绑长度校验。
3. **并发交错（4 项）**：6 线程经 Barrier 同时登记唯一 ID 贡献并核对
   手算聚合；8 线程抢 4 个相同 ID 验证只计一次（20 轮压力重复）；
   合法/非法贡献并发时整步作废且下一步恢复；20 轮重复并发运行稳定性。

未使用任何自写演示的预置输出替代功能验证；所有断言均针对模块真实计算结果。
