# namespace-fs：文件系统命名空间模型

一个**纯内存、纯 Rust 标准库**实现的文件系统命名空间模型，覆盖题面要求的
四类能力：普通文件硬链接、打开后通过固定 inode 的句柄读取、两个目录项的
原子交换（可跨目录）、以及可恢复的预写日志。

- Rust 版本：**1.95.0**（见 `rust-toolchain.toml`）
- 依赖：**无第三方依赖**，仅标准库；`Cargo.lock` 中没有任何外部包
- “磁盘”由一个 `Vec<u8>` 模拟，所有操作只针对本工作区内的合成数据，
  不访问任何真实文件路径、网络或账户

## 目录结构

```
Cargo.toml              工程清单（无 [dependencies]）
rust-toolchain.toml     固定 Rust 1.95.0
src/lib.rs              crate 入口与类型再导出
src/path.rs             路径组件解析（单元测试）
src/journal.rs          预写日志、记录编解码、崩溃注入、提交回放（单元测试）
src/fs.rs               命名空间主体：inode/目录项/句柄/交换/回收/恢复（单元测试）
tests/hardlink.rs       硬链接、句柄存活、真实回收、inode 号复用
tests/swap.rs           原子交换、句柄 vs 路径分叉、目录随移动、成环拒绝、边界
tests/recovery.rs       提交前后断电、日志撕裂、孤儿回收、重开一致性
tests/concurrency.rs    多线程混合压力 + 确定性交错序列 + 崩溃交错
tests/boundaries.rs     路径语法、目录规则、名称冲突、nlink 逐次变化
```

## 公开 API

所有类型都从 crate 根（`namespace_fs::*`）导出。

### 命名空间主体 `FileSystem`

| 方法 | 语义 |
| --- | --- |
| `FileSystem::fresh() -> FileSystem` | 新建空白命名空间，根 inode 固定为 1 |
| `FileSystem::reopen(disk: Vec<u8>) -> Result<FileSystem, FsError>` | 从持久介质重挂：回放全部已提交事务、丢弃未提交尾部、回收孤儿 inode |
| `into_disk(self) -> Vec<u8>` / `disk_len(&self) -> usize` | 取出/查看模拟介质字节 |
| `arm_crash(&mut self, CrashPoint)` | 武装**一次性**断电注入点 |
| `mkdir(&mut self, path) -> Result<u64, FsError>` | 创建空目录 |
| `create_file(&mut self, path) -> Result<u64, FsError>` | 创建空普通文件，已存在报错 |
| `write_file(&mut self, path, &[u8]) -> Result<()>` | 写普通文件；不存在则创建（O_CREAT\|O_TRUNC 语义），内容写日志 |
| `link(&mut self, target, link_path) -> Result<()>` | 仅普通文件可建硬链接；nlink 加一并写 `Nlink` 日志 |
| `unlink(&mut self, path) -> Result<()>` | 删普通文件目录项；nlink 减一，归零且无句柄才写 `InodeFree` 并回收 |
| `rmdir(&mut self, path) -> Result<()>` | 仅允许删空目录，目录 inode 立即回收并写日志 |
| `open(&mut self, path) -> Result<Handle, FsError>` | 打开普通文件，句柄固定绑定当前 inode，`open_count` 加一（易失） |
| `close(&mut self, Handle) -> Result<()>` | 关闭句柄；最后引用消失且 nlink=0 时此刻真实回收并写日志 |
| `read_handle(&self, &Handle) -> Result<Vec<u8>, FsError>` | **旧句柄读值**：永远读打开时绑定的 inode |
| `read_path(&self, path) -> Result<Vec<u8>, FsError>` | **路径读值**：读路径当前解析到的 inode，交换后可能与句柄不同 |
| `swap(&mut self, a, b) -> Result<()>` | 原子交换两个已存在目录项引用的 inode；只改目录项，拒绝成环 |
| `stat_path(&self, path) -> Result<InodeInfo>` / `inode_info(&self, ino) -> InodeInfo` | 查询类型、nlink、open_count、大小、是否仍分配 |
| `inode_exists(&self, ino) -> bool` | inode 是否尚未被真实回收（用于核对回收与号复用） |
| `list_dir(&self, path) -> Result<Vec<(String, u64)>>` | 按名称排序列举目录项 |
| `committed_records(&self) -> Result<Vec<Record>, FsError>` | 解析当前介质，返回全部已提交日志记录（含 Begin/Commit） |
| `check_invariants(&self) -> Result<(), FsError>` | 全量一致性检查（见下） |

### 关键类型

- `Handle { id, inode }`：句柄快照；`inode` 字段即固定绑定的 inode 号。
- `Inode { id, kind, nlink, open_count, data }`：inode 内存状态。
- `InodeInfo { id, kind, nlink, open_count, size, allocated }`：查询结果，
  `allocated=false` 表示该号已真实回收。
- `NodeKind::{File, Directory}`、`FsError`（11 个错误变体，均实现 Display/Error）。
- 日志：`Journal`、`Record`（9 种记录）、`CrashPoint::{BeforeCommit, AfterCommit}`、
  `SimulatedCrash`。

### 日志记录 `Record`

`Begin(tx)` / `Commit(tx)` 界定事务；事务体可以是
`InodeAlloc`、`InodeFree`、`EntryAdd`、`EntryRemove`、`EntrySwap`、
`Nlink(ino, delta)`、`SetData(ino, data)`。
目录交换、链接计数调整、inode 释放与腾出号的再分配都有对应记录。

## 核心语义如何保证

1. **交换只改目录项引用**：`swap` 在单个日志事务里写一条 `EntrySwap`，
   提交后才交换两个目录项里存的 inode 号；inode 自身、其内容与句柄均不动，
   因此事先打开的句柄继续读到原文件。
2. **跨目录与无环**：交换双方可以在不同目录。对“目录换目录”沿目录父指针
   做祖先判定，若任一是另一者的祖先（含自身）则返回 `WouldCycle`，状态不变。
   目录移动后父指针同步更新，目录内相对路径随整棵子树移动。
3. **链接计数与延迟回收**：文件 `nlink` 等于指向它的目录项数；只有
   `nlink == 0 && open_count == 0`（根 inode 除外）才从 inode 表移除、
   写 `InodeFree` 并把号放回空闲栈（LIFO 复用）。打开计数是易失状态，不写日志。
4. **崩溃一致性**：每个结构变更都是“先追加事务体、再追加 Commit”的单事务。
   - `BeforeCommit`：事务体已落盘、Commit 缺失 → 重放时整事务丢弃；
   - `AfterCommit`：Commit 已落盘 → 整事务生效；
   - 介质尾部被截断（半截记录或缺 Commit）→ 静默丢弃最后一个未提交事务；
   - 已提交事务内部出现非法标签/类型才报 `CorruptJournal`。
5. **重开孤儿回收**：若崩溃发生在“nlink 已归零但 `InodeFree` 尚未提交”
   的窗口，`reopen` 回放后会把 nlink=0 或不可达的 inode 回收，并把
   `InodeFree` 补写进日志，保证再次重开仍然一致。

`check_invariants()` 校验：根 inode 状态、目录树无环且无目录硬链接、
目录项不指向已释放 inode、目录父指针与实际结构一致、文件 nlink 等于
目录项计数、目录 nlink 恒为 1、`open_count` 与句柄表一致、句柄不指向
已回收 inode。

## 复现步骤

```bash
# 工具链固定为 1.95.0（rustup 会自动读取 rust-toolchain.toml）
cargo test --offline            # 运行全部单元测试与集成测试
cargo test --offline --release  # release profile 下再跑一遍
```

离线模式可直接运行：工程无任何外部依赖，不需要联网下载 crate。

## 验证范围（共 31 个测试，全部由断言驱动，无预置演示输出）

### 正常路径
- 硬链接共享 inode、nlink 正确、任一链接写入对其它链接可见
  （`hardlink::hardlinks_share_inode_and_counts`）
- 跨目录交换后旧句柄读原 inode、路径读到对侧 inode、nlink 不变
  （`swap::swap_across_directories_handle_vs_path_diverge`）
- 目录交换后内部相对路径随子树移动（`swap::relative_paths_inside_swapped_directories_move_with_them`）
- 重开后交换结果、硬链接关系与 nlink 完整保留
  （`recovery::reopen_replays_swap_and_link_counts_exactly`）

### 边界与错误路径
- 路径含 `..` 拒绝、`.`/空组件忽略、根目录保留（`boundaries::path_syntax_and_component_rules`）
- 目录不可 link/open/unlink，文件不可 rmdir，非空目录不可 rmdir，
  中间组件为文件报 `NotADirectory`（`boundaries::directory_operation_restrictions`）
- 名称冲突、空目录 rmdir、列举排序（`boundaries::empty_dir_rmdir_and_name_conflicts` 等）
- nlink 随 5 个链接逐个删除而递减，归零无句柄立即回收
  （`boundaries::nlink_reflects_multiple_links_and_partial_removal`）
- 交换任一侧不存在、文件/目录混排、自交换、根参与交换
  （`swap::swap_boundaries_missing_entries_and_file_dir_mix`）
- 坏句柄与重复 close 被拒（`hardlink::bad_handle_and_double_close_are_rejected`）

### 故障恢复（题面验收条件）
- Commit 前断电：整事务丢弃，命名空间回到断电前
  （`recovery::crash_before_commit_discards_whole_transaction`）
- Commit 后断电：事务完整生效（`recovery::crash_after_commit_keeps_transaction`）
- 日志尾部撕裂：半截事务被忽略（`recovery::torn_log_tail_is_ignored_on_reopen`，
  另见 `journal::truncated_tail_is_dropped` 单元测试）
- nlink=0 的孤儿 inode 在重开时回收、补写日志、再次重开仍一致、号被复用
  （`recovery::orphan_inode_with_zero_links_is_reclaimed_on_reopen`）
- 句柄关闭驱动的真实回收写入日志并重放保持
  （`hardlink::close_driven_reclamation_is_journaled_and_survives_reopen`）
- rmdir 的目录 inode 回收可重放（`recovery::reopen_after_rmdir_reclaims_directory_inode`）

### 并发与交错（题面验收条件）
- 4 线程 × 75 轮混合 create/link/unlink/swap 压力，每轮后做不变量检查，
  结束后再从日志重开校验（`concurrency::multithreaded_mixed_workload_preserves_invariants`）
- 确定性交错：open → swap → link → 逐次 unlink → close，逐步核对
  句柄读值/路径读值/nlink/回收/号复用
  （`concurrency::deterministic_interleaving_swap_link_unlink_close`）
- 正常操作之间插入断电注入，验证句柄语义与重开一致性
  （`concurrency::crash_interleaved_between_successful_operations`）

### 未覆盖与范围说明
- 模型不实现权限位、时间戳、符号链接、`..` 父目录跳转与并发读写锁；
  并发语义通过外层 `Mutex` 串行化提交来验证交错正确性，而非细粒度锁。
- 句柄是进程内易失对象，按题意不跨“重挂”存活；重开后需要重新 `open`。
- 所有验证均来自 `cargo test` 的真实断言执行，没有使用任何自写演示的
  预置输出替代功能验证。
