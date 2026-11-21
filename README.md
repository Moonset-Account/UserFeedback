# objectstore — 分段上传的对象服务（Go 1.20.2）

在一个进程内实现的对象服务：支持分段乱序上传、按段号升序的完成清单结算、幂等重放和并发安全。所有状态保存在内存中，仅使用 Go 标准库，无第三方依赖。

## 工作区与运行环境

- 模块：`example.com/objectstore`
- `go.mod` 声明 `go 1.20`，实际验证使用 Go 1.20.2 darwin/arm64。
- 从空目录开始，仅新增 `go.mod`、`objectstore.go`、`objectstore_test.go`、`README.md`。
- 只操作本地合成数据，不访问网络、账户或系统凭据。

## 公开 API

```go
type Token string                      // 段的标记，内容决定：SHA-256 十六进制摘要
func ContentToken(content []byte) Token // 不写入也能算出某段内容的标记

type SessionID string
type SegmentRef struct { Number int; Token Token }

func New() *Store                      // Store 零值同样可用
func (s *Store) CreateSession(key string) SessionID
func (s *Store) PutSegment(id SessionID, number int, content []byte) (Token, error)
func (s *Store) Complete(id SessionID, manifest []SegmentRef) (int64, error)
func (s *Store) Get(key string) (content []byte, version int64, ok bool)
func (s *Store) Inspect(id SessionID) (SessionInfo, bool)
```

错误值（配合 `errors.Is` 判断）：

| 错误 | 含义 |
| --- | --- |
| `ErrSessionNotFound` | 会话号不存在 |
| `ErrSessionClosed` | 会话已成功完成，拒绝任何再写入（包括同内容重写） |
| `ErrInvalidSegment` | 段号不是正整数 |
| `ErrManifestInvalid` | 清单为空，或段号非严格递增 / 非正 |
| `ErrSegmentMismatch` | 清单中的段缺失，或标记与当前内容不符；具体信息见 `*SegmentMismatchError` |
| `ErrManifestConflict` | 该会话已绑定另一个清单 |

### 语义要点

1. **乱序写入 / 同段替换**：`PutSegment` 可在任意顺序调用；同一会话内重写同一段号会替换该段内容，并返回新内容对应的标记。标记只由内容决定，相同内容在任何会话、任何段号上得到相同标记。
2. **完成只拼清单列出的段**：`Complete` 按清单顺序（必须严格递增）拼接所列段，未列入清单的已上传段被忽略。
3. **逐段校验**：每段都必须存在，且当前内容的标记等于清单中的标记。任何一段不符时，已发布对象与版本号完全不变，旧对象继续可读；会话保持打开，补写正确内容后可重试。
4. **清单绑定**：会话第一次收到结构合法的清单即与其绑定（即使随后结算因段不符而失败）。之后必须用同一清单重试；换清单返回 `ErrManifestConflict`。结构非法的清单会被拒绝且不绑定。
5. **成功后封闭**：`Complete` 成功后会话封闭并释放段缓存，后续 `PutSegment`（即使写入相同内容）返回 `ErrSessionClosed`，不会影响已发布对象。
6. **幂等重放不倒退**：对已完成的会话重送其清单，返回该会话自己发布的版本号，不会重新发布，也不会把对象指回旧版本——即使其他会话已发布了更新版本。
7. **并发**：所有操作由同一把互斥锁串行化。对同一会话的 `Complete` 与同段 `PutSegment` 并发时，结果必然等价于二者之一先执行：要么完成先做、随后的写被拒绝并返回 `ErrSessionClosed`；要么写先做、完成因标记不符而失败且不发布任何版本。
8. **防御性拷贝**：`PutSegment`、`Get` 均复制字节，调用方修改入参或返回值不会影响存储内容。

## 复现步骤

```sh
cd <workspace>
go test ./... -count=1 -v      # 功能测试
go test ./... -count=1 -race   # 并发场景 + 数据竞争检测
```

如需仅编译检查：

```sh
go build ./...
```

## 覆盖的测试场景（`objectstore_test.go`）

正常路径：

- `TestOutOfOrderUploadThenAscendingManifest` — 3/1/2 乱序上传后按升序清单完成，对象内容与版本号正确，封闭会话释放段缓存。
- `TestTokenIsDerivedFromContent` — 同内容跨会话/段号标记相同，不同内容标记不同。
- `TestRewriteSegmentReplacesContent` — 同段重写替换内容，按新标记完成。
- `TestOnlyListedSegmentsArePublished` — 清单外已上传段被忽略。

边界与错误路径：

- `TestStaleTokenMismatch` — 用过期标记结算返回带详情的 `ErrSegmentMismatch`，随后换清单返回 `ErrManifestConflict`，且不发布对象。
- `TestManifestValidation` — 空清单、降序、重复、零、负段号均返回 `ErrManifestInvalid`，不发布、不绑定，会话仍可用合法清单完成。
- `TestMismatchKeepsOldObjectAndRetrySucceeds` — 新会话结算失败后旧对象与版本 1 原样可读；补段后用同一清单重试发布版本 2。
- `TestSessionBindsFirstManifest` — 失败后同清单可重试成功；完成后再送不同清单报冲突；结构非法清单不产生绑定。
- `TestReplayAfterNewerVersionReturnsOwnVersion` — 旧会话清单在新版本发布后重放返回旧版本号，对象仍指向新版本；再发布第三版后结论不变。
- `TestCompletedSessionRejectsWrites` — 完成后新段与同内容重写均被拒绝，对象不变。
- `TestUnknownSessionAndInvalidSegment` — 未知会话与非法段号。
- `TestGetReturnsCopy` — `Get` 返回防御性拷贝。

并发/交错：

- `TestCompleteAndPutSegmentConcurrent` — 200 轮同段写与完成竞争，逐一断言结果属于“完成先”或“写先”两种合法线性化之一，写先时还可补段重试成功。
- `TestConcurrentSessionsPublishSequentialVersions` — 两会话并发完成同一键，得到互不相同的版本 1/2，最终对象为较新版本，重放各自清单均不倒退。

## 验证范围与状态

- 已执行：`go test ./... -count=1 -v` 全部通过；`go test ./... -count=1 -race` 通过。
- 已执行：`gofmt` 格式化。
- **未执行**：`go vet ./...`（本环境需交互审批，未获授权运行），因此不声称 vet 干净。
- 未做持久化、网络层、鉴权与跨进程协调；上述并发保证仅在一个 `Store` 实例、单进程内成立。
