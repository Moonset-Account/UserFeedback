# 外设接入判定模块（peripheral-arbiter）

受管主机的外设接口准入判定库，纯 Go 实现，仅依赖标准库；模块声明为
`peripheral-arbiter`（`go.mod`，Go 1.20.2 验证）。

## 入口与使用

- 实现：`arbiter.go`，包 `arbiter`
- 测试：`arbiter_test.go`，全部使用 Go 内置 `testing`，无外部依赖
- 核心类型与入口：
  - `arbiter.New()`：创建一个判定器，跨枚举重放维护状态。
  - `Arbiter.Adjudicate(Enumeration) (*Decision, error)`：输入一次枚举快照
    （本次插入编号 `InsertionID`、本次枚举编号 `EnumerationID`、接口功能
    列表、必须一起启用的接口集合 `CoEnableSets`、设备批准用途
    `ApprovedFunctions`），返回裁决结果 `Decision{Permitted, Denied,
    RejectionReason}`。
  - `Arbiter.AckEnable(insertionID, enumerationID, interfaceID)`：处理异步
    “启用确认”。仅当插入/枚举编号与当前轮次一致、接口存在且裁决允许时
    才接受，否则返回 `ErrStaleRound` / `ErrUnknownInterface` /
    `ErrInterfaceDenied`。
  - `Arbiter.ActiveInterfaces()`：当前轮次**实际启用**的接口集合（已被
    裁决允许且收到当轮确认），按枚举顺序返回。
  - `Arbiter.PermittedInterfaces()`：裁决允许但未必已确认的接口集合。

## 判定规则（对应题面约束）

1. 功能不在批准用途内的接口一律不启用。
2. 同一硬件隔离组内存在**不可独立隔离**的未批准功能时，该组的共享部分
   整组保持关闭；标记 `IndependentlyIsolatable` 的已批准接口可单独围栏，
   仍然启用。可独立隔离的接口自身未批准时仍被拒绝，但不污染组。
3. `CoEnableSets` 声明必须一起启用的接口，经并查集合并（含传递绑定）为
   捆绑组；捆绑组中任一成员不可准入，则整组拒绝（all-or-nothing）。
4. 设备改变工作模式重新枚举（同一插入下新的 `EnumerationID`，或新的
   `InsertionID`）时，先清空并停用全部旧接口，再按新拓扑裁决；旧轮次
   迟到的启用确认一律以 `ErrStaleRound` 拒绝，无法恢复旧权限。重复裁决
   同一轮次是幂等的，已接受的确认保留。

## 验证范围

执行：`go test -v ./...`（另已通过 `go test -race -count=1 ./...` 与
`gofmt` 检查；`go vet` 在当前沙箱权限下未获批准，未执行）。

测试均为真实断言（集合比对与 `errors.Is` 错误判定），覆盖：

- 可接受结果：
  - `TestSharedIsolationGroupComposite/all_approved_group_comes_up`：共用
    隔离组全部功能已批准，组内接口与可隔离接口全部启用。
  - `TestGroupableComposite`：可分组复合设备，干净组（含 HID 捆绑对）
    正常启用，最终实际启用集合为 `{kbd, mouse, nic}`。
- 应被阻止/拒绝的结果：
  - `TestSharedIsolationGroupComposite/unapproved_function_holds_shared_group_closed`：
    共用组出现未批准存储功能，整组共享部分关闭，仅独立隔离的已批准
    NIC 可用；对被拒接口发启用确认返回 `ErrInterfaceDenied`。
  - `TestCoEnableBundleAllOrNothing`：捆绑组成员含未批准功能时，已批准
    的绑定成员一并被拒；并验证 A-B、B-C 传递绑定整体拒绝。
  - `TestInvalidEnumerations`：非法输入（空 ID、重复接口、共启引用未知
    接口、单元素共启集合、编号非正等）返回 `ErrInvalidEnumeration`。
- 边界与交错场景：
  - `TestSharedIsolationGroupComposite/unapproved_isolatable_interface_denied_but_group_open`：
    可隔离接口自身未批准时只拒绝自身，不波及干净的共享组。
  - `TestModeChangeReenumerationAndLateAcks`：同一次插入下模式切换、
    拓扑重枚举，先全量停用旧接口；上一轮迟到确认（含对新拓扑仍允许的
    接口的旧轮确认）全部 `ErrStaleRound` 拒绝；当轮确认后实际启用集合
    为 `{nic, cam}`；同轮重复裁决幂等。
  - `TestNewInsertionResetsAllPermissions`：新插入编号从全关状态开始，
    旧插入编号的确认判为陈旧，未知接口返回 `ErrUnknownInterface`。
  - `TestEmptyArbiterRejectsAcks`：尚未裁决时任何确认都不能启用接口。

所有夹具均为测试内合成的复合设备拓扑，不含任何真实设备探测或攻击逻辑。
