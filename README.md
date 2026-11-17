# CSP3 脚本判定库

面向调试场景的 Content Security Policy Level 3（CSP3）脚本执行判定库。输入文档来源、
多份策略原始文本与按创建顺序排列的脚本记录，输出**每份策略各自**使用的指令、匹配依据、
违规信息，以及求交后的最终执行状态，用于回答“这个脚本到底被哪一份策略拦住”。

## 目录结构

```
lib/csp3.mjs           判定库（无外部依赖，Node 内置 crypto 计算 SHA-256）
tools/cases.mjs        C01–C04 合成样本与真实断言
tools/local_verify.mjs 本地核验入口（--list / --case，JSON 输出、非零退出）
```

## 实际 API

### `evaluate({ origin, policies, scripts })`

```js
import { evaluate, sha256Base64, parsePolicy, classifySource } from './lib/csp3.mjs';

const result = evaluate({
  origin: 'https://app.test',
  policies: [
    { id: 'gw',  mode: 'enforce',      text: "script-src https://cdn.test" },
    { id: 'app', mode: 'report-only',  text: "script-src 'strict-dynamic' 'nonce-N1'" },
  ],
  scripts: [
    { id: 's1', kind: 'external', url: 'https://cdn.test/a.js',
      parserInserted: true, nonce: 'N1' },
    { id: 's2', kind: 'inline', body: 'var a=1;',
      parserInserted: false, creator: 's1' },
  ],
});
```

输入字段：

- `origin`：文档来源（含 scheme），用于 `'self'` 与裸主机源的 scheme 继承。
- `policies[].mode`：`'enforce'`（默认）或 `'report-only'`。
- `scripts[]` **必须按创建顺序排列**；每条记录：
  - `kind: 'inline' | 'external'`
  - 内联给 `body`，外部给 `url`
  - `nonce?`、`parserInserted: boolean`、`creator?`（创建者脚本 id，无则省略）

返回结构（节选）：

```jsonc
{
  "origin": "...",
  "policies": [{
    "id": "gw", "mode": "enforce",
    "effectiveDirective": "script-src",
    "effectiveDirectiveText": "script-src https://cdn.test",
    "duplicates": [{ "directive": "script-src", "ignoredText": "..." }],
    "unsupported": [{ "directive": "script-src", "token": "'unsafe-inline'",
                     "reason": "unsupported-keyword" }]
  }],
  "scripts": [{
    "scriptId": "s2",
    "executes": false,                 // 仅对 enforce 策略求交
    "decisions": [{
      "policyId": "gw", "mode": "enforce",
      "directive": "script-src", "directiveText": "...",
      "allowed": false,
      "basis": [],                     // 放行依据，如 "nonce" / "sha256" /
                                       // "host:https://cdn.test" /
                                       // "strict-dynamic:creator:s1" /
                                       // "implicit-allow:no-script-directive"
      "reason": "blocked:no-matching-host-source"
    }],
    "reports": [{ "policyId": "gw", "disposition": "enforce",
                  "effectiveDirective": "script-src", "reason": "...",
                  "blockedReference": "https://..." }]
  }]
}
```

另有导出 `sha256Base64(text)`、`parsePolicy(text)`、`classifySource(token)` 可单独使用。

## 实现的 CSP3 语义

1. **指令回退**：`script-src-elem` → `script-src` → `default-src`，取最先出现者。
2. **重复指令**：同一策略内同名指令按 CSP3 取第一份，后续整体忽略并记入 `duplicates`。
3. **来源表达式**：
   - `'self'`、`'none'`（仅当其为唯一 token 时阻断）、`'strict-dynamic'`
   - `'nonce-<base64>'`（大小写敏感比对，优先于主机源，可与 `'none'` 共存）
   - `'sha256-<base64>'`（仅对内联正文生效，`node:crypto` 实算）
   - HTTPS 主机源：`https://host[:port][/path]`、`*.host` 通配子域、裸主机源
     （scheme 继承文档来源）、`https:` scheme-source
4. **strict-dynamic**：存在时忽略 self/主机/scheme 白名单；仅 nonce/hash 或
   “本策略下已受信创建者”可授信；解析器插入脚本不继承创建者信任；信任链可沿
   非解析器脚本逐级传播。
5. **逐策略独立信任、再求交**：每份策略维护独立受信集合，A 策略下受信的创建者
   不能替 B 策略授信；`executes` 仅对所有 enforce 判定取与。
6. **Report-Only**：照常产出 `reports`（`disposition: 'report-only'`），但不参与
   `executes` 求交，即永不阻断。
7. **不支持语法显式上报**：未知指令、`'unsafe-inline'` 等关键字、sha384/sha512、
   非 https scheme、`*`、畸形 nonce/hash/主机等进入 `unsupported` 并给出 reason，
   绝不静默当作放行依据。

## 本地核验命令

```bash
node tools/local_verify.mjs --list
node tools/local_verify.mjs --case C01
node tools/local_verify.mjs --case C02
node tools/local_verify.mjs --case C03
node tools/local_verify.mjs --case C04
# 也支持逗号分隔多选
node tools/local_verify.mjs --case C01,C02,C03,C04
```

输出为 JSON 记录数组，每条含 `case`、`assertionCount`、`passed/failed/skipped`、
`status`、合成种子与样本摘要、每条断言的期望/观察值、以及库实际返回的策略摘要。
nonce 值在输出中一律脱敏为 `***`；样本全部为本地合成数据，不含真实账户或凭据。

退出码：全部断言通过为 `0`；任一断言失败、入口缺失、用例未实现、零断言、
整组跳过或用例 id 未知均为非零。核验包装器只是运行入口，审计时仍应阅读
`lib/csp3.mjs` 源码与真实返回。

## 可重放样本（C01–C04）

样本与断言均在 `tools/cases.mjs`，无随机性、无网络访问：

| 用例 | 场景 | 断言数 |
|------|------|--------|
| C01 | 回退链、重复指令取第一份、`'self'`/`'none'`/HTTPS 主机（含通配子域）、五类不支持语法上报、多策略求交 | 17 |
| C02 | nonce 优先、SHA-256 仅内联、strict-dynamic 传播/解析器脚本不继承、无 strict-dynamic 不传播 | 10 |
| C03 | 逐策略信任集合：动态子脚本仅 strict-dynamic 策略通过、主机策略拦截、跨策略授信被拒、信任链两级传播 | 10 |
| C04 | Report-Only 不阻断但产违规、指令回退在 RO 下同样生效、enforce 阻断优先、每判定可追溯指令与依据 | 10 |

关键可重放片段：

```js
// nonce 放行与另一份主机限制相冲突（C03/C04 均有体现）
{ id: 'p1', text: "script-src 'strict-dynamic' 'nonce-N1'" }
{ id: 'p2', text: "script-src https://cdn.test" }
// loader 两策略均通过；其动态子脚本仅 p1 通过 → executes=false

// 动态子脚本只在部分策略通过（C03）
{ id: 'child', kind: 'external', url: 'https://evil.example/child.js',
  parserInserted: false, creator: 'loader' }
```

## 未覆盖边界（明确不在本次实现范围）

- 非脚本类指令（`style-src`、`img-src` 等）只识别名称，不做判定；`sandbox`、
  `report-uri`、`report-to`、`trusted-types` 等同理。
- 不支持 `'unsafe-inline'`/`'unsafe-eval'`/`'unsafe-hashes'`、sha384/sha512、
  `data:`/`blob:` 等非 https scheme、裸 `*`、`'report-sample'`、
  `'unsafe-allow-redirects'`、关键字 CSP 版本回退规则（CSP2 下 nonce/hash
  使 unsafe-inline 失效的历史行为）——遇到即上报 unsupported。
- 主机源未实现查询串/片段比较、IDNA/ punycode 归一化、百分号编码归一化与
  IPv6 字面量；URL 比较基于 WHATWG `URL` 解析结果，路径仅做前缀匹配。
- 不处理 `frame-ancestors`、CORS、重定向链、响应头多值拆分的传输层细节，
  也不模拟真实 DOM/事件循环；脚本“是否解析器插入/创建者是谁”完全由输入
  记录决定。
- 不生成浏览器格式的 `SecurityPolicyViolationEvent`/`report-to` 上报体，
  只输出本库自定义的违规摘要。
- 输入假设为可信调用方提供的结构化记录；库不防御原型污染等调用侧问题。
