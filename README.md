# objstore — 分片对象存储的快照一致分页列举

用 Go 1.20.2（仅标准库）实现对象存储的分页列举与续页：对象名按字节排序，
调用方可给前缀、分隔符和页大小；分隔符折叠出的共同前缀与普通对象一起占页。
第一页固定对象可见版本（快照），续页在分片拆分/合并（含迁移期读范围重叠）
之后仍读取旧快照；快照过期时显式报错，绝不从新版本静默续。

## 公开 API

包 `objstore`：

| 名称 | 说明 |
| --- | --- |
| `NewStore() *Store` | 创建空存储（单个无界分片），并发安全 |
| `(*Store).Put(key string, value []byte) uint64` | 写入对象，返回新存储版本 |
| `(*Store).Delete(key string) uint64` | 删除对象（打墓碑），旧快照仍可见 |
| `(*Store).Version() uint64` | 当前存储版本 |
| `(*Store).List(req ListRequest) (Page, error)` | 第一页；固定本次列举的快照版本 |
| `(*Store).ListNext(token string) (Page, error)` | 续页；快照过期返回 `ErrSnapshotExpired` |
| `(*Store).Split(boundary string)` | 在边界处拆分分片 |
| `(*Store).BeginSplit(boundary string)` | 开始迁移式拆分：新旧分片读范围重叠 |
| `(*Store).CompleteSplit(boundary string)` | 完成迁移式拆分：退役旧分片 |
| `(*Store).MergeAll()` | 合并所有分片为单个无界分片 |
| `(*Store).Compact()` |  compact 历史版本；更旧的快照此后不可读 |
| `ListRequest{Prefix, Delimiter, PageSize}` | 列举参数；共同前缀与对象都占页大小 |
| `Page{Entries, Token, Snapshot}` | 一页结果；`Token` 为空表示列举结束 |
| `Entry{Kind, Name}` / `EntryObject` / `EntryCommonPrefix` | 页内条目：普通对象或共同前缀 |
| `ErrSnapshotExpired` / `ErrInvalidToken` | 显式错误（可用 `errors.Is` 判定） |

## 语义要点

- **快照固定**：`List` 记录当前版本，所有续页读取该版本；两页之间的新增
  （即使在已发过的共同前缀之下）和删除对本次列举不可见/不可生效，拼接各页
  与一次性扫描固定快照一致。
- **折叠**：键去掉前缀后的余部含分隔符时折叠为共同前缀，整棵子树跳过；
  键恰等于可折叠前缀时，普通对象与前缀分别保留、按名称排序、同名对象在前。
- **续页位置**：令牌只携带快照版本与键空间位置（最后一条目的名字与种类），
  不含分片身份；共同前缀之后用 `prefixEnd`（末非 0xff 字节进位）跳过整棵
  子树，页内页间都不会重复发已作为共同前缀输出的子树。
- **分片无关**：续页在新分片布局下重新解析分片；迁移期重叠分片按键去重，
  写入打到所有覆盖该键的分片。
- **快照过期**：`Compact` 之后旧快照续页返回 `ErrSnapshotExpired`，不静默
  从新版本续。
- **令牌编码**：键与分隔符是任意字节串，令牌用 gob（字节安全）+ base64
  编码；不能用 JSON——它会将非法 UTF-8（如键中的 0xff）替换为 U+FFFD，
  损坏续页位置（本仓库测试 `TestPrefixEndByteBoundary` 覆盖该回归）。

## 复现步骤

```sh
go test ./...            # 全部功能测试
go test ./... -race      # 含并发交错场景的竞态检测
go test ./... -v         # 查看逐条测试
```

环境：Go 1.20.2，无第三方依赖，仅使用本地合成数据。

## 验证范围（测试与场景对应）

正常场景：

- `TestFoldOrdering` — 混合数据集的精确折叠顺序（字面断言），页大小 1..N+1 全扫
- `TestPrefixScoping` — 前缀限定下的对象与共同前缀
- `TestPagedEqualsOneShot` — 嵌套数据集，页大小 1..7 拼接 == 一次性扫描
- `TestPageBoundaryExactlyAtCommonPrefix` — 页边界恰停在共同前缀之后
- `TestEmptyDelimiter` / `TestMultiByteDelimiter` — 空分隔符不折叠、多字节分隔符

边界场景：

- `TestKeyEqualsCollapsiblePrefix` — 键恰等于可折叠前缀：对象与前缀分别保留、
  对象在前，对象/前缀对被页边界任意切开均正确
- `TestPrefixEndByteBoundary` — 0xff 结尾前缀的 `prefixEnd` 进位；子树延伸到
  键空间末尾时干净终止；非 UTF-8 键/分隔符的令牌往返（回归测试）
- `TestEmptyStoreAndNoMatch` — 空存储、无匹配前缀
- `TestInvalidInputs` — 非正页大小、垃圾令牌（`ErrInvalidToken`）

交错场景（题面明确要求的验收条件）：

- `TestShardSplitBetweenPages` / `TestShardMergeBetweenPages` — 两页之间拆分/合并分片
- `TestMigrationOverlap` — 迁移期新旧分片读范围重叠：不重复发条目；迁移中写入
  只出现一次；跨迁移的列举继续读旧快照
- `TestInsertUnderEmittedPrefixBetweenPages` — 两页之间在已发过的前缀下新增对象，
  拼接结果仍等于固定快照的一次性扫描（并用新一次性扫描证明快照确实被固定）
- `TestDeleteBetweenPages` — 两页之间删除对象，旧快照仍返回它
- `TestSnapshotExpiry` — `Compact` 后续页显式返回 `ErrSnapshotExpired`，不静默续
- `TestSnapshotSurvivesCompactionWhenCurrent` — 快照恰为保留版本时续页仍可用
- `TestSplitMergeInsertInterleaved` — 拆分+合并+新增+删除+迁移在同一列举的各页间交错
- `TestConcurrentWritesDuringPaging` — 分页期间并发写入与分片拆分（`-race` 验证）

未覆盖（明确标注）：性能/压测、超大数据集、网络层与真实后端均未涉及；
存储为内存实现，分片操作为测试提供的布局变更原语。
