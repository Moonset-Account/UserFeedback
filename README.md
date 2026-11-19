# sealedbuf —— 分页封存缓冲区（Go 1.20.2）

## 背景与需求映射

上传组件把同一块分块缓冲区同时交给**编辑器**（持续修改原缓冲区）和**后台内容检查**
（需要一份检查开始时刻的封存视图）。本包 `sealedbuf` 用固定大小分页 + 引用计数 +
写时复制（COW）实现该语义。

题面交叉约束到实现行为的对应关系：

| 题面要求 | 实现位置 / 行为 |
| --- | --- |
| 带分页的封存视图 | `PagedBuffer` 按 `pageSize` 分页；`View(off, length)` 固定覆盖区间对应的页版本 |
| 检查开始时固定一段字节 | `View` 创建时对覆盖页 `refs++`，封存页对象不可变；之后读视图只走被固定的页 |
| 原缓冲区仍可通过任意重叠切片修改 | `WriteAt`（任意偏移/长度）与 `Edit(off, length, fn)`（scratch 回写）均支持跨页重叠区间 |
| 同一页同一版的两次检查共用一份保留空间 | 两个视图固定同一页版本时持有**同一个 `*page`**；`retention` 按页对象去重，保留数只算一份 |
| 可读到各视图封存内容与实际保留的旧页数 | `View.Bytes/ReadAt/Read/Seek`；`PagedBuffer.RetainedPageCount()` |
| 跨两页的视图只改其中一页，旧页计数只 +1 | COW 仅替换被改且被固定的页；未改页不产生旧版本 |
| 再改同一页不重复保留 | 后继页是私有页（`refs==1`），原地改写，不再进入 `retention` |
| 关闭最后一个引用该版本的视图后释放旧页 | `View.Close` 递减引用，旧页 `refs` 归零时从 `retention` 删除 |

### 引用计数约定

- 活页：`refs = 1（缓冲区本身）+ 固定该版本的视图数`。
- 保留的旧页：`refs = 固定它的视图数`；写入触发 COW 时缓冲区引用转移给新页，
  旧页从活页槽摘除并登记到 `retention`，最后一个视图关闭时 `refs==0` 即释放。
- 内容未实际变化的写入不复制页，避免产生无意义的旧版本。

## 实际入口

- 模块：`feedback-studio/sealedbuf`（见 `go.mod`，`go 1.20`）
- 实现：`sealedbuf.go`
  - `New(size, pageSize int) (*PagedBuffer, error)`
  - `(*PagedBuffer).View(off, length int) (*View, error)`
  - `(*PagedBuffer).WriteAt` / `ReadAt`（实现 `io.WriterAt` / `io.ReaderAt`）
  - `(*PagedBuffer).Edit(off, length int, fn func(scratch []byte)) error`
  - `(*PagedBuffer).RetainedPageCount() int`、`LivePageVersion`
  - `(*View).Bytes / ReadAt / Read / Seek / PinnedPageCount / PinnedPageVersion / Close`
- 测试：`sealedbuf_test.go`，Go 内置 `testing`，全部为真实断言（无占位用例）。

## 验证范围与运行方式

```bash
go vet ./...
go test ./... -race -count=1
```

测试覆盖三类场景：

1. **可接受结果**
   - `TestSealedViewSurvivesOverlappingEdit`：检查固定全量后，编辑器通过跨页
     `Edit`/`WriteAt` 重叠修改；断言封存内容不变、活缓冲区已更新、保留页数准确、
     关闭后归零。
   - `TestSameVersionSharedRetention`：两个视图看到同一页同一版时只保留一份；
     不同时期创建的视图固定不同版本，各自独立保留。
   - `TestViewReaderSeeker`：游标读取、EOF、`Seek` 三种模式。
2. **应被阻止/拒绝的结果**
   - `TestRejectedOperations`：非法页大小/容量、越界 `View/WriteAt/ReadAt/Edit`、
     视图越界读、非法 `Seek`、关闭后使用（`ErrClosed`）、重复 `Close`。
   - 断言被拒绝的操作不会改变保留页数。
3. **边界与交错场景**
   - `TestCrossPageViewPartialAndRepeatedEdit`：跨两页视图只改一页 → +1；重复改同页
     不重复保留；等值写入不保留；再改第二页 → 再 +1。
   - `TestBoundaryRanges`：零长度缓冲/视图、末页短页、页边界单字节视图。
   - `TestCloseReleasesOldPages`：最后一个引用视图关闭才释放；存活视图仍可读。
   - `TestConcurrentEditorsAndCheckers`：多编辑器与多检查器并发交错，`-race` 下验证
     封存内容不可变、保留数不为负、全部关闭后无残留、活页引用计数回到 1。
