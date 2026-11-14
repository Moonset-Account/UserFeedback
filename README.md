# QuotaIsolationBench — 多租户限流配额隔离本地测试台

针对「新版限流器把两个合法租户的并发请求计入同一额度」这一缺陷的本地复现与回归测试台。
纯 Swift 5.9 + Foundation，Swift Package 形式交付，**无任何远端 / 第三方依赖**，全程离线运行。

## 1. 目录结构

```
Package.swift
Sources/
  QuotaIsolationCore/          # 可测试核心库（不依赖 Foundation）
    Types.swift                # 领域类型、拒绝理由、账本结构、结构性错误
    QuotaEngine.swift          # 固定周期配额引擎（预占/确认/归还/超时回收）
    DemoScenario.swift         # 内置合成场景
    LedgerPrinter.swift        # 逐步账本的人类可读渲染
  quota-bench/                 # 可执行入口 + 本地 JSON 适配器（仅用 Foundation）
    main.swift
    ScenarioLoader.swift
Tests/
  QuotaIsolationCoreTests/
    QuotaIsolationCoreTests.swift   # 9 个真实 XCTest 用例（正常/边界/失败路径）
Fixtures/
  demo-scenario.json
  shuffled-order-scenario.json
```

## 2. 本地可重跑入口（仅用 /usr/bin/swift）

```bash
# 核验入口：构建 + 运行全部真实 XCTest
/usr/bin/swift test --disable-automatic-resolution --skip-update

# 仅构建
/usr/bin/swift build --disable-automatic-resolution --skip-update

# 运行内置合成演示场景，打印逐步账本
/usr/bin/swift run --disable-automatic-resolution --skip-update quota-bench

# 运行本地 JSON 场景
/usr/bin/swift run --disable-automatic-resolution --skip-update quota-bench Fixtures/demo-scenario.json
```

`--disable-automatic-resolution --skip-update` 保证不下载、不解析任何远端依赖。
本包 `Package.swift` 也未声明任何远端依赖。

## 3. 输入格式

### 3.1 编程接口

输入记录 `QuotaEvent` 包含四类字段（即题目规定的输入）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `tenant` | `TenantID` | 已验证的租户 ID，账本按租户严格隔离 |
| `sequence` | `Int` | 声明的事件序号，引擎始终按序号处理，与存储顺序无关 |
| `requestID` | `RequestID` | 请求 ID，重复事件按「租户 + 请求 ID」识别 |
| `clock` | `Int64` | 虚拟时钟读数，只允许单调不减 |
| `kind` | `reserve / confirm / release` | 预占 / 确认占用 / 归还 |

策略仅实现**固定配额周期**：`periodLength` 个时钟单位为一个周期，周期号 = `clock / periodLength`；
预占在 `timeout` 个时钟单位内未确认，处理下一个事件前被自动回收。

### 3.2 JSON 文件格式（可执行入口）

```json
{
  "periodLength": 1000,
  "timeout": 300,
  "tenants": [ { "tenantID": "alpha", "quota": 2 } ],
  "events": [
    { "tenantID": "alpha", "sequence": 1, "requestID": "A-1", "kind": "reserve", "clock": 10 }
  ]
}
```

## 4. 处理语义（策略规则）

1. **预占**：在事件所属周期占 1 个额度；额度已满则拒绝（`QUOTA_EXHAUSTED`），拒绝不改任何余额。
2. **确认占用**：仅把预占记录状态转为已确认，额度仍挂在原周期，不发生二次扣费。
3. **归还**：把 1 个额度退回**原预占所在周期**；重复归还（重复失败回调）只归还一次（`DUPLICATE_RELEASE`）。
4. **超时回收**：处理每个事件前先做扫描，`clock - reservedAt >= timeout` 且仍处于预占态的记录被回收，
   额度退回原周期，账本产生 `TIMEOUT_RECLAIMED` 条目。
5. **跨周期事件**：周期切换后到达的确认 / 归还 / 回收始终关联原预占记录与原周期，
   绝不消耗新周期额度。回收后的迟到确认返回 `LATE_CONFIRM_AFTER_RECLAIM`。
6. **重复识别**：按「租户 + 请求 ID」识别重复预占 / 确认 / 归还，各自给出明确拒绝理由。
7. **顺序无关**：输入记录可任意打乱存储，引擎按 `sequence` 排序处理，账本逐字节一致。

## 5. 输出语义

每个输入事件（以及事件触发的每条超时回收）产生一条 `LedgerEntry`：

- `sequence / clock / tenant / requestID`：事件定位
- `action`：`RESERVE_ACCEPTED / RESERVE_REJECTED / CONFIRM_ACCEPTED / CONFIRM_REJECTED /
  RELEASE_ACCEPTED / RELEASE_REJECTED / TIMEOUT_RECLAIMED`
- `accepted` 与 `rejectReason`：是否接受及拒绝理由（拒绝理由枚举见 `Types.swift`）
- `attributedPeriod`：事件归属周期，跨周期事件仍指向原预占周期
- `snapshots`：该步之后**所有租户**每个周期的 `held / quota / remaining` 预算余额
- `note`：人类可读说明

## 6. 需求条目 → 测试用例映射（稳定 case_id）

| case_id | 覆盖的题目要求 | 关键断言（输入 → 实际输出 → 期望） |
| --- | --- | --- |
| `QIB-01` | 同一租户并发预占不会超额 | seq1/2 预占后 held=1、2/2；seq3 超额拒绝，reason=`QUOTA_EXHAUSTED`，拒绝后 held 仍为 2 |
| `QIB-02` | 两个租户分别用满额度（缺陷回归） | alpha 持满 2/2 时 beta 的 B-1/B-2 仍被接受；每个快照同时含两租户且 `held+remaining=quota` |
| `QIB-03` | 预占→确认→归还主路径 | 确认不改 held；归还后对应周期 held 减 1、remaining 恢复；beta 事件不动 alpha 余额 |
| `QIB-04` | 重复失败回调只归还一次 + 重复确认/预占 | seq8 重复归还被拒且 held 不变；seq10 归还后确认=`LATE_CONFIRM_AFTER_RELEASE`；seq15 重复预占=`DUPLICATE_RESERVATION` |
| `QIB-05` | 超时回收 + 回收后迟到确认不消耗新周期额度 | seq11 前回收 A-2/B-2 回周期 0；seq12 迟到确认=`LATE_CONFIRM_AFTER_RECLAIM` 且周期 1 held 不变；seq14 新预占仍可用满周期 1 |
| `QIB-06` | 调换存储顺序、按序号处理得到相同账本 | 固定反转+旋转打乱输入后，两条账本 `==` 完全一致，且跨引擎实例可复现 |
| `QIB-07` | 失败路径：结构性输入拒绝 | 重复序号、时钟回退、未知租户、空请求 ID、负时钟、非法配置均在处理前抛 `ScenarioError` |
| `QIB-08` | 边界条件 | 未知请求事件不动预算；超时差 1 不回收、恰好相等回收；timeout=0 下一事件前立即回收；跨周期归还只动原周期 |
| `QIB-09` | 全账本不变量审计 | 任意步 held ∈ [0,quota]、`remaining=quota-held`；拒绝事件前后余额逐租户一致；每个接受的预占都到达释放/回收终态；审计 9 个 case_id 全部真实执行 |

测试运行结束会打印实际执行的用例数与断言检查总数（例如
`executed test cases: 9 / 9, total assertion checks: N`），不存在跳过用例或占位测试。

## 7. 限制与边界

- 仅实现固定配额周期，不实现滑动窗口、令牌桶等其他策略。
- 全部输入为本地合成数据，无网络监听、无云 / 身份 / 邮件系统连接。
- 虚拟时钟由输入驱动，引擎不读取系统墙钟；时钟回退属于结构性错误，直接拒绝整批输入。
- 引擎设计为单线程顺序处理（按事件序号），不引入并发调度。
