# cookiejar — 嵌入式浏览器内存 Cookie 容器

纯 Go 1.20.2 标准库实现（无外部依赖）的线程安全 Cookie 容器，供嵌入式浏览器
上层在收到 HTTP 响应时提交 Set-Cookie 属性，并在发起请求时查询将随请求发送的
Cookie。公共后缀列表由调用方注入，容器本身不联网、不读取任何凭据或外部文件。

## 构建与运行

```sh
go test ./...          # 全部 37 个用例
go test -race ./...    # 竞争检测
go test -cover ./...   # 语句覆盖率（当前 96.0%）
go test -run TestSingleAccessOrderAndLaterEviction -v
```

## 公共 API

| 符号 | 说明 |
| --- | --- |
| `New(psl PublicSuffixList, opts ...Option) *Jar` | 创建容器；`psl` 不能为 nil |
| `(*Jar).SetCookie(req *SetCookieRequest) error` | 提交一条响应属性写入（含删除语义） |
| `(*Jar).Cookies(req *CookieRequest) []*CookiePair` | 查询随请求发送的 Cookie，并刷新命中项访问时间 |
| `(*Jar).Len() int` / `(*Jar).LenForHost(host string) int` | 总量/指定可注册域桶内数量，便于监控与测试 |
| `PublicSuffixList`（接口） | 调用方注入的公共后缀实现，签名 `PublicSuffix(host) (suffix string, known bool)` |
| `NewStaticPublicSuffixList(entries []string)` | 内置内存静态列表，供测试与轻量嵌入场景使用 |
| `Clock`（接口）、`SystemClock`、`FakeClock` | 时间来源抽象；`FakeClock` 支持 `Advance`/`Set` 可控推进 |
| `WithClock(Clock)`、`WithCapacity(int)` | 容器选项；容量按可注册域独立计数，默认 50 |

### 哨兵错误

- `ErrInvalidRequest`：名称/主机/Domain 归属等基础校验失败；
- `ErrSecureOverInsecure`：明文连接写入带 Secure 属性的 Cookie；
- `ErrPublicSuffixDomain`：Domain 属性本身就是公共后缀；
- `ErrShadowedSecure`：明文写入（含删除）会遮住同主机同名、同路径或更具体路径的
  Secure Cookie。

## 关键语义

1. **host-only 与父域 Cookie**：不提供 Domain 属性时仅对当前主机生效；提供时
   要求属性值是主机本身或其父域（RFC 6265 domain-match，忽略前导/尾点与大小写）。
2. **路径完整段边界**：`/app` 匹配 `/app`、`/app/`、`/app/login`，但不匹配
   `/application`；Path 缺省时按 RFC 6265 默认路径算法（取最后一个 `/` 之前）。
3. **Secure 与有效期**：明文请求不回传 Secure Cookie；持久 Cookie 到点失效，
   会话 Cookie 无过期时间；响应携带已到点的持久 Cookie 等价于删除请求。
4. **公共后缀**：仅由注入的 `PublicSuffixList` 决定；Domain 直接落在公共后缀上
   一律拒绝。容量桶按可注册域（eTLD+1）划分，IP 字面量与单标签主机独立成桶。
5. **明文防遮蔽**：明文来源的非 Secure 写入，若与桶内未过期的 Secure 项同名、
   域可在同一主机请求上共同命中、且候选路径等于或处于 Secure 路径的完整段下层
   （即同请求中会排在 Secure 项之前），则拒绝；删除请求同样拒绝；平级路径
   （如 `/a` 与 `/b`）与更上层路径允许独立写入。
6. **容量与逐出**：被拒绝的写入在校验阶段返回，不产生任何副作用（不清理、不
   逐出）。合法写入先清掉桶内过期项，再按 LRU 逐出最久未访问项；访问时间相同
   时按创建顺序（先建先逐出），新写入项不会被自身触发的逐出淘汰。
7. **发送顺序**：路径长度降序（更具体的路径在前）；同路径长度按创建顺序；
   名称作为确定性兜底。一次访问同时命中多项时，各项 `lastAccess` 刷新到同一
   受控时刻。

## 验证范围（本地合成数据，无网络）

- `match_test.go`：域匹配/路径段边界/默认路径/主机规范化/静态后缀列表/
  可注册域分桶/可控时钟，共 7 个用例；
- `jar_test.go`：host-only 与域 Cookie、Domain 归属校验、公共后缀拒绝、
  Secure 来源约束、路径边界发送顺序、默认路径、过期与会话 Cookie、过期写入即
  删除、等价更新保持创建顺序、HttpOnly 过滤、非法请求、桶隔离、IP 字面量，
  共 13 个用例；
- `shadow_test.go`：同路径与更具体路径遮蔽拒绝、平级/上层/非段边界路径放行、
  异名放行、明文删除一并拒绝、可信来源可覆盖、跨子域父域遮蔽、根路径遮蔽所有
  路径、过期 Secure 项不再遮蔽、拒绝写入零副作用，共 10 个用例；
- `eviction_test.go`：LRU 写时逐出、先清过期再逐出、**一次访问同时命中两项的
  发送顺序及后续逐出结果**、同路径长度按创建顺序、同访问时间按创建顺序兜底、
  新项不被自身逐出、8 goroutine 并发竞争，共 7 个用例。

所有时间相关断言均通过 `FakeClock` 确定性驱动；测试只使用本地内存中的合成
主机名与 Cookie，不包含任何利用、攻击或规避载荷。
