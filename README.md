# 混合精度训练协调模块

用纯标准库实现的混合精度训练协调器（Python 3.11.6）。参数与动量以小向量
（`list[float]`）表示，优化器为 SGD + 动量，带动态损失缩放。

## 语义

- **步开始固定**：`begin_step` 固定本步的 rank 集合、微批数与损失缩放值，
  并分配单调递增的 epoch。
- **分批回传**：各 rank 通过 `report_grad` 上报*已缩放*梯度与非有限标志；
  非有限标志与梯度值本身（inf/nan）双重校验。
- **恰好一次收尾**：收齐后只执行一次全局反缩放（`sum(g)/(scale×微批总数)`）、
  合并与优化器推进；收齐即封闭，同 epoch 的额外报告一律拒绝。
- **溢出步**：任一有效微批非有限 → 整步跳过，保留参数与动量，训练步号不
  推进，缩放乘以 `scale_backoff`（不低于 `min_scale`），连续成功计数清零。
- **缩放上调**：连续成功达到 `growth_interval` 才乘以 `scale_growth`
  （不超过 `max_scale`），并重置计数。
- **成员退出撤销**：`abort_step` 撤销整步，保留原缩放与连续成功计数——
  通信中断不被当作梯度溢出。重试用新 epoch，旧 epoch 的迟到报告被
  `StaleReportError` / `NoActiveStepError` 拒绝，不会进入重试步。
- **崩溃恢复**：汇总完成后、优化器发布前先落 `journal.json`（含目标步号）；
  重启时若持久态未达目标步号则幂等重放优化器推进，保证恰好推进一次。

## 公开 API

```python
from mixed_precision_coordinator import (
    MixedPrecisionCoordinator, CoordinatorConfig,
    CoordinatorError, NoActiveStepError, StepAlreadyActiveError,
    StaleReportError, DuplicateReportError, UnknownRankError,
    BadGradientError, NotInitializedError, AlreadyInitializedError,
)
```

| 成员 | 说明 |
| --- | --- |
| `MixedPrecisionCoordinator(config=None, state_dir=".", crash_hook=None)` | 构造；`state_dir` 已有状态时自动恢复并重放未提交的 journal。`crash_hook` 仅供测试，在“journal 落盘后、优化器发布前”调用。 |
| `CoordinatorConfig` | `learning_rate`、`momentum_beta`、`initial_scale`、`growth_interval`、`scale_backoff`、`scale_growth`、`min_scale`、`max_scale` |
| `initialize(params)` | 设置初始参数、动量清零；已初始化（含恢复）时抛 `AlreadyInitializedError` |
| `begin_step(step_id, ranks, num_microbatches) -> epoch` | 开始全局步并返回本步 epoch |
| `report_grad(epoch, rank, microbatch_index, scaled_grad, finite=True) -> str` | 返回 `"collecting"` / `"committed"` / `"overflow_skipped"` |
| `abort_step(epoch)` | 撤销当前步（成员退出等），保留缩放与连续成功计数 |
| 属性 `params` / `momentum` / `train_step` / `scale` / `success_streak` / `current_epoch` / `has_active_step` / `pending_reports` | 只读状态 |

持久化文件（`state_dir` 内）：`state.json`（参数、动量、训练步号、缩放、
连续成功计数、epoch）、`journal.json`（汇总完成但未发布的恢复点）。

## 复现步骤

```bash
cd <工作区根目录>
python3 --version          # 3.11.6
python3 -m unittest discover -s tests -v
```

## 验证范围（tests/test_coordinator.py，27 个用例，全部真实执行）

- **正常步**：手算核对的参数/动量/步号（`test_single_step_values`、
  `test_momentum_accumulates_across_steps`）；连续成功达阈值才上调缩放；
  收齐后推进恰好一次。
- **溢出步**：非有限标志、inf、nan 三种触发；参数动量保留、步号不推进、
  缩放下调、计数清零；缩放上下限钳制。
- **掉线后重试**：撤销保留缩放与计数；旧 epoch 迟到报告被拒绝且不污染
  重试步；缩放序列 `[4, 4, 8, 4]` 证明通信中断未被误当作梯度溢出。
- **崩溃恢复**：在汇总完成与优化器发布之间注入崩溃，恢复后结果与无崩溃
  参照运行逐位一致；重放幂等（多次加载不重复推进）；收集中途崩溃不留
  半成品步，可用新 epoch 重试；溢出结果跨重启持久。
- **边界校验**：重复上报、未知 rank、微批越界、维度不符、无活动步、
  活动步未结束时开新步、重复初始化、空 rank 集合。

未执行的测试：无（以上均为真实执行结果）。
