# 检查点分片恢复（本地基础设施）

把旧作业按行（或按列）切分的二维权重分片，恢复成新作业可能沿另一维切分的
目标 rank 分片。权重与逐元素优化器状态（与权重同形状的 rank-2 张量）走同一
套恢复路径。本项目只包含本地文件基础设施：不联网、不发起外部请求、不包含任何
漏洞利用或目标探测代码。

## 目录结构

```
shard_recovery/
  geometry.py     # 逻辑矩形 Rect 与精确一次覆盖校验（缺块/重叠）
  shard.py        # 分片二进制格式、可定位读取、字节区间审计、分片写入
  checkpoint.py   # 清单（manifest）读写与本地合成检查点构建
  recovery.py     # 恢复接口 recover_rank / 发布接口 publish_recovery
  errors.py       # 异常类型
tests/
  test_recovery.py
README.md
```

仅依赖 Python 3 标准库（`struct`、`json`、`os`、`dataclasses`、`unittest`）。

## 分片文件格式（版本 1）

每个分片文件描述一个 rank-2 张量中的一个**逻辑矩形**，全部数值为按行存放
（row-major）的小端 32 位浮点（`<f`）。

固定文件头 38 字节，小端、无对齐填充，字段依次为：

| 偏移 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| 0 | magic | `4s`，固定 `CKSH` | 格式魔数 |
| 4 | version | `H` | 格式版本，当前为 1 |
| 6 | rows | `I` | 物理行数 |
| 10 | cols | `I` | 物理列数 |
| 14 | rect.row0 | `I` | 逻辑矩形起始行 |
| 18 | rect.col0 | `I` | 逻辑矩形起始列 |
| 22 | stride_floats | `I` | 物理行步长（以 float 计） |
| 26 | valid_rows | `I` | 有效行数 |
| 30 | valid_cols | `I` | 有效列数 |
| 34 | end_padding | `I` | 文件末尾对齐填充字节数 |

逻辑矩形为半开区间
`rows[rect.row0 : rect.row0+valid_rows] × cols[rect.col0 : rect.col0+valid_cols]`。

数据区布局：

```
valid_rows 个物理行，每行：
    valid_cols 个小端 float32（有效元素）
    (stride_floats - valid_cols) * 4 字节行尾填充（零）
最后一个物理行之后：
    end_padding 字节文件尾对齐填充（零）
```

文件总长度恒为 `38 + valid_rows * stride_floats * 4 + end_padding`，读取端会
用该等式校验实际文件大小。

## 清单（manifest）

每个检查点目录有一个 `manifest.json`，通过相对路径使每个分片**可定位**，并在
清单里冗余记录逻辑矩形，使覆盖性判断可以在打开任何分片文件之前完成：

```json
{
  "format_version": 1,
  "dtype": "float32-le",
  "tensors": {
    "weight": {
      "rows": 6, "cols": 8,
      "shards": [
        {"path": "weight.shard0.cksh", "rect": [0, 0, 3, 8],
         "stride_floats": 10, "end_padding": 0}
      ]
    }
  }
}
```

## 恢复语义

入口：`recover_rank(manifest_path, target_rank, target_rect)`。

1. **覆盖校验**：取与目标矩形相交的旧分片，做水平带扫描，证明这些分片把目标
   恰好覆盖一次。`geometry.cover_rect` 同时支持行分片、列分片及任意混合布局，
   因为判断只基于逻辑矩形而不假设切分轴。
2. **形状/元数据一致性**：打开相交分片，核对文件头中的逻辑矩形、物理形状、
   行步长与填充是否与清单一致，并按文件头推算的文件大小核对实际大小。
3. **只读取相交的有效字节**：每个物理行只 `seek` 到与目标相交的列段并读取
   `有效列数 * 4` 字节；行尾填充用 `seek` 跳过，文件尾填充永远不会被触及。
   每次读取都记录半开字节区间 `[start, end)`，最终按分片汇总在
   `TensorRecovery.accessed`（`ShardAccess`）中，可逐字节核对。
4. **逐元素状态**：清单中的每个张量（权重和每个同形状优化器状态）独立走同一
   套校验与拼接；任一张量失败则整个 rank 放弃发布。
5. **失败不发布**：失败时 `RecoveryResult.ok is False`，`tensors` 为空，
   `errors` 给出结构化错误；`publish_recovery` 对失败结果直接抛
   `RecoveryPublishError` 且不创建输出目录。

### 错误类型与逻辑范围

`RecoveryError.kind` 取值：

| kind | 触发条件 | `ranges` 含义 |
| --- | --- | --- |
| `missing` | 目标内存在未被任何旧分片覆盖的区域 | 缺块的逻辑矩形（可能多段） |
| `overlap` | 两个旧分片在目标内相交 | 逐带的相交逻辑矩形 |
| `shape_mismatch` | 清单与文件头的矩形/形状/步长/填充不一致，或目标越界 | 受影响分片的逻辑矩形 |
| `format` | 文件打不开、魔数/版本错误、文件截断、短读等 | 该分片本应贡献的逻辑矩形 |

## 主场景：两份带行尾填充的行分片 → 一份窄列分片

`tests/test_recovery.py::RowShardsToColumnShardTest` 构造 6×8 张量，切成两份
行分片（行 0–2 与行 3–5），两份都设置 `stride_floats=10`（每行 8 个有效
float + 2 个 float 的行尾填充），第二份额外带 6 字节文件尾填充。目标 rank 的
逻辑矩形为全高、列 `[2,5)` 的窄列分片。

可核对项（测试中均为硬编码期望值）：

- 恢复值逐元素等于原矩阵 `rows[0:6, 2:5]`，权重与优化器状态分别核对。
- 每个分片的访问区间精确为三个 12 字节段：
  `ByteRange(38 + r*40 + 8, 38 + r*40 + 20)`（`r=0,1,2`），其中 40 为物理
  行步长字节数、8 为列偏移 `(2-0)*4`、20 为 `8+3*4`。
- 所有访问区间都落在物理行的有效 float 段内，不跨越行尾填充；第二份分片
  的访问终点距文件尾恰好是 `20` 字节行尾填充 + `6` 字节文件尾填充。
- 发布后的新分片可重新打开读取，矩形为 `[0,0,6,3]`，数值与源矩阵切片一致。

其余测试覆盖：缺块（报告 `Rect(2,2,4,4)`）、重叠（报告
`Rect(2,0,3,4)`）、清单/文件头矩形不一致、文件截断、列分片恢复成窄行分片、
几何单元测试与带填充分片的部分读取/魔数损坏测试。

## 运行测试

在仓库根目录（`shard_recovery/` 与 `tests/` 的共同父目录）执行：

```
python -m unittest discover -s tests -v
```

无第三方依赖，标准库即可运行。

> **未运行声明：** 依据本次任务约束，本环境只编辑源码、测试与 README，
> **未执行任何代码、命令、构建或测试**。以上测试为标准库 `unittest` 用例，
> 全部使用本地合成数据，未做实际运行验证；测试通过与否需在允许运行的环境中
> 另行确认，本仓库不宣称测试已验证通过。

## API 速览

```python
from shard_recovery import (
    Rect, ShardPlan, TensorSource, build_checkpoint,
    recover_rank, publish_recovery,
)

# 构建/读取旧检查点（测试基础设施）
build_checkpoint("old/", {
    "weight": TensorSource(rows, cols, matrix, [
        ShardPlan(rect=Rect(0, 0, 3, cols), stride_floats=10),
        ShardPlan(rect=Rect(3, 0, rows, cols), stride_floats=10, end_padding=6),
    ]),
})

# 恢复目标 rank 的逻辑矩形（权重 + 每个逐元素优化器状态）
result = recover_rank("old/manifest.json", target_rank=7,
                      target_rect=Rect(0, 2, 6, 5))
if result.ok:
    result.tensor("weight").values          # 行主序浮点元组
    result.tensor("weight").accessed        # 每个旧分片的访问字节区间
    publish_recovery(result, "new_rank/")   # 成功才会写盘
else:
    for err in result.errors:               # kind + 逻辑范围 + 说明
        ...
```
