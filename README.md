# raidarray — 四成员 XOR 校验阵列（Go 1.20）

四个成员（member）组成一个逻辑卷：每个条带（stripe）包含 3 块等长数据块和 1 块逐字节 XOR 校验块。
逻辑地址按数据块顺序在条带内推进，写满一个条带后进入下一个条带。成员槽位固定映射：

| 成员 | 内容 |
| ---- | ---- |
| 0 | 数据块 0 |
| 1 | 数据块 1 |
| 2 | 数据块 2 |
| 3 | 校验块 = 块0 XOR 块1 XOR 块2 |

条带大小 = `3 * blockSize`，卷容量 = `stripes * 3 * blockSize`。

## 实际入口

全部为本地库代码，无网络、无外部目标、无 CLI：

| 文件 | 作用 |
| ---- | ---- |
| `go.mod` | module `raidarray`，`go 1.20` |
| `member.go` | `Member`：固定几何的内存块设备（每成员每条纹一块），`NewMember` / `BlockSize` / `Stripes` / `Block` |
| `array.go` | 阵列实现，公开 API 如下 |
| `array_test.go` | 内置 `testing` 验收用例（真实断言，无桩） |

### 公开 API（`array.go`）

- `NewArray(blockSize, stripes int, members []*Member) (*Array, error)`
  校验成员数量、非空、非重复指针、几何一致。
- `(*Array).ReadAt(p []byte, off int64) (int, error)` / `WriteAt(p []byte, off int64) (int, error)`
  `io.ReaderAt`/`io.WriterAt` 语义；跨条带自动拆分；越界写/读返回写入前缀 + `io.EOF`；负偏移报错。
- `(*Array).Fail(index int) error`
  成员下线。第二个成员在重建完成前下线即进入停写状态。
- `(*Array).Replace(index int, replacement *Member) error`
  用新成员替换下线成员并开始逐条带重建；要求恰好一个缺失成员且无进行中的重建。
- `(*Array).RebuildStep() (RebuildProgress, error)`
  只重建下一个尚未重建的条带；两次调用之间卷仍可正常读写。
- `(*Array).RebuildAll() error` — 连续调用 `RebuildStep` 直到完成（便利方法）。
- `(*Array).RebuildStatus() RebuildProgress` / `Missing() []int` / `Writable() bool` / `Healthy() bool`
- `(*Array).BlockSize() int` / `Stripes() int` / `Cap() int64`
- 哨兵错误：`ErrStopped`（两成员缺失，停写）、`ErrUnrecoverable`（条带内多块缺失）、`ErrNoRebuild`；
  读写受阻时返回 `*MissingMembersError`，其中 `Missing` 指出缺失槽位，并可通过 `errors.Is/As` 匹配哨兵原因。

## 语义要点

1. **降级读写**：任意单个成员下线时，读通过 XOR 重建缺失块；写采用读-改-写更新存活数据块与校验块。
2. **缺失数据块的字节级覆盖**：写只涉及缺失块中的几个字节时，先重建整块、改字节，再把新内容记入内存复写日志（`overlay`），
   并同步更新校验块——因此仍满足“只剩一个成员缺失”的可恢复条件，重建后的成员拿到的是最新内容。
3. **重建按条带推进**：`Fail`/`Replace` 后 `RebuildStep` 每次只做一个条带；调用方可在任意两步之间插入一次写。
   未重建条带的写进入 `overlay`，重建到该条带时以 overlay 为准；已重建条带走正常读写路径。
   已重建条带上的写同时落到校验块，保证重建完成后整体自洽。
4. **停写状态**：重建完成前第二个成员下线 → `Writable()` 变 false，`WriteAt` 返回 `ErrStopped` 且 `Missing()`/`MissingMembersError` 指出两个缺失槽位。
   此时已重建条带的三个数据块齐全，仍可读；未重建条带两块缺失，读返回 `ErrUnrecoverable`。
5. **完成后的单盘替换验证**：重建完成后，从真实重建结果克隆出一致成员，依次拿掉 0/1/2/3 中任一成员，整个逻辑卷都能读出相同的最新内容。

## 验证范围

运行：

```sh
go test ./...
go test ./... -race
```

`array_test.go` 中的验收用例：

| 用例 | 覆盖的交叉约束 |
| ---- | ---- |
| `TestLayoutCrossesStripes` | 逻辑地址跨数据块与条带映射；健康卷读写一致；校验块等于三数据块 XOR |
| `TestDegradedReadWithEachMemberDown` | 四个槽位分别下线（数据 0/1/2 与校验 3）时全卷读出一致（可接受结果） |
| `TestPartialWriteToMissingDataBlock` | 只覆盖缺失数据块的 3 个字节，降级读回最新值，校验块随更新 |
| `TestSecondFailureStopsWrites` | 两成员缺失 → 停写（应被拒绝）、错误指出缺失槽位、读报不可恢复、`Replace` 被拒 |
| `TestRebuildInterleavedWrites` | 对每个下线槽位：逐条带重建 + **每两步之间插入一次写**，重建中卷始终可读；完成后依次拿掉任一成员全卷读出一致 |
| `TestWriteBeforeRebuildReachesItsStripe` | 重建完成前写未重建条带的缺失块若干字节；overlay 立即可读，重建后新成员持有最新字节 |
| `TestStopDuringRebuild` | 重建进行到一半（4 条带中完成 2 条）再失去第二个成员 → 停写、`Missing()==[1,3]`、已重建条带仍可读、未重建条带报 `ErrUnrecoverable` |
| `TestWriteAndReadEdges` | 空操作、跨卷尾写/读返回前缀 + `io.EOF`、容量边界、负偏移 |
| `TestReplaceValidation` | 健康成员替换被拒、越界索引、几何不匹配、nil 替换、重建中重复替换、完成后 `ErrNoRebuild` |
| `TestNewArrayValidation` | 零块大小/零条带/成员数错误/几何不匹配/重复指针被拒 |

每个用例都通过 `t.Fatalf` 做真实内容与状态断言，不使用桩或伪造数据；所有输入均为测试内合成的确定性字节模式。

## 限制

- 纯内存实现，进程退出即丢失数据；无持久化格式。
- 非并发安全（构造后由调用方串行使用）。
- 仅支持“同时最多一个成员缺失”的降级工作；两个及以上缺失只做停写与缺失报告，不做恢复。
