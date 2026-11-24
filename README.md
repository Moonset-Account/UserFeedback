# local-compaction：有序段局部压实库

纯 Rust 1.95.0 标准库实现（无任何第三方依赖）的 LSM 风格局部压实库。
它在只知道被选中段的完整记录、未选深层**可能含键的区间**（旧值未知）
的前提下，对整值写入、点删除、列表追加和半开区间范围墓碑做压实，
并为每条输出附上来源证据，同时证明可安全回收的记录。

## 1. 模型与语义

记录携带全局唯一、单调推进的序号 `seq`。层按 `depth` 排列，depth 越小越新。
读序号 `r` 下只有 `seq <= r` 的记录可见，按序号回放：

| 操作 | 语义 |
| --- | --- |
| `Put(list)` | 整值写入；写入**空列表仍表示键存在** |
| `Delete` | 键重置为不存在 |
| `Append(elem)` | 向列表末尾追加一个元素；对**不存在**的键追加时以空列表起步 |
| `RangeTombstone [start,end)` | 对区间内每个键等价于在其序号处执行一次 `Delete` |

删除后键不存在；之后的追加可以重新产生该键。
未选深层（`DeepLayer`）只暴露“可能在哪些键区间持有键”，其旧基值对压实器未知，
读取时以符号值 `UnknownBase { depth, key, suffix }` 表达。

## 2. 压实策略（`src/compaction.rs`）

1. **读者点** = 所有仍开放的快照序号 + 当前读序号（去重排序）。
2. **epoch 切分**：对每个点键，按 reset（`Put`/`Delete`/范围墓碑）把记录序列
   切成若干 epoch。没有任何读者点落入的 epoch 整体回收
   （`EpochWithoutReader`，并标注是否已证明深层不覆盖该键）。
3. **存活 epoch 的折叠**：
   - 基值已知（`Put` 复位，或基 epoch 且深层无此键）：把读者不可区分的一串
     `Append` 重放折叠为一条 `Put`，被折叠的操作数进 `FoldedIntoPut` 回收清单；
   - 基值未知（深层可能持有旧值）：**保留每条原始 `Append`**，不做折叠，
     因为折叠会对未知旧值的形态做无根据假设；
   - 存活 epoch 内、最后一个读者之后才出现的事件回收为 `TailAfterLastReader`。
4. **范围墓碑**：先按点键切分，再逐子区间判定——
   - 子区间内有点键需要该墓碑：保留；
   - 无点键需要且与所有深层区间**不相交**：证明死亡，回收
     （`RangeTombstoneDead`）；
   - 与深层区间相交但本无点键需要：**保守保留**（可能负责遮蔽未知旧值）。
   存活部分再按输出段边界拆分，跨边界的墓碑因此被切成多片。
5. **证据闭环**：每条输出记录的 `evidence` 回指输入 `RecordId`；
   所有输入记录要么进入某条输出的证据、要么进入回收清单（被折叠的追加和
   被部分回收的墓碑会同时出现在两处：证据回答“从何推导”，回收回答
   “为何不再独立存在”）。`check_evidence_closure` 对该不变量做强校验，
   悬空引用或未记账记录都会报错。

## 3. 公开 API

根导出（`src/lib.rs`）：

- 模型：`RecordId`、`PointOp`、`Record`、`RangeTombstone`、`Segment`、
  `DeepLayer`、`Value`
- 区间：`KeyRange`、`Bound`（`None` 表示该侧无界）
- 读取：`read_symbolic`、`read_with_deep_values`、`DeepAssignment`、
  `SymbolicValue`、`ReadStack`
- 压实：`compact`、`CompactionInput`、`CompactionOutput`、`OutputSegment`、
  `OutRecord`、`OutRange`、`ReclaimEntry`、`ReclaimReason`、
  `output_as_segments`、`check_evidence_closure`
- 验证：`equivalence_report`、`VerificationReport`、`Mismatch`
- 可回放盘面：`fixture::Tabletop`
- 错误：`EngineError`

关键调用形态：

```rust
let input = CompactionInput {
    selected,            // 选中段（完整记录）
    deep_layers,         // 未选深层的可能含键区间
    read_seq: 60,        // 当前读序号
    open_snapshot_seqs: vec![18, 42],
    output_boundaries,   // 输出段切点
    verification_keys,   // 额外纳入验证的探针键
};
let output = compact(&input)?;
let report = equivalence_report(&input, &output)?;
assert!(report.is_ok());
```

## 4. 等价性验证（`src/verify.rs`）

对**每个开放快照序号与当前读序号**、**每个相关键**（全部点键 + 探针键）：

1. **符号比较**：压实前后 `read_symbolic` 结果逐格相等。符号值相等当且仅当
   同一具体列表，或同一 `(depth,key)` 未知基值带同一后缀——这保证对
   *任何*允许的深层旧值都得到相同结果。
2. **具体穷举**：对验证点上出现的每个未知基值，枚举 4 种候选指派
   （不存在 / 空列表 / 单元素 / 两元素列表），做笛卡尔积后用
   `read_with_deep_values` 比较具体结果。小盘面（4 个未知基值）即 4⁴=256、
   内置盘面（3 个未知基值）即 4³=64 个指派，全部真实执行。
3. **证据闭环校验**。

## 5. 可回放小盘面（`src/fixture.rs`）

`Tabletop::build()` 构造确定性盘面，包含题目点名的全部要素：

- **跨段墓碑**：墓碑 `[n0,n9)` 先被点键 `n2-hit` 切分、再在输出切点 `n4`
  处跨两个输出段拆分；
- **两份开放快照**（18、42）+ 当前读序号（60）；
- **中间追加**：`a-list` 在两快照之间多次追加，演示折叠成三条 `Put`；
- 删除后追加重新产生（`a-reborn`）、空列表写入仍存在（`a-empty`）、
  对缺失键追加从空列表起步；
- 深层未知基值（`a-unknown`、`a-shadowed`、`n5-probe`、`z-probe-out`），
  验证时穷举允许旧值；
- 可证明死亡的删除（`a-dead` 的早期 epoch）、完全死亡墓碑（`[0,1)`）、
  与深层相交而保守保留的墓碑（20 号的阴影部分）；
- 序号晚于读序号的“未来”记录（65 号追加）被回收。

命令行演示（输出全部来自真实执行，非预置）：

```bash
cargo run --offline --example replay_fixture
```

## 6. 复现步骤

```bash
# 工具链：Rust/Cargo 1.95.0，纯标准库、无第三方依赖，可完全离线
cargo test --offline
```

## 7. 验证范围与测试清单

`cargo test --offline` 共 **33 个真实测试**（不含预置输出的演示）：

### 单元测试（`src/**`，21 个）
- `interval`：区间包含/相交/相邻合并/切点拆分/非法区间拒绝（3）
- `read`：Put-Delete-Append 回放次序、空列表存在性、范围墓碑复位、
  未知深层基值的符号/具体读取、缺失指派报错、深层遮蔽次序（4）
- `compaction`：读者间追加链折叠与证据、无读者 epoch 回收、
  未知基值保留原始追加、证据闭环对悬空/未记账记录报错（4）
- `verify`：平凡压实通过、深层指派穷举计数（2）
- `fixture`：全盘面对齐、跨段墓碑拆分、死亡/阴影墓碑、折叠 vs 原始追加、
  删除重生与空列表存在、死亡删除回收 vs 阴影删除保留、证据非空且回指输入、
  具体回放期望（8）

### 集成测试（`tests/`，12 个）
- `end_to_end.rs`（2）：示例同款端到端流程逐格相等、重复压实确定性
- `tombstones.rs`（4）：多输出边界上的墓碑拆分、完全死亡墓碑整条回收、
  与深层区间相交时强制保守保留、多墓碑与点记录交错
- `property.rs`（6）：
  - `random_interleavings_always_preserve_all_allowed_reads`：400 轮
    确定性 PRNG 随机交错盘面（随机操作/序号/墓碑/深层区间/快照/输出边界），
    每轮都做符号比较 + 深层旧值穷举 + 段数与回收类别校验；
  - `random_outputs_partition_keyspace_without_gaps`：150 轮随机输出边界，
    校验段区间首尾相接、键唯一归属；
  - `tail_append_inside_observed_epoch_is_reclaimed_safely`：
    存活 epoch 内最后读者之后追加的回收回归测试（该路径由随机测试首次抓出）；
  - 读序号早于一切记录时全部回收且仍等价；
  - 空列表写入跨快照保持存在；
  - 重复序号、晚于读序号的快照等非法输入被拒绝。

### 明确未覆盖/边界说明
- 深层旧值的“允许集合”按区间与四个代表性具体值建模；真实引擎若有更多
  取值约束，应扩展 `verify::candidate_values`。符号比较本身对取值形态
  不做假设，因此对任意旧值的等价性结论不受候选数量影响。
- 范围墓碑在“与深层相交但无点键需要”时一律保守保留，宁可少回收、
  不误删；这是有意的安全性取舍。
- 所有数据均为本地合成，不访问网络、账户或真实系统目标。
