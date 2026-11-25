# 边界交换机镜像分发器（mirror）

给故障排查与安全留档两个接收端分发同一份流量镜像的 Go 模块。只做防御性的
“按许可裁剪”，不解析、不改写、不重放报文，也不包含任何利用或规避逻辑。

## 运行

```sh
go test ./...          # 全部单测 + 示例 + 模糊测试种子
go test -race ./...    # 竞态检测
go test -cover ./...   # 语句覆盖率
go test -run '^$' -fuzz FuzzDistribute -fuzztime 20s ./...   # 持续模糊
go test -run '^$' -bench BenchmarkDistribute -benchtime 200x ./...  # 基准
```

要求 Go 1.20.2（`go.mod` 中声明 `go 1.20`），无第三方依赖，不联网。

## 公开 API

| 名称 | 作用 |
| --- | --- |
| `Capture` | 一条采集记录：`ID`、`Ingress`（原入口）、`VLANs`（原标签，按外层到内层）、`Frame`（测试帧字节）、`Fields`（各协议头在帧内的字节范围） |
| `FieldRange` | 一个协议头：`Name`、`Depth`、`Start`（含）、`End`（不含）；负数表示该边界资料缺失 |
| `Depth` | `DepthL2`=1、`DepthL3`=2、`DepthL4`=3、`DepthL7`=4；802.1Q/QinQ 标签属于 L2，标签值单独由 `VLANs` 表达 |
| `Receiver` | 一个接收端：`Name`、`Ingresses`（`"*"` 表示任意原入口）、`VLANs`（允许的原标签集合）、`AllowUntagged`、`MaxDepth`（最多保留到哪层） |
| `Config` | 接收端列表，`Validate()` 检查空配置、重名、空入口、非法层深 |
| `Distribute(cfg, capture) Result` | 对一条采集，为每个接收端分别授权并裁剪 |
| `DistributeAll(cfg, captures) []Result` | 逐条独立分发，保持输入顺序，结果之间不共享底层字节 |
| `Result.CopyFor(name)` | 按接收端名取回该条采集的结果 |
| `Copy` | `Allowed`、`Denied`/`DenyDetail`、`Data`（保留的帧前缀）、`Fields`（保留的头名）、`Truncation` |
| `Truncation` | `Reason`、`Field`、`Depth`、`Offset`（= `len(Data)`）、`Detail` |

授权原因：`ingress_not_allowed`、`vlan_not_allowed`、`untagged_not_allowed`。
停止复制原因：`depth_limit`、`missing_bounds`、`unknown_depth`、`invalid_bounds`、
`out_of_bounds`、`gap`、`overlap`、`no_field_data`、`uncovered_bytes`。

## 关键语义

1. **授权只看原入口与原标签。** 中间链路可能改写标签，`Frame` 里的字节完全
   不参与授权判断。测试 `Test_OriginalVLANAndIngressDecideNotFrameBytes` 让帧内
   字节编码 200、采集记录写 300，验证只看 `Capture.VLANs`。
2. **一个接收端都不满足即整体拒绝。** QinQ 帧的每个原标签都必须在
   `Receiver.VLANs` 里；未打标签的帧默认拒绝，需显式 `AllowUntagged`。
3. **两个接收端都能收同一帧时按各自 `MaxDepth` 截取。** 同一条采集可同时
   产出 58 字节（到 L4）和 18 字节（到 L2）两份前缀，互不影响，也不与源帧
   共享底层数组。
4. **只提交能确认合规的已知头部。** 头部必须同时满足：层深合法、不深于
   `MaxDepth`、两端边界齐全、`End > Start`、`Start` 等于当前已确认前缀长度、
   `End` 不越界。第一个不满足的头部即停止，`Truncation` 记录原因、字段、
   深度与已保留长度；其后头部一律不复制。
5. **字段顺序。** 所有 `Start` 已知时按 `Start` 稳定排序；存在负 `Start` 时
   保持输入顺序，让缺失边界立即中止而不是被排序掩盖。
6. **拒绝的接收端也有结果条目**（`Allowed=false`、无字节），便于审计。

## 验证范围（均为本地合成数据）

`go test ./...` 实测通过，`go test -cover ./...` 报告语句覆盖率 100.0%，
`go test -race ./...` 通过；`FuzzDistribute` 种子通过并在 20 秒真实模糊运行中
执行约 159 万次输入、无失败。

覆盖的正常与边界场景：

- 同一帧分发给 L2/L4 两个接收端，各自字节长度、字段列表、截断点；
- 层深恰好等于上限时整帧完整交付、无截断记录；
- 原标签与原入口决定授权，帧内改写后的标签不参与判断；
- QinQ 必须逐个标签命中，只允许外层标签的接收端被整体拒绝；
- 未打标签帧的默认拒绝与 `AllowUntagged` 放行；
- 入口白名单与 `"*"` 通配；
- 边界缺失（`Start` 或 `End` 为负）、`End <= Start`、越界、`gap`、`overlap`、
  未定义层深、完全无字段资料、尾部字节无头部覆盖；
- 字段乱序输入被排序；深于上限的坏头部报 `depth_limit` 而非边界错误；
- 被拒接收端仍出现在结果中，`CopyFor` 对未配置名字返回 false；
- `Distribute` 与 `DistributeAll` 不修改采集记录，也不在副本之间共享字节；
- 多条采集交错、同一接收端对不同标签逐条重新授权。

未覆盖 / 未验证：

- 不做真实抓包、网卡镜像或内核/交换机对接，全部是内存中的合成帧；
- 不判断以太网类型、IP 版本或 TCP 头长度，字段范围由调用方提供并信任其余
  语义；模块只校验范围在几何上是否可确认合规；
- `go vet ./...` 在本环境需交互授权、未能直接执行；改用 `go test -vet=all
  ./...` 跑完全部 vet 检查并通过，`gofmt -l .` 无输出。
