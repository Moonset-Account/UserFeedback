# wipeverify — 加密擦除核验模块

磁盘加密服务把同一把卷密钥分别封存在主机与灾备机的硬件槽里。退租时，本模块依据
**卷最后封存的清单**和**设备签名已验证的回执**（安装 / 搬迁 / 销毁），核实退租密钥的
每个副本均已销毁；全部副本结案后才允许开出擦除证明。

本模块只做防御性的台账核验：不实现、不执行任何攻击或规避逻辑，不做回执签名的密码学
验证（假定调用方已完成），不访问真实设备，全部验证均基于本地合成数据。

## 核验规则

1. **每台设备连续的回执序号**：只有从该设备已见最小序号开始的连续、无冲突前缀是有效
   证据。缺少中间序号（或同一序号出现两张内容不同的回执）时，该序号及其后的回执全部
   不可用，相关副本保留**待核实**状态；缺口补齐后自动转为可结案。
2. **原世代销毁凭据才能结案**：副本只能被与其（设备、槽位、卷、原世代）精确匹配的
   销毁/搬迁回执结案。槽位复用给其他卷后，新世代的销毁回执不能为退租副本结案。
3. **到达顺序无关**：搬迁先建立新副本再销毁旧副本，两份回执可能以相反顺序送到。核验
   只依赖累积的回执集合，与到达顺序无关；同一证据集的任何到达顺序产生相同的报告与
   证明摘要。
4. **副本义务来源**：清单中的副本 ∪ 有效安装回执揭示的历史副本（搬迁前的旧槽位），
   每一个都必须有销毁凭据。

## 公开 API

```go
type Manifest struct { Volume string; Copies []CopyRef }        // 退租时最后封存清单
type CopyRef  struct { Device string; Slot int; Generation uint64 }
type Receipt  struct { Device string; Seq uint64; Kind Kind; Volume string; Slot int; Generation uint64 }
// Kind: KindInstall（安装）/ KindMigrate（搬迁，源槽位结案）/ KindDestroy（销毁）

func NewVerifier(m Manifest) *Verifier
func (v *Verifier) AddReceipt(r Receipt) error   // 任意顺序送达；重复送达幂等；同号冲突毒化链条
func (v *Verifier) Report() Report               // 每个副本 StatePending/StateDestroyed + 原因说明
func (v *Verifier) Certificate() (Certificate, error) // 有待核实副本时返回错误，否则开出证明（含确定性 SHA-256 摘要）
```

`Report.Copies` 按（设备、槽位、世代）排序；待核实副本的 `Note` 说明原因（序号链断裂 /
槽位已复用需原世代凭据 / 尚无销毁回执）。

## 运行方法

```sh
go test ./...        # 运行全部单元测试与演示 Example（Go 1.20.2，仅标准库，无外部依赖）
```

## 验证范围（均已实测，`go test ./... -v` 全部 PASS）

- `TestDecommissionFlow`：题面要求的演示——旧槽已被搬迁回执清除、迁入槽尚存时报告
  待核实且拒绝开证；最后一份有效回执（迁入槽原世代销毁）补到后开出擦除证明。
- `ExampleVerifier`：同一份小型合成记录的端到端演示，输出经 `go test` 断言。
- `TestMigrationReceiptsReverseOrder`：搬迁的安装/销毁回执逆序送达，结论不变。
- `TestSequenceGapKeepsPending`：缺少中间序号时即使销毁回执已到也保留待核实；缺口
  补齐后结案。
- `TestSlotReuseRequiresOriginalGeneration`：槽位复用给其他卷后，新世代销毁回执不能
  结案，只有原世代销毁凭据可以。
- `TestConflictingReceiptPoisonsChain`：同序号冲突回执毒化链条，副本保持待核实。
- `TestDestroyMustMatchVolumeAndGeneration`：卷名或世代不匹配的销毁回执不能结案。
- `TestReceiptValidation`：非法回执（空设备、序号 0、空卷、负槽位、未知类型）报错；
  相同回执重复送达幂等。
- `TestDeterministicAcrossArrivalOrders`：任意到达顺序产生相同报告与相同证明摘要。
- `TestManifestOnlyCopyPending`：无任何回执的清单副本保持待核实。

## 假设与边界

- 回执的设备签名由调用方验证；本模块只核验序号连续性、世代匹配与副本结案逻辑。
- 序号链基线为每台设备**已见**的最小序号（题面只要求核实"中间"缺失的序号）。
- `Verifier` 非并发安全。
- 复核记录：`gofmt -l .` 无输出；`go test ./... -v` 全部通过（10 个测试/示例）。
  `go vet` 未单独执行（环境权限限制），但 `go test` 默认启用的 vet 子集检查已随测试通过。
