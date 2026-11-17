# GF(65537) 阈值秘密重建库（含错误份额纠正）

份额缺失或被错误抄写场景下的阈值重建实现。每个字节由次数小于 `t` 的独立
多项式共享，常数项即该字节；重建阶段调用方声明最多 `e` 个错误份额位置，
使用 Berlekamp–Welch 在 GF(65537) 上完成纠错重建，并对错误位置、字节范围
和原文 SHA-256 做强制联合校验。任一校验失败都只返回分类结果，不返回部分秘密。

## 目录

```
secret_recovery/
  __init__.py        公共 API 导出
  recovery.py        域运算、高斯消元、Berlekamp–Welch、重建与校验
tools/
  local_verify.py    本地核验入口（--list / --case C01..C04|all）
```

## 公共 API

```python
from secret_recovery import (
    FIELD_PRIME,          # 65537
    MAX_SHARES,           # 31
    MAX_BYTES,            # 4096
    BatchDescriptor,      # 可信批次描述（frozen dataclass）
    Share,                # 输入份额（frozen dataclass）
    RecoveryStatus,       # OK / INSUFFICIENT_EVIDENCE / INCONSISTENT
    InconsistencyReason,  # 机器可读原因码
    RecoveryResult,       # status/reason/secret/diagnostics/...
    recover,              # 重建入口
)
```

### BatchDescriptor

| 字段 | 含义 |
| --- | --- |
| `batch_id` | 批次标识，份额必须携带同一批次 ID |
| `prime` | 固定为 `65537`，其他值在构造期抛 `ValueError` |
| `threshold` | 门限 `t`，每列多项式次数小于 `t`（`t >= 1`） |
| `share_xs` | 各份额编号对应的横坐标，互不相同且在 `1..p-1`；长度 1..31 |
| `original_length` | 原始字节长度，1..4096 |
| `sha256` | 原文摘要，接受 32 字节或 64 位十六进制字符串 |

### Share

| 字段 | 含义 |
| --- | --- |
| `batch_id` | 份额声称所属批次；与描述不符即 `BATCH_MISMATCH` |
| `number` | 份额编号，即 `share_xs` 的下标；越界即 `UNKNOWN_SHARE_POSITION` |
| `values` | 各列纵坐标（长度等于 `original_length`，元素在 `0..p-1`）；`None` 表示整份缺失，列表内单个 `None` 表示该列缺失 |

### `recover(descriptor, shares, max_errors) -> RecoveryResult`

`max_errors` 即调用方声明的 `e`（非负整数）。处理顺序：

1. **结构/批次校验**：份额编号必须在描述范围内；批次 ID 不一致立即判
   `INCONSISTENT / BATCH_MISMATCH`，批次混入不能靠插值忽略。
2. **重复坐标处理**：同一编号的多份副本，纵坐标完全相同则去重（记入
   `deduplicated`）；任何一列不同即 `DUPLICATE_CONFLICT`，诊断只列该编号。
3. **门限**：剔除缺失项后，不同横坐标数（以及每列可用点数）必须
   `>= t + 2e`，否则返回 `INSUFFICIENT_EVIDENCE`，`secret=None`。
4. **逐列 Berlekamp–Welch**：构造 `Q(x) = P(x)E(x)` 的线性方程组
   （`deg P < t`，`E` 为首一 `e` 次错误定位多项式），GF(p) 高斯-约当消元
   求解后做多项式带余除法；除不尽、定位多项式退化或次数越界均判
   `DECODE_FAILURE`。
5. **逐列复核**：用恢复多项式重算所有已提供点，错误点必须恰好满足
   `E(x)=0`；单列错误数超过 `e` 判 `TOO_MANY_ERRORS`。
6. **联合错误位置**：所有列错误份额编号取并集，超过 `e` 判
   `TOO_MANY_ERRORS`（满足“所有列共同涉及的错误份额位置不超过 e”）。
7. **字节常数**：每列常数项必须在 `0..255`，否则 `CONSTANT_NOT_BYTE`。
8. **摘要**：`sha256(恢复原文) == descriptor.sha256`，否则 `DIGEST_MISMATCH`。

失败路径上 `RecoveryResult.secret` 恒为 `None`；`diagnostics` 只包含份额
编号（整数），原因码与诊断分离，不携带任何秘密字节。

## 约束

- 域：素数 `p = 65537`，仅支持该域。
- 份额：至多 31 份，横坐标互不相同且为 `1..p-1`。
- 原文：1..4096 字节；每列一个次数小于 `t` 的独立多项式。
- 纠错：声明 `e` 时要求至少 `t + 2e` 个不同横坐标；`e=0` 走同一套
  Berlekamp–Welch 方程（等价于纯插值），后续校验不放松。

## 核验命令

使用工作区内准备好的解释器，以隔离模式运行：

```bash
.venv/bin/python -I tools/local_verify.py --list
.venv/bin/python -I tools/local_verify.py --case C01
.venv/bin/python -I tools/local_verify.py --case C02
.venv/bin/python -I tools/local_verify.py --case C03
.venv/bin/python -I tools/local_verify.py --case C04
.venv/bin/python -I tools/local_verify.py --case all
```

退出码：`0` 全部断言通过；`1` 存在失败断言；`3` 零断言或整组跳过；
参数错误由 argparse 返回 `2`。输出为 JSON，含 `case`、固定种子、
实际断言计数（`total/passed/failed/skipped`）、失败标签与观察摘要；
不输出原始样本字节、摘要之外的秘密内容或任何凭据。

## 可重放样本

核验脚本自带确定性脚手架（固定种子 LCG，无外部服务、无随机熵依赖）：

| 用例 | 种子 | 规模 | 覆盖要点 |
| --- | --- | --- | --- |
| C01 | 2026111801 | t=3, n=7, e=2, 16B | 65537 域、`t+2e` 门限边界（含少一份的证据不足）、字节精确恢复 |
| C02 | 2026111802 | t=3, n=8, e=1, 20B | 整份/单列缺失、同值去重、异值冲突、批次混入、未知编号 |
| C03 | 2026111803 | t=4, n=10, e=2, 32B | 共同错误位置定位、e 上限、常数 256 越界、错误 SHA-256 |
| C04 | 2026111804 | t=5, n=31, e=1, 4096B | 最大声明规模、证据不足、超能力不一致、规模上限拒绝 |

## 未覆盖边界（审计须知）

- 核验只覆盖固定种子的合成数据与本地桩，不接入真实批次、真实密钥或云服务；
  包装器是运行入口，不是独立合格证明。
- 未做侧信道/计时防护：实现为教学/演练用途的常量数据结构，未对输入规模做
  常数时间保证（C04 在本机最大规模约 1.2 秒，仅作性能观察）。
- Berlekamp–Welch 采用稠密矩阵高斯消元，复杂度约 O(n²·(t+2e))，面向
  n ≤ 31 的约束设计，未针对更大参数集优化。
- 份额编号与横坐标的绑定关系完全信任 `BatchDescriptor`；描述本身的真实性、
  摘要分发渠道不在本库威胁模型内。
- 未实现份额生成/分发侧逻辑（脚手架中的生成器仅服务于本地核验）。
- 单列为 `None` 的缺失语义、同编号重复副本语义属于本库约定，调用方接入前
  应核对与上游数据格式是否一致。
