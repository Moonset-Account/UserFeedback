# gateway — 按逻辑时间驱动的双桶放行队列

为网关实现的放行队列组件：每个请求同时消耗一个**租户桶**和一个**路由桶**，
每个桶有容量与每毫秒补充量，请求带整数成本。纯本地库，无网络、无外部依赖。

## 语义

- **放行即扣齐**：放行时一次性扣减租户桶与路由桶的额度；等待与取消均不预扣。
- **队首阻塞**：较早未放行的请求挡住与它共享任一桶的后来者；
  使用完全独立桶的请求可先走。
- **同时刻裁决**：同一逻辑时刻按入队号（提交顺序，从 1 单调递增）裁决。
- **立即退回**：单笔成本超过任一相关桶容量时，提交即被拒绝且不入队。
- **可查询**：每次放行的逻辑时刻可通过 `Request.AdmittedAt()` 查询。
- **重判时机**：提交、队首/任意等待者取消、时间推进（独立桶补满）时都会
  重新扫描队列判断谁可开始。

## 实际入口

源码：`gateway.go`（包 `gateway`，模块 `gateway`，Go 1.20）。

| API | 说明 |
| --- | --- |
| `NewGateway(tenants, routes map[string]BucketSpec)` | 用桶规格创建网关，桶在逻辑时刻 0 满容量启动 |
| `Submit(at, tenant, route, cost) (*Request, error)` | 在逻辑时刻 `at` 提交申请；超容量立即退回 `ErrCostExceedsCapacity` |
| `Cancel(at, id) (bool, error)` | 取消等待中的请求并触发重判 |
| `AdvanceTo(at) error` | 推进逻辑时间并触发重判（桶按 `RefillPerMs` 惰性结算、封顶容量） |
| `Request.AdmittedAt() (int64, bool)` | 查询放行时刻 |
| `TenantAvailable` / `RouteAvailable` / `Pending` / `Now` | 观测当前状态 |

防御性校验：非正成本（`ErrInvalidCost`）、未注册桶（`ErrUnknownTenant` /
`ErrUnknownRoute`）、逻辑时间倒退（`ErrTimeRegression`）、非法桶规格
（容量 < 1 或补充量 < 0 在构造期报错）。

## 验证范围

测试入口：`gateway_test.go`，使用 Go 内置 `testing`，全部含真实断言。

```
go test ./...
```

覆盖的交叉约束：

- **可接受结果**：双桶额度充足时立即放行、一次扣齐、放行时刻可查
  （`TestImmediateAdmissionDeductsBothBuckets`）。
- **应被拒绝的结果**：成本超过任一相关桶容量立即退回、不入队、不扣额
  （`TestOversizedCostRejectedImmediately`）。
- **边界**：成本恰等于容量可放行、超 1 即拒（`TestCostExactlyAtCapacityBoundary`）；
  补充量差 1ms 不够、恰好补到成本才放行（`TestRefillBoundaryExactFit`）。
- **交错场景**：共享桶队首阻塞 + 独立桶先行 + 等待不预扣 + 补满后重判
  （`TestSharedBucketBlockingAndIndependentBypass`）；
  队首取消后重判且取消不预扣（`TestCancelHeadReevaluatesQueue`）；
  同一时刻按入队号裁决（`TestSameTimeOrderingByEnqueueSequence`）。
- **防御性校验**：非法成本 / 未知桶 / 时间倒退 / 非法桶规格
  （`TestDefensiveValidation`、`TestInvalidBucketSpec`）。

当前状态：`go test ./...` 全部 9 个用例通过（`go test` 自带 printf、buildtags
等 vet 子集检查）。单独的 `go vet ./...` 命令在本次环境中未获执行权限，未运行。
