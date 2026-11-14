# AuthzValidator — 网关出站授权离线验证器

对网关出站日志中的**授权记录**与**投递事件**进行离线核对，发现投递复用过期或不匹配的批准决定等问题。程序只核验本地记录，**不发出任何网络请求**，仅依赖 Swift 5.9 + Foundation（无第三方依赖）。

## 输入格式

JSON 文件，支持三种结构：

1. 顶层事件数组；
2. `{ "events": [ ... ] }` 包装；
3. `{ "authorizations": [ ... ], "deliveries": [ ... ] }` 分离包装（也可以把两个文件分别作为两个参数传入）。

每条事件字段（snake_case）：

| 字段 | 适用 | 说明 |
| --- | --- | --- |
| `event_type` | 全部 | `authorization`（授权记录）或 `delivery`（投递事件） |
| `request_id` | 全部 | 匿名请求 ID |
| `stage` | 全部 | 阶段序号（整数） |
| `target_classification` | 全部 | 目标分类 |
| `policy_version` | 全部 | 策略版本 |
| `decision_id` | 全部 | 决策 ID，投递通过它引用授权记录 |
| `event_time` | 全部 | 事件时间，ISO8601（支持小数秒，如 `2026-11-14T08:00:00.123Z`） |
| `decision` | 仅授权记录 | `approve` 或 `deny` |
| `valid_until` | 仅批准记录 | 批准有效截止时间（ISO8601） |
| `case_id` | 可选 | 稳定用例标识，会原样出现在报告中，便于审计追溯 |

## 校验规则

对每次投递按以下顺序核对（同一投递取第一条命中的问题）：

1. **批准记录缺失** `approval_record_missing`：`decision_id` 找不到任何授权记录。
2. **策略拒绝仍投递** `delivery_under_denied_decision`：授权结论为 `deny` 却出现投递。
3. **时序不一致** `authorization_timestamp_after_delivery`：授权记录时间晚于投递时间。
4. **授权到期** `authorization_expired`：投递时间晚于 `valid_until`（恰好等于截止时间视为有效）。
5. **阶段变化未重新授权** `stage_changed_without_reauthorization`：批准与投递的 `stage` 不一致，且投递没有引用新决策 ID 的重新授权记录。
6. **目标分类变化未重新授权** `target_classification_changed_without_reauthorization`。
7. **策略版本变化未重新授权** `policy_version_changed_without_reauthorization`。

记录级预检（不依赖投递）：

- `conflicting_authorization_records`：同一 `decision_id` 存在多条结论/阶段/分类/策略版本/截止时间互相矛盾的授权记录；
- `approval_missing_valid_until`：批准记录缺少 `valid_until`；
- `invalid_timestamp`：事件时间不是合法 ISO8601。

**重新授权的语义**：阶段、目标分类或策略版本变化后，投递必须引用另一条与自身三项属性一致、且在投递前生效的批准记录（新 `decision_id`）；继续复用旧批准即判定为未重新授权。

## 输出语义

stdout 输出单个 JSON 对象：

```json
{
  "status": "violation_found | compliant",
  "total_events": 9,
  "authorization_count": 4,
  "delivery_count": 5,
  "findings": [ ... ],
  "first_violation": { ... }
}
```

`findings` 按事件时间升序排列；`first_violation` 即**第一处违反授权约束的事件**，包含：

- `case_id` / `reason_code` / `reason`（中文原因说明）
- `request_id` / `decision_id` / `event_time`
- `evidence.delivery`：触发问题的投递事件原始记录
- `evidence.authorization_records`：相关授权记录原始证据（缺失时为空数组）

退出码：`0` 合规；`1` 发现授权约束问题；`2` 用法或输入文件错误（错误信息走 stderr）。

## 本地入口（可重跑）

```bash
# 构建与测试（离线，禁用远端依赖解析）
/usr/bin/swift build --disable-automatic-resolution --skip-update
/usr/bin/swift test  --disable-automatic-resolution --skip-update

# 运行验证器
/usr/bin/swift run AuthzValidator --pretty Samples/events.json
# 或授权与投递分两个文件
/usr/bin/swift run AuthzValidator authorizations.json deliveries.json
```

样例 `Samples/events.json` 内含合规投递、阶段变化、授权到期、拒绝后投递、批准缺失五类记录，运行后 `first_violation` 指向时间最早的问题事件，`findings` 给出全部证据问题。

## 测试与需求映射

测试位于 `Tests/AuthzValidatorTests/AuthzValidatorTests.swift`，全部为真实执行的 XCTest 断言（无跳过、无占位）：

| case_id | 场景 | 覆盖的需求点 |
| --- | --- | --- |
| TC-01 | 合规投递 | 批准有效且阶段/分类/策略版本一致 → 无问题 |
| TC-02 | 批准后阶段变化 | 阶段变化未重新授权 |
| TC-03 | 授权到期 | 投递晚于 `valid_until` |
| TC-04 | 策略拒绝仍投递 | deny 下出现 delivery |
| TC-05 | 批准记录缺失 | 决策 ID 无授权记录 |
| TC-06 | 变化后重新授权 | 新决策 ID 覆盖新阶段 → 合规 |
| TC-07 | 目标分类变化 | 分类变化未重新授权 |
| TC-08 | 策略版本变化 | 版本变化未重新授权 |
| TC-09 | 时序不一致 | 授权晚于投递 |
| TC-10 | 授权记录冲突 | 同决策 ID 多条矛盾记录 |
| TC-11 | 批准缺截止时间 | 记录级证据问题 |
| TC-12 | 非法时间戳 | 记录级证据问题 |
| TC-13 | 截止时刻边界 | 投递时间 == `valid_until` 仍有效 |
| TC-14 | 小数秒边界 | 毫秒级到期判定 |
| TC-15 | 重复授权记录 | 完全重复不算冲突 |
| TC-16 | 第一处违反排序 | `firstViolation` 指向最早问题 |
| TC-17 | 多请求隔离 | 请求间互不干扰 |
| TC-18/19 | 解析器正常路径 | 数组/包装/分离结构 |
| TC-20 | 解析器失败路径 | 非法 JSON、结构错误、缺字段、文件缺失 |
| TC-21 | 报告编码 | JSON 含原因码与证据 |
| TC-22 | 空输入 | 零事件合规 |

## 限制

- 纯离线：仅使用 Foundation，不监听端口、不联网、不调用任何外部服务。
- 时间统一按 ISO8601 带时区字符串比较；无法解析的时间戳记为 `invalid_timestamp` 证据问题，不会中断其余核对。
- 同一 `decision_id` 多条完全相同的授权记录视为重复，取事件时间最晚者参与核对；内容矛盾则报冲突。
