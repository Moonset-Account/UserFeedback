# dmaring — DMA 描述符队列

一个纯本地、无外部依赖的设备驱动 DMA 描述符队列实现（Go 1.20.2，仅标准库）。
输入为按逻辑字节顺序排列的物理地址/长度段，输出为满足硬件约束的最少描述符，
并在固定容量的环形队列中以"整笔请求原子预留、乱序完成、按最老连续完成回收"的方式管理槽位。

## 构建与验证入口

所有命令在模块根目录（本目录）执行，模块名为 `dmaring`。

```sh
go test ./...          # 验收（全部使用 Go 内置 testing，含真实断言）
go test -race ./...    # 竞态检测（环形队列方法用 sync.Mutex 保护）
go test -count=3 ./... # 重复运行确认稳定性
gofmt -l .             # 格式化检查，无输出即通过
```

## 实际代码入口

| 文件 | 入口 | 职责 |
| --- | --- | --- |
| `descriptor.go` | `BuildDescriptors(segs []Segment, l Limits) ([]Descriptor, error)` | 把逻辑有序的物理段打包成最少且合法的描述符 |
| `descriptor.go` | `type Limits{MaxBytes, Boundary}`、`type Segment`、`type Descriptor` | 硬件约束与数据结构 |
| `ring.go` | `NewRing(capacity int, l Limits) (*Ring, error)` | 构造固定容量环形队列 |
| `ring.go` | `(*Ring).Submit(id uint64, req []Segment) ([]Descriptor, error)` | 打包并原子预留整笔请求的全部槽位 |
| `ring.go` | `(*Ring).Complete(id uint64) error` | 上报请求完成（允许乱序） |
| `ring.go` | `(*Ring).Reap() []RequestInfo` | 回收最老的连续已完成请求及其槽位 |
| `ring.go` | `Free/Used/Outstanding/Slot/Snapshot/Capacity` | 槽位、剩余空间与物理范围的只读检视 |

哨兵错误：`ErrInvalidLimits`、`ErrEmptyRequest`、`ErrZeroLength`、`ErrOverflow`、
`ErrNoSpace`、`ErrDuplicateRequest`、`ErrUnknownRequest`，均可用 `errors.Is` 判定。

## 实现的语义与交叉约束

1. 相邻物理段合并：下一段起点恰为上一段末地址时，只要当前描述符仍可合法延长就并入，
   从而最小化条目数并保持原字节次序。
2. 每描述符至多 `MaxBytes` 字节；`Boundary` 为 0 或二次幂，非零时描述符不得跨越
   `Boundary` 的整数倍地址；恰好在边界上结束是合法的。
3. 贪心填充：每个可合法延长的描述符沿输入顺序尽量填满；放不下的段按最少次数切分。
   两条约束交错时同时生效（先撞边界按边界切，否则按 `MaxBytes` 切）。
4. 整笔请求所需槽位一起获得：`Submit` 先计算描述符数，空闲槽不足时返回 `ErrNoSpace`，
   不写任何槽、不移动生产指针；只有全部槽位可预留时才一次性写入并推进生产指针。
5. 环形绕尾写：内部用单调 `head/tail` 计数，物理槽位由 `计数 % 容量` 得到，天然支持绕尾，
   且避免满/空状态歧义。
6. 乱序完成与有序回收：`Complete` 可按任意顺序上报；`Reap` 只从最老请求开始，
   遇到第一个未完成请求即停止，其后即使已完成也不释放槽位。

## 测试验证范围

测试均为带真实断言的单元测试（`descriptor_test.go`、`ring_test.go`），无外部目标、无网络。

描述符层（`BuildDescriptors`）：

- 可接受结果：相邻段合并为单描述符；仅 `MaxBytes` 切分；贪心跨多段合并。
- 应被阻止/拒绝：空请求、`MaxBytes=0`、非二次幂 `Boundary`、零长度段、地址回绕（`ErrOverflow`）。
- 边界与交错：跨二次幂边界精确切分；`MaxBytes` 与边界交替决定切点；恰好在边界结束保持单描述符；
  并通过逐地址展开断言描述符覆盖的字节序列与输入完全一致、次序不变。

环形层（`Ring`）：

- 可接受结果：普通提交的槽位记账；头条场景 `TestRingWrapAndOutOfOrderCompletion`
  让一个跨边界请求恰好绕过环尾写入槽 0,1，再先完成排在后面的请求，校验槽位内容、
  剩余空间与物理范围，以及回收后再次绕尾复用。
- 应被阻止/拒绝：空间不足返回 `ErrNoSpace` 且生产指针、槽位、请求登记表均不变；
  超过整环容量的请求不部分入队；重复 ID、未知/已回收 ID 的完成被拒绝；非法构造参数与槽位越界。
- 边界/交错场景：最老请求未完成时，更年轻请求即使完成也无法释放任何槽；
  最老请求补齐完成后，连续多笔按提交顺序一次性回收并恢复全部空闲槽。

## 说明

- 地址与长度为 `uint64`，对 `PhysAddr+Length` 回绕做了显式拒绝。
- 本组件仅做本地基础设施功能，不包含任何真实设备访问、目标探测或负载生成。
