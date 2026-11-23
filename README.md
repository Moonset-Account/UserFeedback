# logical-decoder

关系库逻辑复制日志的解码模块。输入是带位点（LSN）的日志记录流，不同事务
的片段可以在流中任意交错；输出是**按提交位点排序、只包含完整已提交事务**
的解码批次，并支持整批确认、重开重读与空洞拦截。

仅依赖 Rust 标准库，工具链固定为 Rust 1.95.0（见 `rust-toolchain.toml`）。

## 解决的问题与关键语义

1. **交错片段重组**：事务载荷拆成从 1 开始编号的片段（fragment），不同事务
   的片段可交错到达。解码器按事务聚合，提交记录给出末片号，只有持有
   `1..=last_fragment` 全部片段的事务才算完整。
2. **版本化模式与稳定列身份**：模式声明带表号与版本号；列以 `ColumnId`
   作为跨改名稳定的身份。删除一列后再创建同名列会得到新身份。行变更引用
   写入时的模式版本，解码器始终用该版本解码，而不是最新版本。
3. **旧格式事务跨模式更新**：所有已提交的模式版本在解码器生命周期内持续
   保留，因此一笔引用旧版本、迟迟未提交的事务，在新版本提交后仍能按旧版本
   正确解码。
4. **只发布完整的已提交事务**：撤销（abort）事务连同其待发布模式声明整体
   丢弃；输出严格按提交位点排序，事务内行变更保持日志原序。
5. **空洞拦截**：无法解码的提交形成空洞（`BlockReason`），并报告：
   - `Incomplete`：片段缺失，列出全部缺失片号与提交位点；
   - `MissingSchema`：引用了未知的（表，模式版本），给出表号、版本、事务号、
     提交位点和首次引用位点。
   空洞之后提交的任何事务都不得越过它发布。
6. **整批确认与重开重读**：事务按固定大小编号成批（`Batch`），下游按批累计
   确认；未确认批次保留原编号与内容，可重复投递。重开时从已确认位点之前
   重读日志：已确认的提交被静默跳过，未确认批次以相同编号与内容重新派生。

## 公开 API

### 输入模型（`src/model.rs`）

| 类型 | 说明 |
| --- | --- |
| `LogRecord` | 枚举：`Schema { lsn, txn_id, fragment, schema }`、`RowChange { lsn, txn_id, fragment, change }`、`Commit { lsn, txn_id, last_fragment }`、`Abort { lsn, txn_id }` |
| `TableSchema { table_id, version, columns }` | 一个表的一个模式版本 |
| `ColumnSpec { id, name, data_type }` | 列声明；`id` 是稳定身份，`name` 仅为展示元数据 |
| `RawChange { table_id, schema_version, op, values }` | 原始行变更，值按 `ColumnId` 存放 |
| `RowOp` | `Insert` / `Update` / `Delete` |
| `Scalar` | `Null` / `Bool` / `Int` / `Real` / `Text` / `Bytes` |

### 输出模型

| 类型 | 说明 |
| --- | --- |
| `CommittedTransaction { txn_id, commit_lsn, schema_versions, changes }` | 完整已提交事务，变更按原序 |
| `DecodedChange { lsn, op, table_id, schema_version, columns, unresolved }` | 按引用版本解码后的行变更；`columns` 按模式声明序，`unresolved` 保留未知列 id 的值，数据不静默丢失 |
| `ResolvedValue { column_id, name, data_type, value }` | 解析到具体列声明的值（`None` 表示该行未携带该列） |
| `Batch { number, transactions }` | 编号批次 |
| `BlockReason` | `Incomplete` / `MissingSchema` 空洞原因 |

### 解码器（`src/decoder.rs`）

```rust
// 新建：每批最多 batch_size 个事务（必须 >= 1）
let mut decoder = Decoder::new(batch_size)?;

// 重开：confirmed_lsn 为已确认位点，acknowledged_batch 为已确认最高批编号，
// next_batch 必须 > acknowledged_batch（通常传 acknowledged_batch + 1）
let mut decoder = Decoder::resume(batch_size, confirmed_lsn,
                                  acknowledged_batch, next_batch)?;

decoder.feed(record)?;            // 喂入一条记录
decoder.feed_all(records)?;       // 顺序喂入多条，遇错即停
decoder.flush();                  // 封存当前未满批次

let batch: Option<Batch> = decoder.next_batch();       // 取下一批（保留，可重复取）
let all: Vec<Batch>      = decoder.pending_batches();   // 全部未确认批次
decoder.acknowledge(batch_number)?;                    // 累计确认，批被移除并推进 confirmed_lsn

let hole: Option<BlockReason> = decoder.first_hole();  // 当前首个空洞
let holes: Vec<BlockReason>  = decoder.holes();        // 全部空洞（按提交位点）

// 观测接口
decoder.open_transaction_count();
decoder.confirmed_lsn();
decoder.acknowledged_batch();
decoder.next_batch_number();
decoder.retained_schema_count();
```

错误类型：

- `FeedError`：非法片号（0）、 finalized 后到达的片段、重复提交位点、模式
  冲突、列 id 重复等；
- `DecoderError`：批大小为 0、确认乱序/未知批次、resume 参数不一致。

### 模块划分

```
src/model.rs    领域类型（输入/输出模型、枚举）
src/error.rs    FeedError / DecoderError
src/registry.rs SchemaRegistry（已提交模式，全版本保留）/ PendingSchemas（事务内待发布）
src/decoder.rs  Decoder 状态机：重组、完整性、解码、排序、空洞、批次、确认
src/lib.rs      重导出与单元测试
tests/          端到端集成测试（见下）
```

## 复现步骤

```bash
# 工具链由 rust-toolchain.toml 固定为 1.95.0
cargo test --offline
```

无第三方依赖，`Cargo.toml` 的 `[dependencies]` 为空，可在完全离线环境构建。

## 验证范围（以真实测试为准）

共 25 个测试，全部由 `cargo test --offline` 实际执行通过，无编译警告。

### 正常路径
- `tests/normal.rs`
  - 交错片段按事务重组、提交位点排序、行变更保持原序；
  - abort 事务及其模式声明被丢弃，不污染全局模式表；
  - 批大小边界封存、部分批 flush、未确认批次重复投递内容不变。
- `tests/stress_interleave.rs`：5 个事务 × 4 片段逐条轮转交错，模式在事务
  未提交期间升级到 v2，验证全部旧事务仍按 v1 解码且顺序正确。

### 边界与非法输入（`tests/boundary.rs`）
- 空提交事务（无行变更）正常发布；
- `Null` 值、未携带列、未知列 id（进入 `unresolved` 而不丢数据）；
- 片号 0、finalize 后片段、重复 finalize、重复提交位点、批大小 0、
  resume 参数矛盾、确认不可回退/未知批次；
- 同一片段承载多条记录（schema + row）按追加处理。

### 交错与模式演进（`tests/interleaving.rs`）
- 旧格式事务在 v2 模式提交后才提交，仍按引用的 v1 解码；改名保持 id、
  删列后同名列获得新 id；
- 单事务内 DDL 与 DML 交错，提交时可解析；
- 三事务片段轮转、提交顺序与启动顺序不同时的重组。

### 故障恢复与空洞（`tests/holes.rs`、`tests/recovery.rs`）
- 缺模式空洞报告表号/版本/位点，并阻断其后所有事务发布；
- 片段不完整（含只有 commit、无任何片段的中途启动场景）列出全部缺失片号；
- 后续流量不能冲掉空洞；多个空洞按提交位点排列；
- 重开后从更早位点重读可**治愈**缺模式空洞；
- 重放跳过已确认提交，未确认批次以原编号、相同内容重新派生，新批次编号
  不回退；abort 与已确认提交不会被重复发布；
- 未确认批次在确认前持续保留，累计确认后才移除并推进 confirmed LSN。

### 单元测试（`src/lib.rs`）
模式注册表多版本保留与幂等重声明、待发布模式拒绝重复列 id、稳定列身份
解码与顺序保持。

## 设计边界与假设

- 解码器为单线程流式状态机；"并发交错"指日志流中不同事务片段的交错，这是
  复制流真实的交错边界。输入记录需按 LSN 递增喂入。
- 一个片段可承载多条记录；片段集合只用于对 `last_fragment` 做完整性检查。
- 模式冲突（同一表/版本号内容不一致）视为日志矛盾，返回 `FeedError` 而非
  静默覆盖。
- 空洞在当前解码器会话内是永久性的；标准治愈方式是按 `BlockReason` 报告的
  位点从更早位置重开解码器并重放日志。
