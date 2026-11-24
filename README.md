# 分位数归并器（quantile_merger）

面向指标采集管线的可嵌入库：上游只保留**覆盖有限整数值域的不相交整数桶计数**
（没有原始延迟值），多个副本可能对同一源、同一时间段给出**几套不同桶边界**的
观测，它们描述的是同一批整数样本，必须同时成立。本库在给定连续时间窗内返回
第 k 小值**可能达到的最小值和最大值**，并各附一份以**区间与重数**表示、
可独立验算的小型分配见证。

仅使用 Python 3.11 标准库，无第三方依赖。

---

## 1. 问题模型与算法

设某时间段未知的整数样本多重集取值于有限域 `[lo, hi]`，定义前缀计数
`S(v) = #{样本 x : x <= v}`，并固定 `S(lo-1) = 0`。

* 每个桶 `[a,b]` 计数为 c  ⇔  等式约束 `S(b) − S(a−1) = c`；
* 样本重数非负  ⇔  对所有相邻原子边界 p 有 `S(p) >= S(前一节点)`；
* 同一时间段所有观测的样本总数必须一致（由 `S(hi) − S(lo−1) = total` 强制）。

所有约束都是 `S(u) − S(v) <= d` 形式的**差分约束**，约束矩阵全幺模，
因此整数可行域与线性松弛一致。求解管线：

1. **加权并查集**合并所有桶等式，直接发现同一前缀节点的矛盾赋值；
2. 对并查集连通组件之间的单调性边建图，用 **Bellman-Ford/SPFA 最长路**
   检测正环（不可行），并计算每个前缀节点的最小/最大可行值；
3. 第 k 小极值在全部原子单元边界上一次扫描得到：
   * 最小 kth = 最小的 v 使 `max S(v) >= k`；
   * 最大 kth = 最大的 v 使 `min S(v−1) <= k−1`；
4. 窗口跨多个时间段时，各段约束独立，前缀和极值逐点相加后同样扫描；
5. 见证：每段取对应极值前缀和解，把每个原子单元的样本集中放在单元左边界
   （最小 kth）或右边界（最大 kth），得到单点区间 `[v,v]` + 重数的紧凑分配；
6. 不可行时用**删除过滤**抽取极小矛盾观测子集（MUS）：最终集合删任一条后
   都可行，即题面要求的"去掉任一条后都可行的矛盾观测子集"。

版本化方面：观测按 `(source, segment_start, series_id)` 归属观测系列，
同系列高修订号替换低修订号；每次摄入产生单调递增的全局版本，见证内嵌其
生成版本的完整观测快照，迟到更正后旧见证仍可独立复算。

### 规模与复杂度

* 单段桶数合计不超过 80，前缀节点数 O(桶数)；
* 单段样本数可达 10 亿，全程使用 Python 任意精度整数，无浮点、无固定哨兵；
* 单段建模近似线性（并查集 + 小规模 SPFA），MUS 抽取为 O(r²) 次可行性检查，
  r 为该段观测条数。

---

## 2. 公开 API

### 记录类型（`quantile_merger.records`）

| 类型 | 说明 |
| --- | --- |
| `Bucket(lo, hi, count)` | 整数闭区间桶及其非负计数，构造时校验无缝覆盖值域 |
| `Observation(source, segment_start, segment_length, lo, hi, buckets, series_id, revision)` | 单条分桶观测；`segment_end = segment_start + segment_length`，`total` 为桶计数之和 |
| `ValidationError` | 记录结构非法（断档/重叠/越界/负计数/坏修订等） |

两者均为冻结 dataclass，提供 `to_dict()` / `from_dict()` 做 JSON 序列化。

### 计算引擎（`quantile_merger.model`）

| 符号 | 说明 |
| --- | --- |
| `SegmentModel(observations)` | 同段全部有效观测的联合约束模型；`feasible`、`reason`、`total`、`points`、`s_min`、`s_max` |
| `SegmentModel.prefix_bounds(v)` | 可行域上 `(min S(v), max S(v))`，v 可为域内任意整数 |
| `SegmentModel.cell_counts(maximize)` | 极值解在各原子单元上的计数 |
| `SegmentModel.build_allocation(bound)` | `bound="min"/"max"` 的极值分配（RLE 单点区间 + 重数） |
| `InfeasibleError` | 在不可行模型上调用求解方法时抛出 |
| `KOutOfRangeError` | k 超出窗口样本总数 |
| `Witness / WitnessEntry / Allocation / RLE / WindowResult / MusReport` | 见证与结果数据结构，均支持 JSON 往返 |

### 窗口扫描（`quantile_merger.window`）

* `compute_window_bounds(models, k) -> (min_kth, max_kth)`
* `build_window_witness(...)` 构造窗口级完整见证。

### 版本化存储（`quantile_merger.store`）

| 符号 | 说明 |
| --- | --- |
| `VersionedStore(retain_history=True, max_cached_models=256)` | 线程安全（`RLock`）的修订感知存储 |
| `ingest(observation) -> Change` | 摄入/替换修订；过期低修订抛 `ValidationError`，同修订同内容幂等 |
| `ingest_many(observations)` | 批量摄入 |
| `query_window(source, window_start, window_end, k, segment_starts=None, at_version=None) -> WindowResult` | 窗口查询；窗边界只落在段界，段须无缝铺满窗口 |
| `effective_observations(source, segment_start, at_version=None)` | 指定版本上的有效观测 |
| `affected_segments(changes)` | 迟到更正后定位受影响段，便于重查 |
| `HistoryMissingError` | 请求不存在或已裁剪的历史版本 |

`WindowResult.status` 为 `"ok"` 时携带 `min_kth/max_kth/min_witness/max_witness`；
为 `"infeasible"` 时 `mus_reports` 列出各矛盾段的 MUS 与原因。

### MUS（`quantile_merger.mus`）

* `find_mus(observations) -> tuple[Observation, ...] | None`
* `is_feasible(observations) -> bool`

### 独立验算（`quantile_merger.witness`）

* `verify_witness(witness, expected_store_version=None) -> VerificationReport`

验算**只依赖见证内嵌的观测快照与分配**，不需要存储连接，检查：
桶计数/总数/值域/非负约束、分配与声明边界的极值单元计数完全一致、
合并样本第 k 小值等于声明值、以及从零重建模型重算的极值与声明一致。

### 包根便捷导入

```python
from quantile_merger import (
    Bucket, Observation, ValidationError,
    SegmentModel, InfeasibleError, KOutOfRangeError,
    VersionedStore, Change, HistoryMissingError,
    Witness, WitnessEntry, Allocation, RLE, WindowResult, MusReport,
    verify_witness, VerificationReport, find_mus,
)
```

---

## 3. 最小使用示例

```python
from quantile_merger import Bucket, Observation, VersionedStore, verify_witness

store = VersionedStore()
# 两个副本对同一批 5 个样本给出不同桶边界
store.ingest(Observation(
    "latency", 0, 60, 1, 10,
    (Bucket(1, 5, 3), Bucket(6, 10, 2)), "replica-a", 1))
store.ingest(Observation(
    "latency", 0, 60, 1, 10,
    (Bucket(1, 3, 1), Bucket(4, 10, 4)), "replica-b", 1))

result = store.query_window("latency", 0, 60, k=3, segment_starts=[0])
assert result.ok
print(result.min_kth, result.max_kth)        # (4, 5)
print(verify_witness(result.min_witness).ok) # True
```

不可行场景：

```python
result = store.query_window("latency", 0, 60, k=1, segment_starts=[0])
if not result.ok:
    for report in result.mus_reports:
        print(report.reason,
              [(o.series_id, o.revision) for o in report.mus])
```

迟到更正与历史重查：

```python
store.ingest(new_revision_observation)           # revision 更大
latest = store.query_window(..., at_version=None)
old    = store.query_window(..., at_version=2)   # 旧快照结果不变
verify_witness(old.min_witness)                  # 旧见证独立验算通过
```

---

## 4. 复现步骤

要求 Python 3.11.6（仅标准库）。在工作区根目录执行：

```bash
# 运行全部 unittest 测试（从工作区根目录发现 tests/test_*.py）
python3 -I -m unittest discover -s tests -t . -v

# 仅跑某一组
python3 -I -m unittest discover -s tests -t . -k test_mus
```

`-I` 以隔离模式运行，忽略环境变量与当前目录中的第三方模块，避免同名文件
劫持标准库导入；`tests/_bootstrap.py` 负责把工作区根加入 `sys.path`。

---

## 5. 验证范围

测试均为真实功能验证（无预置输出替代），共 65 个用例，分布如下：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_records.py` | 桶/观测校验：断档、重叠、越界、负计数、坏修订、序列化往返 |
| `test_segment_model.py` | 单段手工场景、前缀和极值、k 越界、桶冲突/总数冲突、单点域、零样本段、宽桶自由度、10 亿计数、80 桶上限 |
| `test_difference_constraints.py` | 多组件自由单元（Bellman-Ford 路径）、固定/自由混合单元、两套分层观测、十亿级大整数精度 |
| `test_window.py` | 多段窗口与暴力枚举逐秩对拍、窗边界必须落段界、段缺口拒绝、迟到更正后重查、单段不可行污染窗口、零样本段、不相交值域 |
| `test_mus.py` | 成对矛盾、MUS 极小性（删任一条即可行）、含无关观测、三方矛盾、修订身份携带 |
| `test_store.py` | 高修订替换、过期修订拒绝、同修订冲突/幂等、历史快照重查、历史裁剪、受影响段定位、不可行窗口返回 MUS |
| `test_witness.py` | 两份见证验算、边界达成性、存储更正后旧见证独立有效、篡改/伪造声明被拒、JSON 往返后仍可验算 |
| `test_concurrency.py` | 8 线程并发摄入版本单调、同系列并发修订、4 读者并发查询并发摄入、长更正序列后旧见证与旧快照有效 |
| `test_randomized.py` | 固定种子随机生成分层直方图，单段 60 组、不可行探测 40 组、双段窗口 25 组，全部与暴力枚举逐秩对拍 |

暴力参考实现（`tests/_brute.py`）在小值域上枚举每个原子单元样本在整数点上的
全部弱组合，列出满足全部观测的完整样本多重集，作为引擎极值的独立参照；
随机测试中额外观测由基准多重集重新分桶生成，保证联合可行。

### 明确的故障恢复 / 边界 / 交错验收点

* **矛盾恢复**：不可行段返回 MUS，且测试断言删 MUS 任一条后剩余集合可行；
* **边界**：k=1 与 k=总数、零样本段、单点值域、负值域、零计数桶、10 亿计数、80 桶；
* **并发交错**：多线程摄入/查询下版本号严格单调、每次读到的见证都能当场验算通过；
* **迟到更正**：高修订替换后，指定历史版本的查询结果与旧见证保持不变并独立可验。

### 不在验证范围内的事项

* 未做物理持久化（存储为进程内结构）；
* 未接入真实网络/系统指标，所有数据均为本地合成；
* 不包含任何攻击、漏洞利用或规避类载荷。
