# 多分片 TopK 算子

从多个按页返回候选的分片拉取排名结果，按**分数降序、同分时 ID 升序**全局排序，
同一稳定 ID 只保留一次，并为每个分类执行独立的最大保留数（配额）。算子能基于
分片给出的后续页边界，在**证明未读候选不可能改变选中集合**时提前停止；证明不足
时继续读页。所有结果均与一个独立的全量顺序扫描实现逐条对照验证。

仅使用 Python 3.11.6 标准库，无第三方依赖。

## 目录结构

```
topk/
  models.py    # Candidate / Bound / HeapEntry / Page / TopKResult 等数据模型
  shard.py     # 分页分片：顺序读页 + 后续页紧边界（线程安全）
  merger.py    # TopKMerger：去重、每分类堆维护、早停证明 can_stop
  driver.py    # 顺序轮转 merge_topk、并发交错 merge_topk_concurrent、断点续跑 continue_merge
  reference.py # full_scan_topk：独立的全量顺序扫描预言机
tests/
  test_topk.py # 27 个 unittest 用例，可从工作区根目录发现
```

## 公开 API

### 数据模型（`topk.models`）

| 类型 | 说明 |
| --- | --- |
| `Candidate(id: str, score: float, category: str)` | 候选对象；同 ID 副本的 `score`/`category` 必须一致，否则合并器抛 `ValueError` |
| `Bound(category, max_score, min_id_at_max)` | 某分片某分类后续页的最优可能边界；`Bound.better_than(score, id)` 判断是否严格优于给定排序位置 |
| `HeapEntry` | 每分类最小堆条目，堆顶为“最差已选项”（分数最低、同分时 ID 最大） |
| `TopKResult` | 结果：`selected`、`per_category_counts`、`pages_read`、`used_bounds`、`early_stopped`、`stop_reason`、`shard_summaries` |

### 分片（`topk.shard`）

- `Shard(shard_id, pages, *, bound_overrides=None)`
  - `read_page() -> Page | None`：顺序读下一页，耗尽返回 `None`；
  - `frontier_bounds() -> dict[str, Bound]`：**当前未读后续页**按分类的最高可能
    分数，以及该最高分对应的最小可能 ID；耗尽时为空映射；
  - `seek(page_index)`：重置读指针（断点续跑/测试用）；
  - `pages_read` / `total_pages` / `exhausted()`。
- `build_shards({"S1": [[page0...], [page1...]], ...}) -> tuple[Shard, ...]`

本地合成实现里边界由后续页真实数据计算，因此是**紧边界**；
`tests` 中的 `LooseBoundShard` 会故意抬高边界，验证算子在边界偏松时的保守性。

### 算子（`topk.driver` / `topk.merger`）

- `merge_topk(shards, quotas) -> TopKResult`：顺序轮转拉页，每页后重做证明；
- `merge_topk_concurrent(shards, quotas) -> TopKResult`：每分片一个线程并发拉取，
  共享加锁合并器（`_ThreadSafeMerger`）；
- `continue_merge(merger, shards)`：用已吸收过历史页的同一个合并器继续拉取（断点续跑）；
- `TopKMerger(quotas)`：底层合并器，`add_page(...)`、`can_stop(shard_bounds)`、
  `worst_selected(category)`、`category_full(category)`。
- `full_scan_topk(shards, quotas) -> tuple[Candidate, ...]`：测试预言机，读全部页、
  去重、全局排序后按分类截取配额，不使用任何边界或早停逻辑。

`quotas` 形如 `{"A": 2, "B": 1}`；配额为 0 或未列出的分类永不入选，也不参与证明。

## 排序与去重语义

- 全局顺序：`(-score, id)`，即分数降序、同分 ID 升序。
- 每个分类独立保留前 `quota[c]` 个去重对象；最终 `selected` 再按全局顺序输出。
- 同一 ID 以**首次出现**的属性为准；后续副本若 `score` 或 `category` 不同，
  抛出 `ValueError`（违反“固定查询版本下同 ID 副本属性相同”的题面前提）。

## 早停证明（核心正确性论证）

记分片 `s` 对分类 `c` 的后续页边界为 `B(s,c) = (max_score, min_id_at_max)`，
其含义是后续页中该分类任何候选都满足 `score <= max_score`，且等分时
`id >= min_id_at_max`。对每个配额为正的分类 `c`：

1. **分类未满额**：任何未读的 `c` 候选都可能入选。只有当**所有分片**对 `c`
   都没有边界（即后续页不存在 `c` 候选）时才可停；否则继续读。
2. **分类已满额**：设最差已选项位置为 `(w_score, w_id)`（堆顶）。只有严格优于
   该位置的未读候选才能把它挤出，因此要求所有分片的 `B(s,c)` 都**不严格优于**
   `(w_score, w_id)`。严格优于是指：边界分数更高，或同分但边界最小 ID 更小。
3. **边界与最差位置相等**（同分且同 ID）：后续页中达到该边界的对象只能是同一
   ID 的副本，去重后不改变选中集合，因此安全，可停。
4. 配额为 0 / 未配置的分类不参与证明，不会因为“还有候选”而阻止停止。

题面特别要求的“**某分类配额已满，但它最差已选项还能被同类替换时仍要读下去**”
由规则 2 保证：此时 `B(s,c).better_than(w_score, w_id)` 为真，`can_stop`
返回 `False`，驱动继续读页。`tests/test_topk.py::QuotaFullButReplaceableTest`
与 `EqualScoreBoundTest` 分别覆盖“跨分片更优对象”和“同分更小 ID”两种替换来源。

`can_stop` 返回的原因字符串与 `TopKResult.used_bounds`（停止时刻各分片参与证明
的全部边界）、`pages_read`（已读页下标）一起构成可审计的停止依据。

## 复现步骤

要求 Python 3.11.6，仅标准库。在工作区根目录执行：

```bash
# 标准发现（普通模式）
python3 -m unittest

# 显式从 tests 目录发现
python3 -m unittest discover -s tests -v

# 隔离模式（-I 忽略环境变量与用户 site-packages）
python3 -I -m unittest discover -s tests
```

本机 Homebrew Python 3.11.6 在 `-I` 下不把当前目录加入 `sys.path`，因此
`tests/test_topk.py` 顶部显式把工作区根插入 `sys.path`，保证上述三种方式
都能从根目录发现并运行全部用例。

## 验证范围（27 个用例，均真实执行）

所有功能性断言都以 `full_scan_topk` 的真实计算结果为预言机，没有使用预置的
演示输出。场景分类：

- **题面组合场景**（`CombinedScenarioTest`）：跨分片重复 ID（`id1`/`id2`）、
  排名较低的 B 类先达到配额、B 类边界分数与最差已选项相等；断言算子结果与全量
  扫描一致、S2 第二页（纯副本页）被相等边界证明跳过、停止时刻边界满足证明不变量。
- **配额满仍需继续**（`QuotaFullButReplaceableTest`）：满额后跨分片出现更优对象，
  必须读到并替换。
- **边界分数相等**（`EqualScoreBoundTest`）：同分但未读 ID 更小时必须继续；
  边界恰好等于最差位置（只剩同 ID 副本）时允许早停；`Bound.better_than` 单元语义。
- **去重与不变量**（`DedupAndInvariantTest`）：跨分片/跨页重复 ID 只保留一次；
  同 ID 分数或分类不一致时抛 `ValueError`。
- **边界与堆序回归**（`ShardBoundaryTest`、`HeapDirectionTest`）：边界随读指针
  正确收缩、最高分同分取最小 ID；堆顶严格为最差已选项。
- **边界条件**（`EdgeCaseTest`）：空分片零页停止、零配额/未知分类不阻塞、
  配额大于候选数、纯同分 ID 升序、负配额拒绝、偏松边界强制读完全部页但结果不变。
- **交错与并发**（`ConcurrentInterleaveTest`、`ShardOrderingTest`）：20 个随机
  种子下顺序轮转与多线程并发结果都与全量扫描一致，读取页数不超总页数；反转分片
  顺序结果不变。
- **断点续跑**（`ResumeTest`）：预热首页后用同一合并器 `continue_merge` 续跑，
  结果与全量扫描完全一致；从头重跑可复现相同的读取账本。
- **随机化属性测试**（`RandomizedPropertyTest`）：60 组随机数据（随机分数/分类、
  跨分片重复 ID、随机分页含空页、随机 0–4 配额），逐条比对全量扫描，并校验
  页下标合法、计数不超配额、早停时证明不变量成立。

## 明确的范围限制

- 题面未要求持久化故障恢复（如崩溃后从外部存储重放页日志）；本实现提供的是
  进程内的确定性重跑与 `continue_merge` 断点续跑能力，并由测试覆盖。
- 分片为本地合成数据源，不访问任何真实网络目标、账户或系统；边界由本地数据计算。
- 并发版在“证明成立”与“工作线程实际停下”之间存在竞态，可能比顺序版多读若干页，
  但不会超过分片总页数，且选中集合与全量扫描一致（测试已断言）。
