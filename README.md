# Trusted Types 策略覆盖审计（SEC-VULN-027）

离线检查工具：读取各页面入口的构建清单、CSP 策略记录、动态模块依赖与已标注的 DOM
写入点清单，按入口判定敏感 DOM 写入点是否落在预期的 Trusted Types 保护之下，并对照
两版构建快照输出 CI 可读的 JSON 报告。纯 Node.js 标准模块实现，无任何外部依赖。

## 运行环境

- Node.js（开发与核验使用 v24.15.0），仅使用 `node:test`、`node:assert`、`node:fs` 等标准模块。
- 不需要 `npm install`，不联网。

## 快速开始

```bash
# 对照两版构建快照（diff 模式），JSON 输出到 stdout
node app.mjs --snapshot-a fixtures/snapshot-a --snapshot-b fixtures/snapshot-b

# 单快照审计
node app.mjs --snapshot fixtures/snapshot-b

# 报告写入文件；存在任何 finding 时以退出码 2 结束（CI 门禁）
node app.mjs --snapshot-a fixtures/snapshot-a --snapshot-b fixtures/snapshot-b \
  --out report.json --fail-on-findings
```

退出码：`0` 成功；`1` 参数或快照读取错误；`2` 指定 `--fail-on-findings` 且存在 findings。

## 输入格式（每份快照一个目录，5 个 JSON 清单）

| 文件 | 结构 | 说明 |
| --- | --- | --- |
| `entries.json` | `{ "entries": [{ name, loadPath, sharedModules[], lazyModules[] }] }` | 页面入口及其共享/懒加载模块 |
| `csp.json` | `{ "entries": { <entry>: { mode, requireTrustedTypesForScript, allowedPolicies[] } } }` | 每入口 CSP 记录；`mode` 为 `enforce` 或 `report-only` |
| `policies.json` | `{ "policies": { <policy>: { creates: ["TrustedHTML", ...] } } }` | Trusted Types 策略定义及其可创建的可信类型 |
| `sinks.json` | `{ "sinks": [{ id, module, sink, requiredType, actualPolicy, location{file,line} }] }` | 已标注的 DOM 写入点：所需可信类型、实际调用策略、源码位置 |
| `dynamic-deps.json` | `{ "deps": { <lazyModule>: [<dynamicModule>, ...] } }` | 懒加载模块的动态依赖（传递展开） |

`fixtures/snapshot-a`、`fixtures/snapshot-b` 为合成示例：快照 B 相对 A 将 checkout
入口从 Report-Only 切换为强制、新增 account 入口及动态模块 `stats-widget` 的新写入点。

## 判定语义（每个写入点 × 每个加载它的入口）

按以下顺序判定，命中即定：

1. `insufficient_info` — 写入点标注缺字段（`requiredType`/`sink`/`module`）、入口缺 CSP
   记录、或实际策略缺少策略定义，清单信息不足以下结论。
2. `report_only` — CSP 为 Report-Only，仅上报不拦截。
3. `not_enforced` — 未设置 `require-trusted-types-for 'script'`。
4. `no_policy` — 写入点未经由任何 Trusted Types 策略。
5. `policy_not_authorized` — 实际策略名未列入该入口 `trusted-types` 指令白名单。
6. `type_mismatch` — 策略已获准，但其 `creates` 不含写入点所需的可信类型。
7. `covered` — 以上均通过。

## 快照对照（--snapshot-a/--snapshot-b）

- **共享模块跨入口保护状态**：对两版均为共享的模块，比较其在各入口的保护状态是否一致
  （同一模块在入口 X 全覆盖、在入口 Y 未覆盖即 `shared_module_inconsistent`），并标记
  A 版不一致、B 版已对齐的收敛项（`resolved`）。
- **动态模块新增写入点**：B 中新出现且属于懒加载/动态模块的写入点，逐一给出各入口的
  覆盖判定；未覆盖者产生 `new_dynamic_sink_uncovered` finding。
- **覆盖回归**：同一入口同一写入点由 `covered` 退化为其他状态即 `coverage_regression`。

## 输出语义（CI 可读 JSON）

```jsonc
{
  "tool": "trusted-types-coverage-audit",
  "schemaVersion": 1,
  "generatedAt": "…",
  "snapshots": { "base": "snapshot-a", "current": "snapshot-b" },
  "summary": { "entries": 3, "sinkEvaluations": 12, "covered": 5,
               "uncovered": 7, "byStatus": {…}, "findings": 9, "byCategory": {…} },
  "entries": { "<entry>": { "loadPath", "modules", "sinks", "summary" } },
  "diff": { "sharedModules", "newDynamicSinks", "regressions", "findings" },
  "findings": [
    {
      "id": "policy_not_authorized:home:home-lazy:s3",  // 稳定 ID，可用于 CI 基线比对
      "category": "policy_not_authorized",
      "severity": "error",                              // error | warning
      "entries": ["home"],                              // 受影响入口
      "loadPaths": ["/home → home-lazy"],               // 加载路径（入口 → 模块链）
      "module": "home-lazy",
      "sinkId": "s3",
      "location": { "file": "home-lazy/src/banner.js", "line": 7 },
      "detail": "…",
      "fixPoint": "csp.json → entries[\"home\"].allowedPolicies: …"  // 配置修复点
    }
  ]
}
```

severity 映射：`policy_not_authorized` / `type_mismatch` / `no_policy` / `not_enforced` /
`new_dynamic_sink_uncovered` / `coverage_regression` 为 `error`；`report_only` /
`insufficient_info` / `shared_module_inconsistent` 为 `warning`。

## 测试与本地核验

```bash
# 运行测试（node:test，20 个用例，含稳定 case_id TT-001…TT-105）
node --test 'tests/**/*.test.mjs'

# 本地核验包装器：真实执行测试与 CLI，校验实际执行测试数 ≥ 1、
# 无失败/取消/全跳过，并保留原始 TAP 输出；任何一项不满足即以非零退出
node tools/local_verify.mjs
```

需求点到测试的映射：

| 需求 | 用例 |
| --- | --- |
| CSP 仅报告 | TT-001、TT-015、TT-102 |
| 策略名称未获授权 | TT-002 |
| 实际类型与写入要求不符 | TT-003 |
| 清单信息不足 | TT-004、TT-008 |
| 覆盖判定 / 未经策略 | TT-005、TT-006、TT-007 |
| 动态模块依赖传递展开 | TT-009 |
| 共享模块跨入口保护状态（两版对照） | TT-010、TT-011 |
| 动态模块新增写入点覆盖判定 | TT-012 |
| 覆盖回归 | TT-013、TT-014 |
| CI 可读 JSON / 退出码 / 文件输出 | TT-101、TT-103、TT-104、TT-105 |

## 限制

- 判定基于构建期清单与标注数据，不执行页面运行时检测；未标注的写入点不在覆盖范围。
- `insufficient_info` 表示清单信息不足，需先补齐标注/记录再下结论。
- CSP 记录按入口静态建模（`mode`、`requireTrustedTypesForScript`、`allowedPolicies`），
  不解析原始 CSP 头字符串。
