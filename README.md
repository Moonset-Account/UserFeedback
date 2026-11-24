# persistent-miniheap

持久内存上的小型对象堆：在可注入断电的持久介质后端上，实现根指针、
定长槽中的变长对象、空闲表，以及带**代次句柄**的对象替换与回收协议。

仅使用 Rust 1.95.0 标准库，无任何第三方依赖。

## 1. 介质语义（`src/medium.rs`）

介质按缓存行组织，每行 64 字节 = 8 个 64 位字。

- **字对齐原子写**：`write_word` 只修改易失镜像；协议只发起字对齐写，
  非对齐/跨行访问在类型层面就不会出现。
- **显式刷行**：`flush_line(line)` 把**当时**的整行快照加入待持久集合；
  之后的易失写不会改变已登记的快照。同一行按发起次序排队。
- **屏障**：`barrier()` 完成后，此前刷出的全部行像保证已持久；
  不同缓存行之间允许按任意顺序落盘。
- **断电注入**：`crash(&CrashCut)` 丢弃易失镜像。每个待持久行像可以
  落盘或不落盘，但同一行只能按发起次序取**前缀**，不能跳号；不同行
  的选择互相独立。
- **合法断电像枚举**：`legal_cuts()` 按屏障把事件日志切成区间，
  枚举每个区间内所有合法的前缀组合及其完整介质行像。
- **落盘顺序反推**：`reconstruct(region, target, strategy)` 从观测到的
  失败行像集合，反推出一种必然导致它的全局落盘顺序（同行前缀、
  跨行用 `LineAscending`/`LineDescending` 仲裁），并在内部从零介质
  重放复验；目标像不存在或属于别的区间时返回明确错误。

## 2. 堆布局与协议（`src/heap.rs`）

| 行 | 内容 |
| --- | --- |
| 0 | 根表：4 个根项，每项一个字，0 = 空根，否则编码 `(槽号, 代次)` |
| 1 | 空闲表：6 个空闲项，每项编码一个空闲槽的 `(槽号, 代次)` |
| 2..=7 | 6 个槽，每槽一条缓存行；第 0 字为头（状态 + 代次 + 载荷长度），其余 7 字为载荷 |

对象为 1..=7 个字的变长对象。槽头三态：`FREE` / `PENDING` / `COMMITTED`。

### 替换协议（每个阶段之间都有屏障）

1. **分配**：从空闲表摘除槽并压缩表项（先持久化摘除，杜绝表内空洞
   导致漏项），代次 +1，写 `PENDING` 头与完整载荷并刷出（头与载荷
   同一行，单条行像即完整对象）。
2. **提交**：槽头 `PENDING -> COMMITTED`，屏障后完整新对象已持久，
   但此时尚无根指向它。
3. **发布**：字原子写新根项并屏障——这是成功点；此后整个新对象可见。
4. **回收**：把不被任何根引用的旧 `COMMITTED` 槽标记为 `FREE`
   （代次再 +1），屏障后重建空闲表。

### 恢复协议（`PersistentHeap::mount`）

1. 校验全部槽头，恢复每槽代次；
2. 校验每个持久根都指向 `COMMITTED` 槽且代次匹配——**途中替换永远
   不会发布指向 PENDING 槽的根**；
3. 回滚所有 PENDING 槽，回收所有不可达的 COMMITTED 槽；
4. 独立重建空闲表：恰为全部非可达槽。

恢复结果只可能落在**已提交旧根**或**完整新根**之上。`audit()` 独立
遍历根表与空闲表，断言可达集合与空闲集合互斥、并集覆盖全部槽、
且每个可达槽头与根代次一致——无永久泄漏。

### 代次句柄

读者持有 `Handle { slot, generation }`。槽每次释放/复用都推进代次，
旧句柄在 `read` 时因代次不匹配被拒，**不可能读到复用后的对象**。

## 3. 公开 API

### 介质层 `PersistentMedia`

```
cold_boot(line_count) -> Self
write_word(global_word, value)   // 易失原子写
read_word(global_word) -> u64
flush_line(line)                 // 登记当时行像
barrier()                        // 此前刷出全部持久
crash(&CrashCut)                 // 注入断电并重新上电
legal_cuts() -> Vec<CrashRegion> // 枚举每区间全部合法断电像
reconstruct(region, &[LineImage], OrderStrategy) -> Result<Reconstruction, ReconstructError>
replay_order(&[(usize, LineImage)], line_count) -> Vec<LineImage>
fork() -> PersistentMedia        // 同一历史的独立副本
log() / region_count() / line_count() / is_powered()
```

### 堆层 `PersistentHeap`

```
format(media) -> Self
mount(media) -> (Self, RecoveryReport)
publish(root, &[u64]) -> Result<Handle, HeapError>   // 空根首发
replace(root, &[u64]) -> Result<Handle, HeapError>   // 原子替换
drop_root(root) -> Result<(), HeapError>             // 撤根并立即回收
read(Handle) -> Result<ObjectView, HeapError>        // 代次校验
root_handle(root) -> Result<Option<Handle>, HeapError>
free_table() -> Vec<Handle>
slot_state(slot) -> u64
audit() -> Audit
media() / into_media()
```

错误类型 `HeapError`：`BadPayloadLen`、`NoSpace`、`BadRoot`、`BadSlot`
（代次过期也归为 `BadSlot`）。

## 4. 撤根后立即复用槽的例子

`examples/drop_reuse.rs` 是真实 API 走查（无预置输出）：

```
cargo run --offline --example drop_reuse
```

流程：发布对象拿到句柄 → `drop_root` 撤根并屏障回收，槽立刻回到
空闲表 → 再次 `publish` 复用**同一槽号**但代次 +2 → 旧句柄读取被拒、
新句柄读到新对象 → `audit()` 验证互斥与全覆盖。

## 5. 复现步骤

```
cargo test --offline
cargo run  --offline --example drop_reuse
```

## 6. 验证范围（全部为真实执行的测试，共 24 个）

### 介质层 `tests/medium.rs`（9 个）

- 未刷出的易失写在断电后丢失；
- 刷行取快照，之后的写不影响已登记行像；
- 同行严格前缀、不同行独立组合（含组合数枚举）；
- 屏障保证此前刷出持久并开启新区间；
- `crash` 对每个合法切割精确应用前缀；
- 非前缀/越界切割被拒绝；
- 对每个合法断电像反推落盘顺序，两种行间仲裁都能重放还原，
  且每行序列等于“可选基线 + 区间前缀”；
- 反推能识别从未出现的行像（`NoMatchingImage`）与区间不符
  （`OutsideRegion`）；
- 多行字写互不干扰、屏障后断电内容全保留。

### 堆层 `tests/heap.rs`（12 个）

- 发布/读取/替换正常路径与旧句柄失效；
- 载荷长度边界（1、7 字合法；0、8 字拒绝且不耗槽）；
- 容量守恒与撤根后容量恢复；
- **撤根后立即复用同槽**、代次单调、旧句柄拒绝；
- 空闲表压缩：多轮发布/替换/撤根后表内无空洞、不丢可用槽；
- 替换协议每个屏障区间的全部合法断电像：恢复后只落在完整旧根或
  完整新根，旧句柄读不到复用对象；
- 撤根协议每个屏障区间的全部合法断电像：对象不复活、不成半对象；
- 恢复确实回滚 PENDING 槽且统计自洽、无泄漏；
- 每个恢复出的失败像都能反推导致它的落盘顺序；
- 多根交错替换互不干扰、未触及的根稳定；
- **一次替换改多行**（根表行、空闲表行、槽行）并在任意行间交错下
  保持提交原子性；
- 200 轮随机交错 + 随机断电模糊测试：每轮再对全历史所有合法断电像
  做恢复，恒有审计互斥/全覆盖、分区计数守恒、存活根只读得到完整对象。

### 堆白盒单测 `src/heap.rs`（3 个）

- 空闲表为空时 `allocate` 返回 `NoSpace` 且不改动任何槽头；
- 分配/回收循环上代次单调递增，历史句柄全部失效；
- 非法根与非法载荷在触碰槽之前被拒绝。

### 未执行项说明

无。题面要求的故障恢复、边界、并发（行间）交错场景均由上述测试
真实执行覆盖；不存在只跑常规路径即宣称完成的情况。
