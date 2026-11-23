# 检查点权重平均器（可复核发布）

为重试/分叉的训练运行发布可复核的权重平均版本。检查点目录中的每个检查点
用若干有理数（`fractions.Fraction`，精确运算）表示参数，携带父节点列表、
逻辑步与内容摘要（SHA-256 截断）。同一逻辑步可登记多个尝试，不同分支可经
多父节点检查点再合并。

- 运行环境：Python 3.11.6（仅标准库，无第三方依赖）。
- 主模块：`checkpoint_averager.py`
- 测试：`tests/test_checkpoint_averager.py`（unittest，27 例）

## 公开 API

### 数据与目录

- `Checkpoint.create(checkpoint_id, step, parents=(), params=())`
  构造检查点；`params` 接受 int/str/Fraction，统一转为 `Fraction`；
  内容摘要 `digest` 由 `compute_digest(step, parents, params)` 自动计算。
- `compute_digest(step, parents, params) -> str`
  对规范 JSON 序列化做 SHA-256 并取前 16 位十六进制。
- `CheckpointDirectory()`
  - `register(checkpoint) -> int`：登记并返回新修订号（从 1 递增）。
    登记顺序任意，支持**迟到补登记**（后登记更早逻辑步的尝试）；
    重复 id 或重复父节点抛 `ValueError`。
  - `revision`：当前修订号。
  - `get(checkpoint_id, revision=None)` / `at_step(step, revision=None)`：
    查询；传入 `revision` 即**固定旧修订回读**，只看见该修订（含）之前
    登记、且当时未被替换的记录。
  - `is_reachable(ancestor_id, descendant_id, revision=None)`：父链可达性。
  - `lineage_path(ancestor_id, descendant_id, revision=None)`：返回一条
    确定性父链路径（含两端），不可达返回 `None`。

### 发布清单与评估

- `StepRequirement(step, allowed_ancestors=None, fixed=None)`
  清单中一个逻辑步的要求：`allowed_ancestors` 限定所选检查点必须落在的
  祖先范围（是其中至少一个检查点的后代或其本身）；`fixed` 固定选择某个
  尝试（检查点 id）。
- `Manifest(requirements, window, alpha)`
  `requirements` 给出需要的逻辑步顺序；`window` 为指数平均窗口 W（超过
  步数时按全部步计）；`alpha` 为清单给定的有理数系数（须在 [0, 1]）。
- `evaluate(directory, manifest, revision=None) -> PublishResult`
  在指定修订（默认最新）下评估。对血缘上前后可达的所有合法选择做链式
  动态规划，按最近 W 步计算**顺序相关**的指数平均：

  ```
  s_0 = x_first            # 窗口首项作为初值
  s_t = alpha * x_t + (1 - alpha) * s_{t-1}
  ```

  返回 `PublishResult`：
  - `dimensions`：每个参数维度的 `DimensionResult(minimum, maximum,
    min_witness, max_witness)`，最小/最大各附一条**完整见证路径**
    （`Witness.chosen` 为每个清单步选中的检查点，`Witness.path` 为展开到
    根的完整血缘链，共享祖先只出现一次）。
  - `publishable`：只有每一维 `minimum == maximum`（值唯一）才为真。
  - `divergence`：不可发布时，从产生不同平均值的两条路径中找**首个分歧
    步**，给出 `step`、`position` 与竞争双方的 `(checkpoint_id, digest)`。
  - `window_steps` / `revision`：实际窗口覆盖的逻辑步与评估所用修订。
- `InfeasibleManifestError`：清单在当前修订下不存在合法选择（缺步、血缘
  断裂、固定尝试不存在等）时抛出。

## 复现步骤

```bash
cd <工作区根目录>
python3 --version          # 3.11.6
python3 -m unittest discover -s tests -v
```

## 验证范围（全部已真实执行，27 例全部通过）

- **正常路径**：唯一链可发布且各维值唯一；窗口首项作初值的顺序相关平均；
  固定尝试与允许祖先范围的过滤；多维参数各维独立求极值并各附见证。
- **边界**：W=1（取末项）、W 大于步数（截断）、alpha=0（保留窗口首项）、
  alpha=1（保留末项）、单步清单、缺步/血缘断裂的不可行清单、清单参数校验
  （空清单、重复步、W<1、alpha 越界）、重复登记/重复父节点拒绝、修订 0
  不可见任何记录。
- **交错（迟到补登记 × 固定旧修订回读）**：先登后步再补登前步；在固定修订
  下评估不可行，补登后最新修订可行而固定旧修订结果逐字段不变；迟到补登
  同步新尝试使最新结果从可发布变为不可发布，旧修订回读仍可复现。
- **两个分叉后再合并的小例（防拼接）**：
  - 例 1：A 支第 1 步有利（0）、B 支第 2 步有利（0），合并点 M。逐步最小
    值拼出的下界 5/2 不存在；合法路径 A、B 的平均都是 5，断言结果恰为 5
    且可发布。
  - 例 2：两合法路径平均分别为 3 与 7/2，拼接下界 2 不存在；断言最小值
    为 3（≠2）、不可发布，且分歧报告指向第 1 步，竞争摘要与目录中 B1/A1
    的 digest 一致；两条见证路径均血缘完整、共享祖先只算一次。

未执行项：无。
