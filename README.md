# overlay — 分层文件系统的可嵌入写入层

在只读底层镜像（`LowerImage`）之上提供可写覆盖层（`FS`）。底层文件首次经
**任一名字**被写入时，按底层 inode 身份提升（copy-up）为**一个**上层对象；
其余仍可见的硬链接名与所有已打开句柄随即读到同一对象的新内容。

## 设计要点

- **按对象而非按路径提升**：所有解析到同一底层 inode 的名字共享同一个
  `node`（`lowerFiles` 提升表）。提升只改节点内容指针，从不触碰路径，
  因此复制期间目录改名不可能靠旧路径把文件重新建回来。
- **单次可见提升**：每个节点一把 `promoMu` 串行化 copy-up；并发首次写
  只有一个提升被发布（`FS.Promotions()` 可观测）。
- **失败原子性**：复制先写入旁路块，全部成功后一次交换发布；中途失败
  释放部分块，旧内容与全部可见名字原样保留。
- **回收时机**：对象的引用数 = 可见目录项数（nlink）+ 打开句柄数
  （nopen）。末名取消链接后对象仍被句柄钉住，末次 `Close` 才回收并
  释放后端块（`FS.Reclaimed()`、`MemBackend.LiveBlocks()` 可观测）。
- **目录语义**：目录项懒合并底层内容，删除/改名用 whiteout 遮蔽底层
  名字；目录改名整体移动子树指针。

## 公开 API

```go
// 后端（可停顿、可注入故障的内存块后端）
be := overlay.NewMemBackend(blockSize)
be.SetHook(func(op overlay.Op, id uint64) error { ... }) // 可阻塞/返回错误
be.LiveBlocks() int

// 只读底层镜像（构建后不再修改）
li := overlay.NewLowerImage()
li.Mkdir(path)                 // mkdir -p
li.AddFile(path, data)
li.Link(existing, linkPath)    // 底层硬链接

// 覆盖层
fs := overlay.New(li, be)
fs.Open(path, flags) (*File, error)   // O_RDONLY/O_WRONLY/O_RDWR/O_CREATE/O_TRUNC
fs.Link(old, new) error               // 覆盖层硬链接
fs.Rename(old, new) error             // 文件替换/目录整体移动
fs.Unlink(path) error
fs.Mkdir(path) error
fs.ReadDir(path) ([]string, error)    // 合并并过滤 whiteout 后的目录项
fs.Stat(path) (Info, error)           // Info{IsDir, Size, Nlink}
fs.Promotions() int                   // 已发生的提升次数
fs.Reclaimed() int                    // 已回收的上层对象数

// 句柄
f.Read(p) / f.Write(p) / f.Seek(off, whence) / f.Close()
```

## 运行方法

```sh
go test ./...            # 标准测试
go test -race ./...      # 含竞态检测
```

环境：Go 1.20.2，无第三方依赖。

## 验证范围（全部经实际执行断言）

| 测试 | 覆盖点 |
|---|---|
| `TestPromoteSharesObjectAcrossNamesAndHandles` | 正常场景：经一个名字首次写提升为一个对象，另一硬链接名与提升前打开的句柄读到同一新内容；目录项与 nlink 正确 |
| `TestConcurrentFirstWritePromotesOnce` | 并发边界：两个名字并发首次写（用后端钩子确定性交错），`Promotions()==1`，两个名字内容一致且无撕裂 |
| `TestFailedCopyKeepsOldContentAndNames` | 失败边界：复制第 2 块注入失败，旧内容、全部可见名字保留，`LiveBlocks()==0` 无泄漏，`Promotions()==0`，重试成功且写叠加在旧内容上 |
| `TestRenameDirDuringCopyDoesNotRecreateOldPath` | 交错：复制中途暂停 → 目录改名 → 恢复，旧路径不重建，新路径见新内容 |
| `TestUnlinkReclaimsOnlyAfterLastClose` | 回收时机：末名 unlink 后对象与块仍存活，逐次关闭，末次关闭才 `Reclaimed()==1` 且块归零 |
| `TestInterleavedPromoteRenameUnlinkLastClose` | 故意交错：提升暂停中 unlink 一个硬链接名 + 目录改名，恢复后核对目录项、内容，再 unlink 末名、末次关闭核对回收 |
| `TestNamespaceOps` | 覆盖层硬链接共享对象、跨目录改名、改名替换底层文件并回收、whiteout 与 O_CREATE/Mkdir 重建、目录移入自身子树被拒、unlink 目录被拒 |
| `TestOpenCreateAndTruncate` | O_CREATE 新建、O_TRUNC 提升并清空、目录打开/缺路径/只写句柄读等错误路径 |

实测结果：`go test ./...` 与 `go test -race -count=2 -v ./...` 全部通过
（8 个测试）。`go vet` 因环境权限限制未能单独运行，但 `go test` 默认
开启的 vet 子集（printf、buildtags 等）已随测试通过。

## 已知限制

- 不提供 rmdir；目录改名目标已存在（非自身改名）时返回 `ErrExist`。
- 路径不支持 `..`；句柄上的普通写（非提升复制）不做失败原子化。
