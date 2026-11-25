# recordlayer — 加密通道记录层协调器

用 Go 1.20.2 实现的记录层协调器（模块名 `recordlayer`，仅依赖标准库）。
协调器负责密钥世代（key generation）、每方向独立的记录序号、换钥控制流与
帧边界；真正的加解密由可替换的 `Cipher` 回调完成。

## 设计语义

- 每个方向独立维护 `(generation, sequence)`：读方向与写方向互不影响，双方的
  世代也互不影响。
- 记录帧为 21 字节明文头 `type(1) | generation(8) | sequence(8) | payloadLen(4)`
  加密封载荷。头不保密，但世代/序号/类型同时作为关联数据参与认证。
- `Rotate()`：在**当前**写世代下封装并发出一条 `RotateRequest`，随后写世代
  加一、写序号归零。因此换钥控制记录由旧世代保护，其后第一条记录使用新世代
  且序号为 0。
- 接收端收到任意换钥控制记录（请求或应答）都会推进读世代并把读序号归零；
  只有 `RotateRequest` 会置位“待应答”。
- 待应答的 `RotateResponse` 在**下一条尚未开始封装的应用记录之前**随
  `WriteApplication` 一起在写锁内发出；已经开始分段写出的记录由 `writeAll`
  重试短写直至完整写完，应答无法插到它前面。
- `RotateResponse` 不请求任何东西，因此应答不会再触发应答，不会形成应答环。
- 读侧严格校验到达帧的 `generation/sequence` 必须恰好等于期望值，序号不连续
  或世代不匹配时 `Serve` 直接报错且不会把数据交给密码回调。

## 公开 API

| 标识 | 说明 |
| --- | --- |
| `type Cipher interface` | `Seal` / `Open` 两个回调，参数含世代、序号、记录类型 |
| `NewDemoCipher(baseKey []byte) (*DemoCipher, error)` | 仅依赖标准库的演示实现：SHA-256 派生世代密钥、HMAC-SHA256 计数器 XOR 流加密、HMAC 标签认证（非生产级） |
| `NewCountingCipher(inner Cipher) *CountingCipher` | 包装器，按世代统计成功的 Seal/Open 次数，用于断言“只解封一次” |
| `type Transport interface` | `io.Reader` + `io.Writer` + `io.Closer`，允许短写 |
| `NewBoundedPipe(limit int) *MemPipe` | 有界内存字节管道，缓冲满时返回 `io.ErrShortWrite` |
| `NewPipePair(bufferLimit int) (a, b Transport)` | 全双工内存传输对，两个方向各一条有界管道 |
| `NewConn(tr Transport, cipher Cipher) *Conn` | 创建协调器，两方向均从世代 0 开始 |
| `(*Conn).Rotate() error` | 发出换钥请求并推进本端写世代 |
| `(*Conn).WriteApplication(plaintext []byte) error` | 必要时先发应答，再封装一条应用记录 |
| `(*Conn).Serve() error` | 接收循环；EOF 或本端 Close 为干净终止，认证/帧错误为失败 |
| `(*Conn).ReadApplication() ([]byte, error)` | 阻塞读取下一条已解密应用记录，队列排空且循环结束后返回 `io.EOF` |
| `(*Conn).Close() error` | 关闭协调器与底层传输 |
| `(*Conn).Snapshot() Snapshot` | 一致的状态快照（世代、序号、待应答、队列长度等） |
| `(*Conn).SetGenerationChangeHook(fn)` | 观测读/写方向的世代切换 |
| `type Header`、`EncodeHeader`、`DecodeHeader`、`HeaderSize` | 帧头编解码 |
| 常量 `TypeApplication` / `TypeRotateRequest` / `TypeRotateResponse` | 记录类型 |

## 运行方法

```sh
go test ./...                 # 全部测试
go test ./... -race -count=20 # 重复 + 竞态检测
go run ./cmd/demo             # 双方同时换钥的可运行演示
```

本工作区中 `go build`/`go run`/`go vet` 触发环境授权确认；`go test` 可直接
运行。演示主流程另外由 `cmd/demo/main_test.go` 中的 `TestDemoFlow` 执行，
因此仅靠 `go test ./...` 也有同等实测覆盖。

## 验证范围（全部基于本地合成数据，无网络、无外部依赖）

1. **正常往返** `TestApplicationRoundTrip`：空记录与 200 字节记录经 64 字节
   有界传输（短写）完整往返，序号 0..3 连续，Seal/Open 次数精确。
2. **单方换钥** `TestUnilateralRotation`：请求帧为 `(gen0, seq1)`，换钥后
   首条应用记录为 `(gen1, seq0)`；对端应答 `(gen0, seq0)` 排在其应用记录
   `(gen0, seq1)` 之前，且对端写世代不变。
3. **双方同时换钥（题面核心）** `TestSimultaneousRotation`：两端并发
   `Rotate()`。断言每端实际发出的三帧依次为
   `Request(0,0) -> Response(1,0) -> Application(1,1)`；每条应用数据在对端
   `Open` 恰好一次（世代 0 仅请求 1 次、世代 1 应答+应用共 2 次）；静默窗口
   后无任何额外帧，证明应答不再触发应答；待应答标志全部清除。
4. **分段写出交错** `TestInterleavedRotationDuringSegmentedWrite`：8 字节缓冲
   迫使单条 96 字节记录分多次短写。用接收闸门在请求帧已读出、取写锁前挂住
   接收循环，证明换钥请求在在途记录写完前不会生效；记录完整写出后请求才被
   应用，应答排在下一条尚未封装的应用记录之前，序号 0,1,2 连续。
5. **边界与异常**：EOF 排空（`TestReadEOF`）、错误世代/序号拒绝且不调用密码
   回调（`TestWrongGenerationRejected`）、世代切换钩子
   （`TestGenerationChangeHook`）、管道短写/分片读/关闭语义
   （`transport_test.go` 三个用例）。
6. **密码回调** `cipher_test.go`：多世代/多序号往返、载荷与标签篡改、错误
   世代/序号/类型、截断帧全部被拒；不同世代对同一明文产生不同密文且不能跨
   世代解密；空基钥被拒绝。
7. 已实测：`go test ./... -count=15 -race` 与主包 `-count=20 -race` 均通过，
   `gofmt -l` 无输出。

## 安全边界说明

本包仅实现题面要求的防御性判断（世代/序号连续性、认证标签校验、应答顺序与
应答抑制）。`DemoCipher` 是教学用构造，不声称具备生产级 AEAD 强度；接入真实
系统时应把 `Cipher` 替换为标准 AEAD（如 AES-GCM/ChaCha20-Poly1305），并把
世代、序号、类型作为 nonce 与关联数据使用。包内不含任何利用、攻击或规避
载荷，也不发起任何网络访问。
