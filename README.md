# podresize — 原地纵向扩容协调器

为集群控制器实现的 Pod 原地资源调整协调过程。典型场景：一个 Pod 从
**3 CPU / 5 内存** 原地调整到 **5 CPU / 3 内存**，准备期间节点至少要保留
**5 CPU / 5 内存**（每一项取旧值与新值的较大者）。

## 协调语义

1. **两阶段、按代次（generation）握手。** 每个带版本的调整请求产生一个新
   代次。节点对 CPU、内存分别准备；只有同一代次的两项都回报 Ready，Pod 的
   对外可见规格才**一次性原子切换**。
2. **准备期按 `max(旧, 新)` 占用每一项。** 即使某一项是缩容（如内存 5→3），
   在同代次切换确认（apply 回执）之前仍按旧的较大值占用，已减下来的项也不
   提前释放。
3. **新请求可替换未完成请求。** 旧代次进入 Revoking，但其预留继续计入节点
   容量，直到节点确认撤销（revoke 回执）——同一节点的其他 Pod 无法抢占尚在
   撤销中的容量。
4. **重放旧确认不能推进新请求。** 携带旧代次/未知代次的回执通过返回值中的
   `View.Stale` 标记，状态不发生任何变化，也不作为错误抛出。
5. **持久化与重启恢复。** 每次状态变更都通过 `Store` 落盘完整快照；重启后
   从快照重建所有预留、待确认动作与可见规格。
6. **已过切换屏障的代次不可撤销。** 处于 `Applying` 的代次其规格已生效，新
   请求不会撤销它，仍由 apply 回执收尾。

## 公开 API（包 `podresize`，仅依赖 Go 标准库）

### 类型

| 类型 | 用途 |
| --- | --- |
| `Resources{CPU, Memory int}` | 资源量（整数单位，仅比较/加减） |
| `ResizeRequest{PodUID, Version, Target}` | 带版本的调整请求，版本须严格递增 |
| `PrepareReceipt{PodUID, Gen, Resource}` | 节点确认某代次某项已准备好（`CPU`/`Memory`） |
| `ApplyReceipt{PodUID, Gen}` | 节点确认已切换到该代次规格 |
| `RevokeReceipt{PodUID, Gen}` | 节点确认已撤销被替换的代次 |
| `View` / `NodeView` / `PodView` / `GenView` | 每一步的已用量、对外可见规格、可继续动作 |
| `Action{Kind, Resource, Gen}` | 可继续动作：`Prepare` / `Apply` / `Revoke` / `None` |
| `Snapshot` | 持久化记录（节点、Pod、代次全量状态） |

### Store（持久化边界）

- `Store` 接口：`Save(*Snapshot) error`、`Load() (*Snapshot, error)`。
- `MemoryStore`：进程内快照，用于测试，同样走完整序列化/恢复路径。
- `FileStore`：单 JSON 文件，临时文件 + 原子 rename，崩溃不会留下半写文件。

### Controller 方法

```go
func NewController(store Store) (*Controller, error)          // 启动并从快照恢复
func (c *Controller) AddNode(name string, cap Resources) error
func (c *Controller) AddPod(uid, node string, spec Resources) error
func (c *Controller) Submit(req ResizeRequest) (View, error)  // 接纳/替换请求
func (c *Controller) PrepareAck(r PrepareReceipt) (View, error)
func (c *Controller) ApplyAck(r ApplyReceipt) (View, error)
func (c *Controller) RevokeAck(r RevokeReceipt) (View, error)
func (c *Controller) View() View
```

返回的 `View.Stale` 为 true 表示收到的是旧/重放/重复回执（`View.Message`
附带原因），状态未改变。哨兵错误：`ErrInsufficient`、`ErrStaleVersion`、
`ErrPodNotFound`、`ErrNodeNotFound`、`ErrUnknownResource`、
`ErrNegativeResource`、`ErrAlreadyExists`。

### 容量核算

- 单个 Pod 的实际占用 = 可见规格与它所有未完结代次（含 Revoking 中）预留的
  **逐资源最大值**，同一 Pod 的多个代次不重复累加。
- 节点已用量 = 节点上各 Pod 实际占用之和。接纳新代次时要求
  `节点已用量 − 该Pod当前占用 + 新预留 ≤ 节点容量`。

## 复现步骤

环境要求：Go 1.20.2（仓库 `go.mod` 声明 `go 1.20`，无第三方依赖）。

```sh
go test ./... -count=1          # 完整功能测试
go test -race ./... -count=1    # 竞态检测
go test -cover ./...            # 覆盖率
go test -v ./...                # 查看每个用例
gofmt -l .                      # 格式检查（无输出即合格）
```

## 验证范围（10 个测试，全部以真实断言验证）

| 测试 | 覆盖场景 |
| --- | --- |
| `TestHappyPath35To53` | 正常路径：3/5→5/3 全程 5/5 预留；单项 Ready 不切换；双项 Ready 一次切换；apply 后才释放收缩项 |
| `TestCapacityBoundaryAndVersioning` | 边界：精确占满可用量为 0、超订拒绝、版本必须严格递增、负值拒绝 |
| `TestShrinkReservationHoldsUntilApply` | 缩容项在切换前后、apply 之前持续占用；apply 后容量才重新可接纳 |
| `TestSupersedeReplayAndRevocation` | 交错：新请求替换旧请求、旧预留待撤销仍占容量、重放旧 prepare/apply 不推进新代次、revoke 后才释放、重复 revoke 安全 |
| `TestSupersedeDoesNotRevokeApplyingGeneration` | 已过切换屏障（Applying）的代次不被撤销，其 apply 回执仍合法并正确释放，且不影响新 active 代次 |
| `TestRestartRecoveryMidPreparation` | 恢复：准备中途重启，重建预留/可见规格/待确认动作并从中断点继续完成 |
| `TestRestartRecoveryDuringRevocation` | 恢复：撤销中重启，容量保护持续有效，重启后可确认撤销 |
| `TestMultiPodInterleaving` | 多 Pod 交错回执、节点占用求和、Pod 间互不干扰、未知 UID/资源报错、未知代次记为 stale |
| `TestConcurrentSubmissions` | 并发：16 goroutine 交叉提交，账本始终不超容量、恰好一个 active 代次、其余均在 Revoking |
| `TestFileStoreRoundTrip` | FileStore 落盘/加载往返、原子文件语义、空文件存储返回 nil 快照 |

说明：`go vet` 在本环境需交互式授权未执行；编译、`go test`、`-race`、
覆盖率与 `gofmt` 均已实际执行通过。所有测试使用本地合成数据，不访问网络、
不依赖外部服务。
