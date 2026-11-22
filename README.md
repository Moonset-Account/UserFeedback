# ssiarbiter — 可序列化提交裁决器

给内存关系存储使用的可序列化（SSI 风格）提交裁决器。裁决器维护事务
历史的**直接串行化图（DSG）**，仅在接纳候选提交会形成环时驳回该提交，
并返回包含事务 ID、键/区间与依赖种类的见证环。

仅依赖 Go 1.20.2 标准库，无外部依赖。

## 依赖模型

| 依赖种类 | 常量 | 边方向 | 形成条件 |
| --- | --- | --- | --- |
| 写-读依赖 | `DepRead` (`read-dependency`) | 写者 → 读者 | 读者在快照中实际看到写者提交的行版本 |
| 读-写反依赖 | `DepAnti` (`anti-dependency`) | 读者 → 写者 | 后来的写者改变了读者已形成的查询结果（含空读幻影、行消失、投影列变化） |
| 写-写依赖 | `DepWrite` (`write-dependency`) | 先提交写者 → 后提交写者 | 两个写者覆盖同一主键，按提交先后排序 |

反依赖的判定基于**查询结果是否改变**，而不是行是否被触碰：

- 空读（结果集为空）也会记录为 `ReadRecord`；之后插入满足条件的行 →
  幻影反依赖；
- 删除读者看到的行、或把行改成不满足谓词 → 行从结果集消失 → 反依赖；
- 只改投影外且非谓词的列 → 投影结果逐列相同 → **不产生**反依赖（对照场景）。

驳回只在环检测命中时发生：候选边先在临时图上做 DFS，无环才安装；被驳回
事务的边和版本不会进入裁决器。

## 公开 API

```go
type TxnID string
type DepKind string // DepRead / DepAnti / DepWrite
type Row map[string]string // nil 表示删除墓碑

type ReadSpec struct {
    TxnID      TxnID
    StartKey   string            // 点读只填 StartKey
    EndKey     string            // 非空时为闭区间 [StartKey, EndKey]
    Predicate  map[string]string // 列等值合取
    Projection []string          // 空表示整行
    Snapshot   uint64
}

type ReadRecord struct {
    Spec     ReadSpec
    Observed map[string]Row // 空读是非 nil 的空 map
}

type WriteIntent struct {
    Key    string
    Before Row // 快照可见的旧版本，nil 表示原本不存在
    After  Row // 新版本，nil 表示删除
}

type Edge struct {
    From, To TxnID
    Kind     DepKind
    Scope    string // key=<k> 或 range=[lo,hi]
}

type Decision struct {
    Committed bool
    RejectSeq uint64 // 候选本应获得的提交序
    Cycle     []Edge // 驳回时的见证环，起止均为候选事务
}

type GCStats struct {
    Watermark         uint64
    ReclaimedTxns     int
    CompactedKeys     int
    CompactedVersions int
}

func New(seed map[string]Row) *Arbiter
func (a *Arbiter) Begin(id TxnID, snapshot uint64) error
func (a *Arbiter) Read(spec ReadSpec) (map[string]Row, error)
func (a *Arbiter) Commit(id TxnID, writes []WriteIntent) (Decision, error)
func (a *Arbiter) Abort(id TxnID) error
func (a *Arbiter) Edges() []Edge
func (a *Arbiter) Reads(id TxnID) ([]ReadRecord, error)
func (a *Arbiter) Collect() GCStats
func (a *Arbiter) Current() uint64
```

### 回收语义（`Collect`）

- 水位线 = 最老活跃事务的快照；没有活跃事务时为当前最新提交序。
- 回收只清理**已结束事务的行载荷**与每键版本链的过期版本（保留水位线
  处的压缩基准，老快照可见性不受影响）。
- 图节点、边（含见证）、读记录一律保留：回收后仍可裁决持有更老快照的
  活跃事务，重放同一历史仍得到同一见证。

## 复现步骤

```sh
go version          # 要求 go1.20.2
go test -race -v ./...
```

## 验证范围

全部断言为真实功能断言（无预置输出对照），共 11 个测试：

| 测试 | 场景类别 | 验收点 |
| --- | --- | --- |
| `TestThreeDisjointKeysCloseRing` | 题面主场景 | 三笔互不写同一主键的事务，仅靠空读反依赖闭成 t3→t1→t2→t3 环，第三笔被驳回；见证环每条边带事务 ID、键与 `anti-dependency` 种类；驳回边不安装 |
| `TestUnrelatedColumnNoConflict` | 题面对照 | 只改投影外/谓词外的列，查询结果不变 → 无反依赖、允许提交；反向验证改投影列确实产生反依赖 |
| `TestWriteWritePlusAntiDependencyTwoNodeCycle` | 交错 | 同键 WW 约束与 RW 反依赖组成两节点环，见证同时包含两类边；驳回后 `Abort` 生效 |
| `TestEmptyReadPhantom` | 边界 | 空读被记录（非 nil 空 map），后续插入形成幻影反依赖 |
| `TestGCOldSnapshotActiveTxn` | 故障恢复/边界 | 活跃事务持快照 0 时回收受保护；回收（含老事务结束后的二次回收）后老事务仍可正确裁决并被驳回，压缩后头部快照可见性正常，图与见证保留 |
| `TestReplaySameWitnessWithAndWithoutGC` | 重放 | 同一历史三次重放（含穿插 GC）见证环逐边一致 |
| `TestRangeBoundariesAndPredicate` | 边界 | 闭区间端点、谓词过滤、区间外插入、谓词内插入、删除观察行的反依赖判定 |
| `TestWriteReadDependency` | 正常 | 读到已提交版本形成写者→读者边 |
| `TestConcurrentInterleaving` | 并发交错 | 13 个 goroutine 并发 Begin/Read/Commit/Abort（`-race` 下通过），不相交键全部提交，共享键串行化一致 |
| `TestAbortedReaderLeavesNoEdges` | 边界 | 已中止事务的读不属于串行化历史，后续写不再从它派生反依赖边，且图中无其出边 |
| `TestValidationErrors` | 边界 | 空 ID、重复 ID、空键、倒置区间、重复 Abort/Commit、同事务重复写同一键均被拒绝 |

验证命令与结果：

- `go test -race -v ./...` — 11/11 PASS；
- `go test -race -count=2 ./...` — 重复 2 轮全部 PASS；
- `gofmt -l .` — 无输出（格式合规）。

未执行项：无。所有列出的测试均已在本地 Go 1.20.2 darwin/arm64 实际运行。
