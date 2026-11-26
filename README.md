# sparseblockvol — 稀疏块卷的版本化映射层

纯 Go（仅标准库，Go 1.20.2）实现的本地持久化逻辑块 → 物理块映射层，面向多快照、
多对齐规则并存的稀疏块卷场景。

## 设计模型

- **物理块内容寻址**：物理块 id 为块内容的 SHA-256（hex）。不同快照、不同卷中
  内容相同的块自动共享同一份物理文件，按首两位 hex 分目录存放于 `blocks/`。
- **引用计数按需重建**：不信任盘上的计数。每次打开与每次提交后，通过遍历所有卷、
  所有已发布版本的映射重新统计引用；`Audit` / `Reclaim` 据此识别和回收孤儿物理块。
- **每卷独立块大小**：`CreateVolume(id, blockSize)` 要求正的 2 的幂。对齐规则升级后，
  旧卷继续用旧块号，新卷可用新块大小，二者共存。
- **版本不可变**：写事务基于当前 head 复制出工作视图，提交时生成新的不可变版本；
  旧版本（含命名快照）始终可读，只能显式删除未被快照钉住的非 head 版本。
- **三种块状态可区分**：
  - `mapped`：指向物理内容块；
  - `zero`：显式写入的已知零段，不占物理块；
  - `hole`：从未写入或被打洞（`PunchHole`）的未分配范围。
  零段和洞读取都返回零字节，但 `LookupEntry` / `Extents` 能区分二者。
- **部分写读-改-写（RMW）**：写可以只覆盖某块中间；受影响块先按事务视图物化，
  再覆盖对应字节，块边缘的未改字节得以保留。事务内已暂存但尚未提升的块通过内存
  暂存表参与后续 RMW，避免读不到新内容。
- **崩溃恢复**：
  - 写事务：载荷先写入私有 `pending/txn-<id>/` 并落描述文件，再提升块、原子替换
    `meta.json`。崩溃重开时，未提交的暂存事务整体回滚；已完成元数据切换的事务
    保留新版本，仅清理暂存目录。
  - 合并：完整变更计划与暂存块先写入 `merge/<vol>.json` 日志，再提升并发布。
    日志记录父版本、新 head、变更列表和提交标记，重开时按日志确定性幂等回放，
    保证旧快照可读、版本不重复。
- **注入式中断测试**：`Store.SetCrashPoint` / `MergeOptions.CrashPoint` 支持
  `txn.after_stage`、`txn.after_meta`、`merge.after_stage`、`merge.after_meta`
  四个中断点，命中时返回 `ErrInjectedCrash` 并保持该点的真实磁盘状态。

## 磁盘布局

```
root/
  meta.json              全部卷/版本/映射元数据（临时文件 + rename 原子替换）
  blocks/ab/<sha256>     内容寻址物理块
  pending/txn-<id>/      写事务私有暂存区（meta.json + blocks/）
  merge/<vol-id>.json    合并回放日志
  merge/<vol-id>/blocks/ 合并暂存块
```

## 公开 API（`package sparseblockvol`）

| API | 说明 |
| --- | --- |
| `Create(root) (*Store, error)` | 初始化空存储 |
| `Open(root) (*Store, RecoveryReport, error)` | 打开并执行崩溃恢复，返回修复报告 |
| `Store.CreateVolume(id, blockSize)` / `Volume(id)` / `VolumeIDs()` / `DeleteVolume(id)` | 卷生命周期 |
| `Volume.Begin() (*Txn, error)` | 开始 RMW 事务 |
| `Txn.WriteAt(data, off)` / `WriteZero(p, off)` / `PunchHole(off, len)` / `Abort()` / `Commit() (*View, error)` | 事务写、零段、打洞、提交 |
| `Txn.SetSnapshot(name)` | 提交时把新版本钉为命名快照 |
| `Volume.Head()` / `Version(id)` / `Snapshot(name)` | 获取只读 `View` |
| `Volume.Versions()` / `SnapshotNames()` / `TagSnapshot` / `DeleteSnapshot` / `DeleteVersion` | 版本与快照管理 |
| `View.LookupEntry(lba)` | 查询单块状态（mapped/zero/hole） |
| `View.Extents()` / `MappedRanges()` | 按字节偏移枚举合并后的区段（含洞与零段） |
| `View.ReadAt(p, off)` | 读取，洞/零段返回零字节 |
| `Volume.Merge(src *View, opts MergeOptions) (headID, error)` | 跨卷（可跨块大小）合并，带日志与回放 |
| `Store.Audit()` / `Reclaim()` | 审计孤儿/缺失物理块；回收不可达块 |
| `Store.SetCrashPoint(point)` | 测试用崩溃注入 |

## 运行方法

```sh
go test ./...            # 运行全部测试
go test ./... -race -v   # 带竞态检测的详细运行
```

无外部依赖，不需要联网；所有测试使用 `t.TempDir()` 合成本地数据，运行结束自动清理。

## 验证范围（均有断言且实际执行）

- 正常路径：多卷不同块大小（4B / 8B / 4096B）、两次写、跨块边界的部分重叠范围、
  块中间覆盖的 RMW 边缘保持、写后扩容与读语义。
- 内容共享：相同内容块只产生一份物理文件，引用计数随快照/版本变化正确增减；
  重写相同内容不新增物理块。
- 零段 vs 洞：`LookupEntry`、`Extents` 对 mapped/zero/hole 的区分；二者读取均为零。
- 版本与快照：旧快照在后续写入、合并、回收后仍字节级可读；快照钉住的版本禁止删除。
- 中断恢复：
  - 写事务暂存后崩溃 → 重开回滚，映射与块目录不变；
  - 写事务元数据切换后崩溃 → 重开保留已提交版本并清理暂存；
  - 合并暂存后崩溃、合并提交标记后崩溃两条路径 → 重开放映日志，head 内容与
    预期字节一致，重复打开为幂等空恢复；
  - 恢复报告包含回滚事务、续做合并、孤儿块与缺失块。
- 回收：删除卷/未钉版本后 `Audit` 报告孤儿块，`Reclaim` 精确删除且不影响仍被
  引用的块；手工植入孤儿块可被识别并回收。
- 交错：不同对齐卷之间带非对齐目标偏移的合并，源侧 mapped/zero/hole 正确翻译为
  目标块大小下的 RMW 变更。

## 已知边界与取舍

- 单存储实例通过互斥锁串行化写路径；`View` 为不可变快照读，不受后续写入影响。
- `ReadAt` 越过卷大小按零填充返回，不自动扩容；扩容由写路径负责。
- 元数据整体存于一个 `meta.json`，适合题面规模的合成验证，未针对超大规模卷做分片。
- 崩溃注入是测试钩子，生产调用不应设置 `CrashPoint`。
