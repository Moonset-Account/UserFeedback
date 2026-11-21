# 同步训练步协调器（StepCoordinator）

纯本地基础设施组件：汇总同一步内各 rank 上报的微批贡献，按全局有效
Token 数加权归一，执行 `theta -= lr * normalized_gradient`。

无第三方依赖，仅使用标准库（需要 Python 3.10+，因为使用了 `X | None`
类型标注与 `dataclass(slots=True)`）；不涉及网络、持久化、序列化格式或
优化器状态。

## 文件

| 路径 | 说明 |
| --- | --- |
| `stepcoord/coordinator.py` | 协调器实现与数据类型 |
| `stepcoord/__init__.py` | 公开接口导出 |
| `tests/test_coordinator.py` | 纯合成数据的 `unittest` 测试 |

## 语义约定

### 上报与身份

* `GradientReport(step, mb_id, rank, valid_tokens, gradient, overflow)`：
  `gradient` 是该微批**有效 Token 上已求过平均**的梯度向量，长度必须等于
  参数维度、各分量有限。`valid_tokens` 为 0 或 `overflow=True` 的微批没有
  数值贡献，这类上报可以省略梯度，也可以携带溢出产生的 `inf`/`nan` ——
  此类梯度既不校验、也不参与重传比较。
* 名单（`MicroBatch(mb_id, rank)` 元组）在构造时固定：`mb_id` 唯一，且每个
  微批只由指定 rank 上报。名单里的每个微批都必须上报（零 Token 的微批也要
  报到，只是不贡献数值），否则该步无法结算。未知 `mb_id`、rank 不匹配、
  步号超前都抛异常且不改变状态。梯度长度不符同样抛 `ValueError` 且不改变
  状态。

### 重传与旧回执

* 结算前，同一微批的**相同重传**返回 `DUPLICATE`，只计一次。
* 同一微批结算前出现**不同载荷**返回 `CONFLICT`，该步标记为失败，之后
  该步的上报返回 `STEP_FAILED`。
* 载荷比较的规范化规则：零 Token 或 overflow 的微批不参与数值计算，其
  梯度也不参与身份比较；其余情况比较 `(valid_tokens, overflow, gradient)`，
  梯度按浮点**精确相等**比较。
* 某步结算后，所有 `step` 小于当前步的回执返回 `STALE`，不改变参数、
  学习率位置或历史记录。

### 结算

`settle()` 结算当前步，判定顺序固定：

1. 若出现过冲突重传 → `FAILED_CONFLICT`，不更新（冲突是终局判定，不必等
   名单收齐）；
2. 名单未收齐 → 抛 `IncompleteStepError`，状态完全不变，可继续接收补齐后
   重试；
3. 名单收齐后，若任一条上报 `overflow` → `SKIPPED_OVERFLOW`，不更新；
4. 名单收齐且整步有效 Token 为 0 → `SKIPPED_NO_TOKENS`，不更新；
5. 否则 `APPLIED`：权重 `w_i = tokens_i / sum(tokens)`，零 Token 微批权重
   为 0、梯度被忽略，参数更新为
   `theta - lr[position] * sum_i w_i * g_i`，然后 `position += 1`。

第 1、3、4 种情况都推进**步号**（`current_step`），但不推进**学习率位置**
（`lr_position`）。`lr_schedule` 可以是序列，也可以是接收位置并返回学习率
的回调；序列耗尽或返回非有限值时抛 `LearningRateScheduleError`，该步保持
未结算状态（步号、参数、学习率位置均不变，可配置后重试）。

若参数更新结果出现非有限值，抛 `CoordinatorError`，同样保持该步未结算。

### 可验证示例：1 个 Token 与 9 个 Token

```python
from stepcoord import StepCoordinator, MicroBatch, GradientReport

coord = StepCoordinator(
    parameters=(0.0, 0.0),
    roster=(MicroBatch("a", 0), MicroBatch("b", 1)),
    lr_schedule=(2.0,),
)
coord.submit(GradientReport(0, "a", 0, valid_tokens=1, gradient=(1.0, 0.0)))
coord.submit(GradientReport(0, "b", 1, valid_tokens=9, gradient=(0.0, 1.0)))
result = coord.settle()
```

期望更新后参数为 `(-0.2, -1.8)`（测试中按浮点近似比较）：`g_a` 只落在第 0 维、
`g_b` 只落在第 1 维，因此从更新后参数可直接反解权重
`|Δθ_i| / (lr · |g_i|) = (0.1, 0.9)`，与 Token 比例 1:9 一致。
`result.contributions` 同时给出每个微批的 `weight`（跳过/失败时为 `None`）。

`StepResult` 是不可变快照，包含 `step`、`status`、`total_valid_tokens`、
`lr_position`、`learning_rate`、`parameters`、`contributions`、`reason`。
`contributions` 按名单顺序列出**实际收到**的微批（失败/提前结算时可能不完整），
每项带 `weight`（未成功更新时为 `None`）。
`coordinator.history` 保留全部已结算步；`coordinator.pending_micro_batches`
给出当前步尚未收到的微批身份。

`submit` 的返回值：`ACCEPTED`、`DUPLICATE`、`CONFLICT`、`STEP_FAILED`、
`STALE`。协调器内部加锁，可从多个线程投递上报，但 `settle` 应由单一方
（训练循环）调用。

## 测试状态

**本次交付未运行任何测试、未执行任何命令或构建。** 上述期望值来自静态
推导，不构成“验证通过”的声明。

在本地自行运行：

```sh
python -m unittest discover -s tests -t . -v
```

测试全部使用本地合成数据，不联网、不读写外部文件、不依赖第三方库。
