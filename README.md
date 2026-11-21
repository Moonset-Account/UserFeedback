# 灰度闸门（Canary Gate）

按约定的地区权重比较新版与基线错误率，对发布进行裁决：回滚、放量或继续等待。

## 问题模型

- 发布开始时**冻结**每个地区在新版组和基线组各有哪些请求（两组均非空、互不重叠，
  请求 ID 全局唯一）。
- 每个地区有权重的**有理数**权重，各权重为正且总和精确为 1；阈值也是有理数。
- 每条请求最终回报成功或失败；未回报的请求两种结果都有可能。
- 每收到一批回执，计算各地区 `新版错误率 − 基线错误率` 的加权总和 `S`
  在所有未回报请求的所有可能取值下的**下界 lower** 与**上界 upper**：
  - lower：新版未回报全部成功、基线未回报全部失败；
  - upper：新版未回报全部失败、基线未回报全部成功。
- 裁决：
  - `lower > 阈值` → **回滚**（记录触发批次与界值 lower）；
  - `upper <= 阈值` → **放量**（记录触发批次与界值 upper）；
  - 否则继续等待。
- 首次可判时记录触发的回执批次号与界值；重复回执只计一次（冲突时以首次为准）。
- 某个地区回报较慢时，其余地区的结果可能已把界值推过阈值，从而在全部回齐前得出结论。

所有算术基于 `fractions.Fraction`，无浮点误差。

## 公开 API（`canary_gate.py`）

| 名称 | 说明 |
| --- | --- |
| `CanaryGate(groups, weights, threshold)` | 构造闸门。`groups`: 地区名 → `RegionGroups` 或 `(新版ID列表, 基线ID列表)`；`weights`: 地区名 → 正有理数（键集合须与地区一致，总和为 1）；`threshold`: 有理数。权重/阈值接受 `Fraction`、`int`、有限小数字符串等可精确转换的值。 |
| `gate.submit_batch(receipts) -> GateReport` | 提交一批回执（`Receipt` 或 `(request_id, success)` 元组），批次号从 1 递增；返回处理后的报告。重复回执只计一次；未知请求抛 `UnknownRequestError`（整批作废、不占批次号）；终局后再提交抛 `GateClosedError`。 |
| `gate.bounds() -> (Fraction, Fraction)` | 当前 `(lower, upper)`，不推进批次。 |
| `gate.final_report` | 首次可判时记录的终局 `GateReport`，未决时为 `None`。 |
| `gate.threshold` / `gate.weights` | 构造时解析后的阈值与权重。 |
| `GateReport` | 字段：`decision`、`batch_index`、`lower`、`upper`、`triggered_bound`（未决为 `None`）。 |
| `Decision` | 枚举：`PENDING` / `ROLLBACK` / `RAMP_UP`。 |
| `Receipt` | `request_id: str`、`success: bool`。 |
| `RegionGroups(new, baseline)` | 冻结分组；校验两组非空且不重叠。 |
| `GateClosedError` / `UnknownRequestError` | 异常类型。 |

## 复现步骤

环境：Python 3.11.6（仅标准库，无第三方依赖）。

```bash
cd <工作区根目录>
python3 -m unittest discover -v
```

## 验证范围

测试位于 `tests/test_canary_gate.py`，共 18 个用例，全部基于真实功能执行：

- **正常场景**
  - 高权重的慢流量地区全败、基线全胜，另一地区完全未回报时提前回滚，
    并校验记录的批次号与界值（`TestEarlyRollbackSlowRegion`）。
  - 尚有请求未回报时上界已不超过阈值，提前放量（`TestEarlyRampUp`）。
  - `RegionGroups` 数据类与元组两种分组写法（`TestValidation`）。
- **边界场景**
  - 上界恰好等于阈值 → 放量；上界略高于阈值 → 继续等待（`TestRampUpBoundary`）。
  - 下界恰好等于阈值 → 不算"高于"，不触发回滚（`TestLowerEqualThresholdNotRollback`）。
  - 1/3、2/3 权重下界值为精确 `Fraction`（`TestExactRationalArithmetic`）。
  - 未知请求整批拒绝且状态、批次号不变（`TestUnknownRequest`）。
  - 权重和不为 1、权重非正、权重键与地区不匹配、空组、组内重叠、
    请求跨地区重复冻结均被拒绝（`TestValidation`）。
- **交错场景**
  - 同批/跨批重复回执只计一次，结果冲突时以首次为准（`TestDuplicateReceipts`）。
  - 空批次占批次号、重复与新回执混杂时批次号按提交递增，
    首次可判批次被正确记录（`TestInterleavedBatches`）。
  - 终局后再提交抛 `GateClosedError`，终局报告不被改写
    （`TestEarlyRollbackSlowRegion.test_gate_closed_after_decision`）。

最近一次执行结果：`Ran 18 tests ... OK`（全部通过，无跳过）。
