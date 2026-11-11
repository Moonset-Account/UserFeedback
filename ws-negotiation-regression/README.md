# WebSocket 扩展协商离线回归套件

针对某服务启用新 WebSocket 扩展组合（RFC 6455 握手 + RFC 7692 `permessage-deflate`）的
**离线协商回归**。不发起任何网络请求，只对三份数据做回归审计：

1. **解析后的客户端声明**（`Sec-WebSocket-Protocol` / `Sec-WebSocket-Extensions` 原始头 → 结构化声明）；
2. **服务端能力表**（两代策略快照：`v1-legacy` / `v2-fixed`，含资源预算与参数合法区间）；
3. **响应决策记录**（每个生效参数的取值、来源、适用规则、预算绑定与最终判定）。

## 背景缺陷

旧策略在切换可选扩展时，**默认路由分支遗失了资源限制**（本套件编号 PM-6）：

- 配置分支会把窗口位数钳制到消息处理预算 `maxWindowBits=12`、强制 `no_context_takeover`、
  绑定 1 MiB 解压预算；
- 默认分支走遗留回退，原样回显对等端声明（最高 15 位窗口），不注入强制项、不绑定解压预算，
  导致协商参数超出服务端为消息处理预设的资源预算（压缩上下文内存超配 2^15/2^12 = 8 倍，
  且解压炸弹无字节上限）。

修正策略 v2：删除无约束回退，所有分支统一预算路径，并新增声明完整性闸门（PM-5）。

## 运行

要求 Node.js >= 20（开发环境 Node 24），零依赖。

```bash
npm test       # node:test 回归，31 项断言
npm run report # 生成 artifacts/negotiation-report.md 与 .json，含回归门禁退出码
```

`npm run report` 的退出码即门禁：v2 任何 ACCEPT 决策预算不安全、或 v1 默认分支未复现失效，均退出 1。

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `src/constants.js` | 判定、参数来源、策略条目（SP/EX/PM 规则）等术语表 |
| `src/parser.js` | 离线头解析与 RFC 级语法校验（与策略判定分离） |
| `src/capabilities.js` | v1/v2 服务端能力表与资源预算快照 |
| `src/negotiation-common.js` | 两代共用的子协议选择（SP-1~SP-3）与响应头序列化 |
| `src/policy-v1.js` | 旧策略：配置分支（正确）+ 默认分支（复现遗留缺陷） |
| `src/policy-v2.js` | 修正策略：统一预算绑定 + PM-5 完整性闸门 |
| `src/auditor.js` | 独立审计器：对照真实资源预算核验决策，输出双轨结论 |
| `test/fixtures.js` | 10 个离线捕获夹具与三列预期矩阵 |
| `test/negotiation.test.js` | 31 项 node:test 断言 |
| `scripts/run-offline.js` | 回归入口：跑矩阵、审计、出 Markdown/JSON 报告、门禁 |

## 审计器的双轨结论（关键设计）

审计不采信协商引擎的自我声明，而是拿真实资源预算重新核验，输出两个独立结论：

- `policyConformant`：决策是否符合其声称使用的能力表条目；
- `budgetSafe`：决策是否真正落在资源预算内。

v1 默认分支因此呈现 `policyConformant=true` 而 `budgetSafe=false`——
证明问题是**策略表本身缺失限制**，而非实现偏离策略。

## 规则与用例覆盖

| 规则 | 含义 | 对应用例 |
| --- | --- | --- |
| SP-1/2/3 | 子协议顺序选择 / 无交集降级 / 未声明 | TC-01 ~ TC-03 |
| EX-1/EX-2 | 可选扩展按策略省略 / 畸形声明拒绝 | TC-04、TC-05、TC-10 |
| PM-1/PM-2 | 窗口钳制与合法区间（RFC ∩ 预算） | TC-06 ~ TC-08 |
| PM-3/PM-4 | 强制无上下文接管 / 解压预算绑定 | TC-06 ~ TC-08 |
| PM-5 | 声明不完整 → PENDING_CONFIRMATION 单列待确认 | TC-09 |
| PM-6 | 默认分支同样适用 PM-1..PM-4（v1 缺陷根因） | TC-08 |

参数来源（provenance）共 7 类：客户端声明、服务端能力表、策略默认、策略钳制、
策略强制注入、RFC 缺省、旧策略遗留回显——每个生效参数在 JSON 报告中均可追溯。
