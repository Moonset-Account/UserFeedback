# 双槽签章设备适配层

Go 1.20.2，无第三方依赖。

## 入口

- `signing/adapter.go` — 适配层本体。入口是 `signing.New(device, allowedKeys...) *Adapter`。
  - `Adapter.Begin(document, key) (*Session, error)`：占一个槽并让设备为该钥匙建立授权。
  - `Session.Submit(fragment []byte) error`：按调用顺序提交下一个片段。
  - `Session.Sign() ([]byte, error)`：出签、释放槽。
  - `Session.Abort(cause)` / `Session.Close()`：取消；清理成功后才归还槽。
  - `Adapter.Reset()`：设备复位，让所有进行中的授权过期。
  - 设备抽象是 `signing.Device` 接口，可注入。
- `signing/fake_device_test.go` — 设备替身，按授权记录「槽号 + 钥匙编号 + 按顺序的片段」，并像真机一样拒绝：同槽重复授权、过期纪元、乱序片段、非当前占用者的释放。
- `signing/adapter_test.go` — 验收用例，使用标准库 `testing`。

## 运行

```
go test ./...
go test -race ./...
```

## 被验证的题面约束

| 约束 | 用例 |
| --- | --- |
| 两份文档同时占用两个槽并各自出签 | `TestTwoDocumentsSignConcurrentlyOnTwoSlots` |
| 两槽占满时第三份被拒绝，且不触达设备 | `TestRejectsThirdDocumentWhileBothSlotsBusy` |
| 只允许获准钥匙，拒绝不消耗容量 | `TestRejectsUnapprovedKeyWithoutConsumingCapacity` |
| 清理失败则槽隔离，重试成功后才交给下一份 | `TestSlotFreedOnlyAfterSuccessfulCleanup` |
| 复位使授权过期；旧会话迟到的关闭不能清掉新占用者 | `TestDeviceResetExpiresSessionsAndLateCloseCannotStealSlot` |
| 两个客户交替提交多轮，字节与钥匙不串台 | `TestManyClientInterleavingsKeepSignaturesIsolated`、`TestConcurrentClients` |
| 片段提交失败 / 出签失败后槽保持隔离 | `TestAppendFailureQuarantinesSlot`、`TestSignFailureQuarantinesSlotUntilCleanup` |
| 无片段不得出签，且不占死槽位 | `TestSignWithoutFragmentsIsRejected` |

可接受结果与应被拒绝的结果都有覆盖；边界（两槽刚好占满、两个片段客户端交替、复位抢占同一物理槽）由上述用例交错验证。

## 设计要点

- 适配层用一把互斥锁串行化设备调用，设备实现无需并发安全。
- 槽位对设备的操作一律凭 `AuthID` 而不是槽号，`Release` 前还要校验槽的占用令牌与纪元，因此旧授权的迟到通知在两层上都碰不到新占用者。
- 设备复位可绕过适配层（例如面板直接复位）；每次入口调用都会比对设备纪元，发现漂移即把旧授权全部标记为过期、腾空槽位。
- 任何设备拒绝（提交或出签）都会让会话进入 `stateCleanup`：既不能再提交/出签，也不会归还槽，直到 `Close` 成功。

## 验证范围

仅在本工作区用本地合成夹具验证，不涉及真实设备、网络或外部目标。交付物不含探测真实设备或构造攻击载荷的代码。
