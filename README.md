# rs_shamir — GF(65537) 阈值秘密重建库

有限域 GF(65537) 上的逐字节 Shamir 分享 + Reed-Solomon 纠错重建。
每个字节由一条次数 `< t` 的独立多项式共享，常数项即该字节；重建端按
Berlekamp-Welch 译码容忍调用方声明的至多 `e` 个错误份额位置。

## 数据类型与 API（`rs_shamir/`）

```python
from rs_shamir import (
    BatchDescriptor, Share, RecoveryResult,
    generate_shares, make_descriptor, recover,
    PRIME, MAX_SHARES, MAX_LENGTH, OK, INSUFFICIENT, INCONSISTENT,
)
```

- `BatchDescriptor(batch_id, threshold, x_coords, length, sha256, prime=65537)`
  —— 可信批次描述：素数、阈值 t、全部预期横坐标、原始字节长度、原文
  SHA-256（十六进制）。
- `Share(index, batch_id, x, ys)` —— 一条份额：份额编号、批次 ID、横坐标、
  各列纵坐标（长度须等于 `descriptor.length`，值域 `[0, 65536]`）。
- `recover(descriptor, shares, max_errors) -> RecoveryResult`
  —— 重建入口。`max_errors` 为声明的错误份额位置上限 e。
- `generate_shares(secret, threshold, x_coords, batch_id, seed)` /
  `make_descriptor(...)` —— 确定性本地夹具生成（`random.Random(seed)`），
  用于演练与测试。

`RecoveryResult` 字段：

| 字段 | 含义 |
| --- | --- |
| `status` | `ok` / `insufficient_evidence` / `inconsistent` |
| `reason` | 机器可判读的原因码（见下） |
| `secret` | 仅 `status == ok` 时为恢复的原文 `bytes`，否则恒为 `None`（绝不返回部分秘密） |
| `error_positions` | 成功时被纠正的份额编号 |
| `diagnostics` | 失败时相关的**份额编号**，不含任何份额值或秘密材料 |

### 判定流程与原因码

1. 描述校验（素数=65537、t≥1、长度 1..4096、横坐标 ≤31 且互异、摘要格式）
   → `invalid_descriptor`；份额数 >31 → `too_many_shares`。
2. 份额格式（坐标/纵坐标值域、列数一致）→ `invalid_share_format`。
3. 批次混入显式拒绝，不靠插值忽略：`batch_id_mismatch`、
   `unknown_x_coordinate`。
4. 重复横坐标：同值去重；异值 → `conflicting_shares`（诊断列双方编号）。
5. 去重后不同横坐标数 `< t+2e` → `insufficient_evidence /
   insufficient_distinct_coordinates`，不进入纠错重建。
6. 逐列 RS 译码；在声明 e 内无可行重建 → `insufficient_evidence /
   no_reconstruction_within_error_bound`。
7. 所有列共同涉及的错误份额位置并集 `> e` → `error_budget_exceeded`。
8. 常数项超出字节范围（>255）→ `constant_term_out_of_byte_range`。
9. 原文 SHA-256 与描述不一致 → `digest_mismatch`。
10. 全部通过 → `ok`，返回完整原文。

## 本地核验

```bash
.venv/bin/python tools/local_verify.py --list          # 列出用例
.venv/bin/python tools/local_verify.py --case C01      # 运行单组，输出 JSON
echo "exit=$?"
```

每组输出一条 JSON：`case`、实际断言数、`passed/failed/skipped`、样本种子
与逐条期望/观察摘要（只含摘要值与份额编号，不含秘密或份额值）。任一断言
失败、零断言或整组跳过均以非零退出。

### 可重放样本（种子固定，输出确定）

| 用例 | 种子 | 规模 | 覆盖点 |
| --- | --- | --- | --- |
| C01 | 20261119 | t=3, e=2, 7 份, 16 字节 | 65537 域值域、t+2e 门限（少 1 份即拒绝）、字节级正确恢复 |
| C02 | 20261202 | t=2, e=1, 5 份, 8 字节 | 缺失 1/2 份、同值重复去重、异值冲突及诊断编号、批次 ID 混入、外来横坐标 |
| C03 | 20261203 | t=3, e=2, 7 份, 24 字节 | 恰 e 个错误可纠正并定位、e+1 个被拒、跨列错误位置并集 > e、常数项 300 拒收、摘要不符拒收 |
| C04 | 20261204 | t=15, e=8, 31 份, 4096 字节 | 证据不足/不一致分类、失败无部分秘密、诊断仅份额编号、最大声明规模、31 份上限 |

## 未覆盖边界（保留未知）

- 横坐标 `x = 0` 未被禁止（数学会直接暴露常数项）；描述方应避免使用。
- 描述对象本身的真实性（批次描述的来源认证、防篡改）不在库职责内。
- 实现非常数时间，未考虑时序侧信道。
- 错误值为主动构造的任意值时，RS 唯一译码保证 ≤ e 错误下结果唯一；
  超出 e 的行为按“证据不足/不一致”处理，但不保证识别出全部作恶份额。
- 份额编号由调用方提供，库不校验其全局唯一性（仅按值透传诊断）。
- 未做并行/性能强化；最大规模（31 份 × 4096 字节）单次恢复约秒级。
