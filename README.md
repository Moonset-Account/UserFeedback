# Gradient Layout Compiler（梯度布局编译与双向映射）

训练运行时中，优化器按**固定长度**接收每个 rank 的一段扁平梯度字节，而模型侧按
**参数名 + 逻辑下标**读写。本模块从参数声明编译出静态布局，并提供两个方向的映射：
按名收集任意矩形切片（`gather`），把该切片的更新写回相应分片（`scatter`）。

仅使用 Python 标准库，无需安装任何依赖。运行环境：Python 3.11.6。

## 布局规则

- 只有**可训练**（非冻结）参数进入梯度缓冲，按**声明顺序**排布；冻结参数不占位。
- 每个参数的起始字节偏移对齐到 `alignment` 的整数倍，间隙为零填充。
- 末尾补零至 `lcm(alignment, num_ranks)` 的整数倍，因此每个 rank 恰好拥有
  `total_bytes // num_ranks` 字节，且 rank 边界保持对齐。
- 参数可以**跨 rank**，单个元素也可以跨 rank 边界（读写自动拼接相邻分片）。
- 多维参数按行主序（C order）展开；矩形切片（如取若干列）的逻辑行在缓冲中
  一般**不连续**，映射按坐标逐元素寻址。
- `gather`/`scatter` 只访问参数自身的字节，**从不触碰填充位**。
- 逻辑映射（参数名 + 下标 → 绝对字节偏移）与 `num_ranks` 无关，因此**改变 rank
  数后，按同一组逻辑更新重建的每个参数逐元素一致**（测试覆盖 1/2/3/5/8 rank）。

## 公开 API（`gradient_layout.py`）

### 声明与编译

```python
from gradient_layout import GradientLayout, ParamSpec, LayoutError

specs = [
    ParamSpec("w1", shape=(2, 3), width=4),                 # float32，可训练
    ParamSpec("bn", shape=(4,), width=4, frozen=True),      # 冻结，不进缓冲
    ParamSpec("w2", shape=(4,), width=4),
    ParamSpec("emb", shape=(8,), width=2, kind="int"),      # int16
]
layout = GradientLayout(specs, alignment=64, num_ranks=4)
```

- `ParamSpec(name, shape, width=4, frozen=False, kind="float")`
  - `shape`：非空正整数元组；`width`：元素字节宽度；`kind`：`"float"`（2/4/8 字节）、
    `"int"`、`"uint"`（1/2/4/8 字节）。
- `GradientLayout(specs, alignment=64, num_ranks=1)` 属性：
  - `params`：`{name: ParamLayout}`，`ParamLayout` 含 `offset/nbytes/strides/byte_span` 等；
  - `total_bytes`、`rank_bytes`、`num_ranks`、`alignment`。

### 缓冲与映射

- `new_buffers() -> list[bytearray]`：分配按 rank 切好的零初始化缓冲。
- `padding_spans() -> list[(start, end)]`：返回纯填充字节区间。
- `gather(buffers, name, selection=None)`：读取矩形切片，返回嵌套列表
  （全整数下标返回标量）。`selection` 为 `int`/`slice` 或其元组，缺省维度取全部；
  支持负下标；slice 仅支持单位步长（矩形）。
- `scatter(buffers, name, values, selection=None)`：把更新写回对应分片，
  `values` 可为扁平序列或与切片形状一致的嵌套列表；只写选中元素的字节。
- `apply_updates(updates, buffers=None)`：按序应用
  `(name, values)` 或 `(name, selection, values)` 更新并返回缓冲。
- `rebuild(buffers) -> {name: 完整参数值}`：按名重建全部可训练参数。

### 异常

- `LayoutError`（`ValueError` 子类）：非法声明、重复名称、缓冲数量/长度不符、
  更新值数量不匹配、对不可变缓冲执行 `scatter`。
- `KeyError`：未知或已冻结的参数名。
- `IndexError` / `TypeError`：越界下标、越界 slice、非单位步长、维度过多、非法下标类型。

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

## 验证范围（tests/test_layout.py，19 个用例，全部实测通过）

- **布局编译**：声明顺序、对齐偏移、冻结参数排除与不留空洞、末尾补零、
  rank 均分、填充区间计算；非法声明（重名、空名、空形状、零维、非法宽度/
  对齐/rank 数）拒绝。
- **正常读写**：全参数与矩形切片（含列切片——逻辑行在缓冲中不连续）的
  gather/scatter 往返；整数/负下标；缺省维度；扁平与嵌套值形式等价；
  int/uint/float 多宽度类型。
- **跨 rank**：参数跨 rank 边界、单元素跨 rank 边界的读写，且两侧邻居不受影响。
- **边界与拒绝**：越界整数/负下标、越界 slice 起止、start>stop、非单位步长、
  维度过多、未知/冻结参数名、缓冲数量或长度错误、只读缓冲 scatter、
  更新值数量不匹配。
- **填充保护**：任意 scatter 后所有 `padding_spans()` 区间保持全零；
  相邻参数字节不变。
- **rank 数无关性**：同一组逻辑更新（含整参数、矩形切片、单点、交错序列）
  在 num_ranks = 1/2/3/5/8 下重建出的每个参数逐元素一致，且填充保持为零。
