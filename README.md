# heap — 堆表插入的选页与落位

用 Go 1.20 实现的堆表（heap table）插入路径模拟：近似空闲空间目录（按容量档取整）、
页内槽位复用与碎片压紧、以及可暂停、可交错的分步插入。纯内存实现，无外部依赖。

## 模型

- **页（Page）**：定长槽页。行指针（line pointer）从页头向下增长，元组字节从页尾向上
  增长，中间是连续空闲间隙（gap）。删除的记录留下**死元组**（仍占字节，形成碎片）和
  **可复用槽**（行指针可被新记录回收）。
- **压紧（compact）**：把存活元组滑向页尾、回收死元组与搁浅字节，全部空闲合并进间隙。
  **存活记录的槽号不变**（只改偏移），因此已返回的 `Location` 在压紧后仍然有效。
- **空闲空间目录（FreeSpaceDir）**：每页记录一个**容量档**（tier = 空闲字节数 ÷ 档大小，
  向下取整）。档位是保守下界：档值 × 档大小 ≤ 记录时的真实空闲。提示可能因并发写入而
  过时，落位前必须在页锁下按真实状态复核。
- **插入决策**：先按目录选页；拿到页锁后按真实状态决定——间隙够则**直接放入**；间隙不够
  但间隙+碎片够则**压紧一次后放入**；都不够则**修正目录提示、换页重试**。会话会记住
  失败过的页，绝不会因过时提示反复试同一页。

## 公开 API

```go
tb := heap.NewTable(pageSize, tierSize)   // 页大小、目录档大小（字节）

loc, err := tb.Insert(rec)                // 一步插入：选页 + 落位（内部循环换页）
rec, err := tb.Get(loc)                   // 按 Location{Page, Slot} 读活记录
err := tb.Delete(loc)                     // 删除：槽变为可复用，字节变为碎片，刷新目录提示

// 可暂停的分步插入（用于在选页后插入另一笔事务）
s, err := tb.BeginInsert(rec)             // 校验记录能放进空页
p := s.PickPage()                         // 第一步：按目录选页（必要时分配新页）
loc, err := s.Place()                     // 第二步：锁页复核，直接放/压紧放/返回 ErrPageFull
loc, err := s.Finish()                    // 从当前状态循环 PickPage/Place 直到落位

// 观测
pi, err := tb.PageInfo(id)                // 页真实账目：连续空闲/碎片/活/死/未用槽
tier, ok := tb.Directory().TierOf(id)     // 目录当前为某页记录的容量档
id, ok := tb.Directory().Search(need)     // 按需求字节数查目录选出的页
n := tb.Directory().TierBytes(tier)       // 容量档换算为保证的空闲字节
n := tb.PageCount()
```

哨兵错误：`ErrRecordTooLarge`、`ErrPageFull`、`ErrNotFound`、`ErrNotLive`、
`ErrNoPagePicked`、`ErrSessionDone`。

并发约定：`Table`/`Page`/`FreeSpaceDir` 的方法均可并发调用；`InsertSession` 属于单个
写者，不可并发使用。后到写者在页锁下复核真实空闲，不会覆盖先到写者；`Place` 失败时
会把页的真实空闲写回目录并将会话切到别的页。

## 运行

```sh
go test ./...            # 全部测试
go test -race ./...      # 含竞态检测（并发用例）
```

需要 Go 1.20+（开发环境 go1.20.2，darwin/arm64）。无第三方依赖，无需联网。

## 验证范围（本地合成数据，全部实测通过）

| 测试 | 覆盖点 |
| --- | --- |
| `TestInsertAndGetRoundTrip` | 正常插入/读取回环，槽号顺序分配，目录档位随空闲下降 |
| `TestSlotReuseAfterDelete` | 删除后槽号被复用；直接放入不触发压紧；旧记录槽号仍可用 |
| `TestCompactionPreservesSlotNumbers` | 间隙不足、间隙+碎片足够时压紧一次；存活记录槽号与内容不变；第二个被删槽之后仍可复用 |
| `TestInsertSwitchesToFreshPageWhenFull` | 现有页都放不下时分配新页 |
| `TestPausableInsertInterleavedFill` | 选页后插入另一笔事务把页填满：暂停的写者不覆盖、换页落位；最终目录档位（页0=1、页1=5）与 `Search` 选页结果逐项断言 |
| `TestPausableInsertInterleavedCompaction` | 选页后另一写者吃掉连续间隙：暂停的写者压紧一次后仍落在原选页；四条记录逐槽核对 |
| `TestConcurrentWritersSamePageHint` | 两写者拿到同一页提示：后到者不覆盖先到者，且失败一次后不再试同一页 |
| `TestConcurrentInsertsNoLostRecords` | 8 goroutine × 25 条并发插入（`-race` 通过）：位置唯一、无丢失、内容正确 |
| `TestExactFitBoundary` | 恰好填满空页（52 字节）可放；多 1 字节返回 `ErrRecordTooLarge`；满页后下一条去新页 |
| `TestErrorPaths` | 未知页/越界槽/重复删除/空会话 `Place`/会话完成后复用等错误路径 |
| `TestDirectoryTierIsConservative` | 目录档位向下取整，承诺值不超过页真实空闲 |
| `TestFinishResumesFromPausedState` | 暂停后经 `Finish` 恢复完成，等价于 `Insert` |
| `TestMixedInsertDeleteWorkload` | 200 轮插入+删除混合负载后全部活记录可读回 |

## 限制

- 纯内存模拟：无持久化、WAL、事务隔离与可见性；并发正确性由页锁 + 目录提示修正保证。
- 目录只存近似档位，提示可过时；正确性依赖落位前在页锁下按真实状态复核。
- 删除只标记死元组，碎片由后续插入触发的压紧回收（无独立 vacuum）。
