# Multipart 离线归属检查器

核对**上传网关**与**业务层**分别记录的 Multipart 解析结果，发现同名附件的安全校验结果被错误归属到其他附件等问题，输出带记录编号的阻止提交报告。纯离线、零运行时依赖，仅使用 Node.js 内置模块（`node:crypto`、`node:test`）。

## 背景

- 业务层允许同名附件字段（如多个 `attachment`）出现多次，靠**字段实例编号**区分。
- 旧版在某些情况下会把一个附件的安全校验结论记到另一个附件上。
- 本工具输入两端已解析的事件摘要，按请求与字段实例重建字段序列，检查元数据、内容摘要与安全校验记录能否一一关联。

## 输入格式

两端事件摘要同构：

```jsonc
{
  "side": "gateway",                 // 或 "business"
  "fragments": [                     // 片段解析记录
    {
      "recordId": "GW-F-001",        // 该端记录编号（端内唯一）
      "requestId": "REQ-7788",       // 请求号
      "fieldName": "attachment",     // 字段名（同名可重复）
      "fieldInstance": 1,            // 字段实例编号
      "fragmentIndex": 0,            // 片段编号（从 0 起连续）
      "parseStatus": "ok",           // ok | error | truncated
      "contentDigest": "sha256-...", // 片段内容摘要，可缺省
      "byteLength": 1024             // 可选
    }
  ],
  "checks": [                        // 安全校验记录
    {
      "recordId": "GW-S-001",
      "requestId": "REQ-7788",
      "fieldName": "attachment",
      "fieldInstance": 1,
      "verdict": "allow",            // 安全结论，如 allow / block
      "contentDigest": "sha256-..."  // 字段级摘要（全部片段摘要的重建哈希）
    }
  ]
}
```

字段级摘要规则（两端一致即可，见 `src/digest.js`）：

```
fieldDigest = SHA256("field-v1\n" + fragmentDigest[0] + "\n" + ... + fragmentDigest[n-1])
```

## 检查逻辑与问题分类

重建顺序：`(请求号, 字段名)` 分组 → 组内按 `fieldInstance` 重建实例序列 → 实例内按 `fragmentIndex` 排序重建片段序列与字段摘要 → 安全校验记录按同一坐标归位。

| 类型 | 触发条件 | 是否阻止提交 |
| --- | --- | --- |
| `attribution_conflict` 关联冲突 | 校验摘要与字段重建摘要不一致；本端摘要与对端其他实例的记录吻合（错位证据）；两端结论不一致；两端校验摘要不一致；校验记录引用不存在的实例 | 是 |
| `field_missing` 字段缺失 | 一端解析出的字段实例/校验记录在另一端不存在 | 是 |
| `truncation` 传输截断 | 片段 `truncated`/`error`；片段编号有缺口或重复编号，序列无法完整重建 | 是 |
| `metadata_conflict` 元数据冲突 | 同实例编号两端片段数量、片段内容摘要或字节数不一致 | 是 |

无任何问题的请求进入 `validRequests`，并标记 `multiAttachment`（合法多附件请求）。

每条问题都包含：`type`、`requestId`、`fieldName`、`fieldInstance`、`gatewayRecordIds`、`businessRecordIds`、`blockReason`、内容寻址的稳定 `issueId`。

## 幂等保证

1. `inspectPair` 是纯函数：相同输入 → 相同 `reportId` / `issueId`（确定性排序 + 键排序 JSON + SHA-256，函数内部不读取时钟）。
2. `ReportStore` 按 `issueId` 全局去重：同一批结果重复导入返回 `deduped: true` 且 `newIssueCount: 0`；不同批次导入相同内容，问题也不重复计数。
3. 同一 `batchId` 以不同内容导入会抛错，拒绝静默覆盖。

## 用法

```bash
# 库 API
npm test

# CLI：退出码 0 通过，1 有阻止性问题，2 输入错误
node src/cli.js --gateway examples/gateway-event.json \
                --business examples/business-event.json \
                [--batch-id BATCH-DEMO]
```

```js
import { inspectPair, ReportStore } from './src/index.js';

const report = inspectPair(gatewayEvent, businessEvent);
if (report.blocking) {
  for (const issue of report.issues) {
    // issue.gatewayRecordIds / issue.businessRecordIds / issue.fieldName / issue.blockReason
  }
}

const store = new ReportStore();
store.importPair(gatewayEvent, businessEvent, 'BATCH-001');
store.importPair(gatewayEvent, businessEvent, 'BATCH-001'); // 幂等，不产生新问题
```

## 测试覆盖（`test/inspector.test.js`）

1. 合法多附件请求：同名字段两个实例完整对齐，不误报
2. 同名附件顺序变化：业务端先解析到第二个附件，按实例编号归位后零问题
3. 不同附件共用字段实例编号：内容摘要不一致 → 元数据冲突
4. 第二个附件被截断：`truncation` + 校验记录缺失同时上报
5. 业务端只收到部分校验记录：`field_missing`
6. 旧版错位归属：两个实例的校验记录互换 → 多条 `attribution_conflict`，含跨端吻合证据
7. 重复导入幂等：`reportId`/`issueId` 稳定，重复与跨批次导入均去重，同批次异内容拒绝覆盖
8. 校验记录引用不存在的实例编号
9. 业务端整体缺少某字段实例
10. 片段编号缺口 / 重复编号的重建防护
11. 非法输入（缺字段、非法状态、记录编号重复）

## 目录结构

```
src/digest.js     SHA-256、字段摘要重建、确定性序列化
src/model.js      输入校验与字段序列重建
src/inspector.js  四类判定与确定性报告
src/store.js      批次导入与幂等去重
src/cli.js        命令行入口
test/             node:test 测试
examples/         网关/业务端示例事件（含错位归属复现）
```
