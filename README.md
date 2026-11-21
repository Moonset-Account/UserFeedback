# incview — 增量物化视图（机房聚合）

用一个 Go 包实现“事实表 ⋈ 设备维表，按机房聚合”的**增量物化视图**。
事实表按行号标识一行，携带设备编号和整数耗时；维表把设备映射到机房。
视图按机房保存 `行数 / 总耗时 / 最小耗时`，每次事务提交只向读者推送**净变化**的分组。

## 公开 API

```go
v := incview.NewView()

tx := v.Begin()                       // 开启写事务（同一时刻只有一个）
tx.PutFact(row, device, duration)     // 插入/替换一条事实（行号是身份，可借此改设备或改耗时）
tx.DeleteFact(row)                    // 删除一条事实行
tx.SetDevice(deviceID, room)          // 新增或移动设备映射
tx.DeleteDevice(deviceID)             // 删除设备映射
res := tx.Commit()                    // 原子提交，返回本事务的净变化与遍历行号
tx.Rollback()                         // 放弃事务（或出错时调用）

v.Group(room)                         // (Group, bool)，机房不在视图时 bool=false
v.Groups()                            // 所有非空机房快照，按机房名排序
v.Fact(row) / v.Facts()               // 读事实表（含未映射的事实）
v.Device(id)                          // 读设备当前机房
v.FactRowsForDevice(id)               // 某设备当前事实行号（诊断用）
```

关键类型：

```go
type Fact  struct { Row int; Device string; Duration int }
type Group struct { Room string; Count int; Total int64; Min int }

type Delta struct {
    Room         string
    Before       Group
    BeforeExists bool   // 提交前该机房是否在视图中
    After        Group
    AfterExists  bool   // 提交后该机房是否仍非空（false = 空组退出视图）
}

type CommitResult struct {
    Deltas      []Delta  // 仅含净变化的分组，按机房名排序
    VisitedRows []int    // 本次维护遍历过的事实行号，升序去重
}
```

### 语义要点

- **内连接**：事实的设备没有映射时，该事实留在事实表里，但不进入视图；映射恢复后重新计入。
- **净变化**：只比较事务提交前与提交后的聚合。重写同值、把设备 SetDevice 到原机房等无净变化的操作不会产生 `Delta`。
- **空组退出**：机房最后一行离开视图时，`AfterExists=false`，之后 `Group(room)` 返回 `false`。
- **一次结清**：同一事务里既移动设备机房、又删除该设备一条事实时，旧机房与新机房各只产生**一个** `Delta`，取净效果。若旧机房还有其它设备持有同样的最小耗时，旧机房的 `Min` 保持不变。
- **遍历范围**：维护只访问本事务触及设备的事实行。单个设备换组只遍历该设备自己的行，不扫描其它设备的事实。`VisitedRows` 即维护实际读过的行号。
- **并发**：事务串行；读者可与提交并发，只能看到已提交状态。读取接口持读锁。

## 复现与验证

仅使用 Go 1.20.2 与标准库，无第三方依赖：

```sh
go version          # go1.20.2
go test ./... -count=1
go test ./... -count=1 -race   # 并发读写检查
```

文件：

| 文件 | 内容 |
| --- | --- |
| `view.go` | 实现（包 `incview`） |
| `view_test.go` | 定向功能测试 + 并发读测试 |
| `random_test.go` | 随机事务对拍：每步与全量重算比较 |
| `example_test.go` | 可执行示例（`go test` 会校验输出） |

## 验证范围

已覆盖的正常 / 边界 / 交错场景：

- 正常：种子数据聚合正确；只推送发生净变化的分组；单条事实改耗时。
- 边界：最后一行删除导致空组退出；无净变化不产生增量；未映射事实留表不入视图、映射恢复后回补；事务内插入后又删除/删除映射，`Rollback` 后不可见；参数校验（非法行号、空设备/机房、删除不存在的行/映射、事务已结束）；负数耗时与重复最小值：删掉一个最小值后仍保留该最小值，删掉最后一个才重算（`TestNegativeDurationsAndMinRestore`）。
- 交错：**设备从东区改到西区并在同笔事务里删掉它的一条最低耗时记录**，两边一次结清且东区保留其它设备同样的最小值（`TestMoveDeviceAndDeleteItsMinimumTogether`）；设备换到只剩单行的机房；两台设备互换机房并同时删除/插入/改值。
- 遍历范围：`TestSingleDeviceChangeDoesNotVisitUnrelatedFacts` 断言 51 行无关事实不被遍历；对拍测试逐步校验 `VisitedRows` 不越出本事务触及的设备。
- 并发：`TestReadersSeeCommittedStateOnly` 验证未提交不可见；`TestConcurrentReadersDuringCommits` 在 `-race` 下并发读写。
- 对拍：`TestRandomizedAgainstFullRecompute` 跑 400 步随机事务（含移动设备、删除映射、增删改事实，耗时取 `[0,50)`），每步断言增量视图 == 全量重算、`Deltas` 恰为真实变化的分组、`VisitedRows` 均为允许行。

未覆盖 / 明确不做的内容：

- 不做持久化、WAL、崩溃恢复；状态仅在内存中。
- 不支持多版本快照或跨事务的长读。
- 不做 SQL 解析；本包直接以 Go API 表达事实表/维表操作。
- 未测量大规模性能，只做了正确性验证。
