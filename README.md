# 流水线分片收集与合并模块

把每行输入对状态的变换按**原输入顺序**叠加。连续行可以先归约为一个
复合变换；一次重试可能把旧分片区间拆成若干新片段。本模块负责：

1. **收集**分片区间、尝试编号、复合变换与执行器确认的重试替换关系；
2. **合并**出可执行的完整变换，以及实际使用的片段序列。

## 核心语义

- **顺序**：合并严格按原区间从前往后（区间 `lo` 升序）应用变换。分片
  可以乱序上报，到达顺序不影响结果；顺序颠倒会改变状态，测试中有
  非交换变换的专门断言。
- **替换启用条件（全有或全无）**：一次重试替换引用的全部新片段都已
  上报且成功，替换才启用；只要有一个片段失败，替换整体不启用。
- **可用回退**：替换不启用时，若旧分片自身的尝试成功，则回退为使用
  旧分片；同分片有多次尝试时，从最新尝试向更旧尝试逐级回退。
- **缺口等待**：片段尚未上报属于缺口，合并结果为 `waiting` 并在
  `missing` 中列出精确缺口区间（可由已上报邻片推断），此时不输出
  不完整的变换，继续等待缺失片段到齐后再次合并。
- **嵌套替换**：替换的目标片段自身可以再次发生替换，递归解析；任一
  层失败按该层的回退规则逐级吸收。
- **结构校验**：片段必须连续、无重叠且恰好铺满旧区间；重复片段引用、
  越界区间、循环替换引用均抛 `ValueError`。

## 目录结构

```
pipeline_collect_merge/
    __init__.py          # 公开 API 导出
    transforms.py        # 变换原语、compose 组合器、标签变换
    collect_merge.py     # Collector 与数据结构、合并算法
tests/
    test_collect_merge.py
smoke_demo.py            # 端到端冒烟演示
```

## 公开 API

| 名称 | 说明 |
| --- | --- |
| `Interval(lo, hi)` | 半开区间 `[lo, hi)`，非法区间（空/反向/负端点）构造即报错 |
| `ShardAttempt(shard_id, attempt, interval, transform, success)` | 一次分片尝试：编号、区间、复合变换、成功标志 |
| `Replacement(shard_id, attempt, segments)` | 执行器确认的替换关系，`segments` 为按区间顺序排列的 `(shard_id, attempt)` 引用列表 |
| `MergeResult` | 合并结果：`status` / `full_transform` / `used_segments` / `missing` / `gap` / `failures`，含 `ready` 属性 |
| `Collector()` | 收集器，方法见下 |
| `compose(transforms)` | 按从左到右顺序组合变换：`compose([f, g])(x) == g(f(x))` |

`Collector` 方法：

- `report_attempt(attempt: ShardAttempt) -> None`
  上报一次尝试。相同 `(shard_id, attempt)` 的幂等重放被忽略；区间或
  成功标志冲突时抛 `ValueError`。
- `report_replacement(replacement: Replacement) -> None`
  登记执行器确认的替换关系。旧尝试必须先上报；片段可延迟到齐，到齐
  时校验拼接结构。
- `merge() -> MergeResult`
  执行顺序合并，可在更多片段到齐后重复调用（收集器保留全部已收数据）。

`MergeResult.status` 取值：

- `ready`：区间连续覆盖，`full_transform` 可直接执行，`used_segments`
  为实际生效的片段序列；
- `waiting`：存在缺口/未到齐引用，`missing` 给出等待区间，变换为恒等
  占位；
- `failed`：结构完整但无任何成功尝试可用，`failures` 给出失败引用。

## 运行方法

要求 Python 3.11+，无第三方依赖，仅使用标准库。

```bash
# 运行全部单元测试
python3 -m unittest discover -s tests -v

# 端到端冒烟演示（乱序上报 -> 等待缺口 -> 补齐后启用替换）
python3 smoke_demo.py
```

## 最小示例

```python
from pipeline_collect_merge import (
    Collector, Interval, ShardAttempt, Replacement,
)
from pipeline_collect_merge.transforms import LabeledTransform

collector = Collector()
collector.report_attempt(
    ShardAttempt("A", 0, Interval(0, 6), LabeledTransform("A0"), True)
)
collector.report_replacement(
    Replacement("A", 0, [("A1", 1), ("A2", 1)])
)
collector.report_attempt(
    ShardAttempt("A1", 1, Interval(0, 3), LabeledTransform("a1"), True)
)
print(collector.merge().status)          # waiting（缺 [3,6)）

collector.report_attempt(
    ShardAttempt("A2", 1, Interval(3, 6), LabeledTransform("a2"), True)
)
result = collector.merge()
print(result.status)                     # ready
print(result.used_segments)              # [('A1', 1), ('A2', 1)]
print(result.full_transform([]))         # ['a1', 'a2']
```

## 验证范围（本地合成数据）

全部测试数据为进程内构造的合成数据，无网络、无外部目标、无第三方
依赖。共 38 个用例，覆盖：

- **正常场景**：连续分片合并、乱序上报按区间排序、完整重试拆分启用、
  多层嵌套替换递归解析、多分片全局顺序保持；
- **边界条件**：空收集器、非法区间（空/反向/负端点）、相邻缺口合并、
  输入状态不被变换修改、组合器快照语义、无任何成功尝试的 `failed`；
- **交错情况**：等待分片/回退分片/正常分片并存、片段先失败后结构
  固定、重试分片再次重试、缺口到齐前后两次 `merge` 的状态切换；
- **回退语义**：片段失败禁用替换并回退旧分片、旧分片失败时回退更旧
  尝试、嵌套失败逐层吸收；
- **结构校验**：重复片段/重复登记、替换先于旧尝试、片段未铺满区间、
  片段越界、顶层区间重叠、循环引用、幂等重放与冲突重放；
- **顺序不可交换**：用标签变换断言到达顺序不影响应用顺序，颠倒组合
  顺序产生不同状态。

实测证据：`python3 -m unittest discover -s tests -v` 输出 `Ran 38 tests ... OK`；
`python3 smoke_demo.py` 输出 `smoke OK`。
