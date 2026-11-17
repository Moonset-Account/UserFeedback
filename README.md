# html-template — 通知系统小型安全 HTML 模板编译模块

先编译、再渲染的 HTML 模板模块。模板是可信输入，渲染数据是不可信输入；
所有插值按所在上下文做专用编码，渲染值无法改变已编译确认的标签/属性结构。

## 文件

| 路径 | 说明 |
| --- | --- |
| `src/html-template.mjs` | 模板编译/渲染模块（纯 ESM，无依赖，Node ≥ 18） |
| `tools/local_verify.mjs` | 本地核验入口（C01–C04 真实断言） |

## 模板语法

- 普通标签：静态标签名 `<div>`、`</div>`、`<br/>`；属性必须带引号（`name="..."` 或 `name='...'`）。
- `{{field}}` 插值：可出现在**文本节点**和**属性值**中；字段支持 `a.b` 点路径；缺失/`null`/`undefined` 渲染为空串。
- `{{#if field}}...{{else}}...{{/if}}` 布尔条件（JS 真值判断），`{{else}}` 可省略。分支可跨标签或引号边界。

## 上下文专用处理规则

| 插值位置 | 规则 |
| --- | --- |
| 文本节点 | HTML 转义：`& < >` |
| 普通属性值 | 属性转义：`& < > " '` |
| `href` / `src`（URL 属性） | 先校验 URL，再做属性转义 |

URL 白名单规则（`validateUrl`）：

- 仅接受 `http://`、`https://`（协议名大小写不敏感）或以**单个** `/` 开头的站内路径。
- 拒绝：控制字符（码位 `< 0x20` 及 `0x7F`）、反斜杠、协议相对地址（`//host`）、空串、其他协议（`javascript:`、`ftp:` 等）。

## 编译期拒绝（`TemplateCompileError`，含 `offset`/`line`/`col`）

- 动态标签名 `<{{tag}}>`、动态属性名 `<div {{a}}="x">`
- 事件属性（`on` 前缀，如 `onclick`、`onmouseover`，无论静态还是插值）
- `<script>` / `<style>` 内的 `{{...}}` 插值
- if/else 两条分支**结束时的解析上下文不一致**（见下）
- 未闭合标签/属性/`{{#if}}`、游离 `{{else}}`/`{{/if}}`、无引号属性、未知指令

### if/else 分支上下文规则

两条分支在渲染时二选一，因此词法上**都从 `{{#if}}` 处的解析上下文开始**；
`{{else}}` 处恢复该快照。编译继续的条件：then 分支结束于 `{{else}}` 时的上下文
与 else 分支结束于 `{{/if}}` 时的上下文一致（无 `{{else}}` 时，要求 then 分支
结束上下文与 `{{#if}}` 处一致）。上下文描述包含：text / tag（含当前标签名）/
attr-value（含引号字符与属性名）/ rawtext（含标签名）。

可编译（跨引号，两分支均结束于 `attr-value(") of "title"`）：

```
{{#if a}}<div title="{{else}}<div title="b{{/if}}">x</div>
  a=true  → <div title="">x</div>
  a=false → <div title="b">x</div>
```

拒绝（then 结束于属性值内，else 结束于文本）：

```
{{#if a}}<div title="{{else}}<div title="x">{{/if}}
→ TemplateCompileError: if/else branches end in different parse contexts
  ("attr-value(\") of \"title\"" vs "text") (line 1, col 45)，offset 指向 {{/if}}
```

## 错误分层（调用方可区分）

```
Error
├── TemplateCompileError   模板结构错误（编译期，含 offset/line/col）
└── RenderError
    └── InvalidUrlError    某次渲染数据中 URL 字段非法（含 field）
```

```js
import { compile, TemplateCompileError, InvalidUrlError } from './src/html-template.mjs';

let tpl;
try {
  tpl = compile('<a href="{{u}}">go</a>');
} catch (e) {
  // e instanceof TemplateCompileError → 模板本身有问题，e.line/e.col 定位
}
try {
  tpl.render({ u: 'javascript:alert(1)' });
} catch (e) {
  // e instanceof InvalidUrlError → 这次数据非法，模板仍然可用
}
```

## API

- `compile(source: string) → { render(data), source, ast }`，失败抛 `TemplateCompileError`。
- `render(compiled, data) → string`（等价于 `compiled.render(data)`），URL 非法抛 `InvalidUrlError`。
- `validateUrl(value, field?) → string`，`escapeHtml(s)`，`escapeAttr(s)` 辅助导出。
- 错误类：`TemplateCompileError`、`RenderError`、`InvalidUrlError`。

## 本地核验

```sh
node tools/local_verify.mjs --list        # 列出用例
node tools/local_verify.mjs --case C01    # 运行单个用例（C01/C02/C03/C04）
node tools/local_verify.mjs --all         # 全部
```

每个用例输出一条 JSON 记录：`case`、`assertions`（实际断言数）、`passed/failed/skipped`、
合成样本（`sample.seed`/模板/数据）以及逐条断言的期望/观察摘要。任一断言失败、
用例缺失、零断言均以非零码退出（1=断言失败，2=用法/未知用例）。样本全部为本地合成数据。

- **C01**（7 断言）：同一字段 `x` 进入文本、`title`（普通属性）、`href`（URL 属性）三种上下文，
  校验上下文专用编码差异（文本中 `"` 不转义、属性中 `&quot;`）与结构保持（恰好一对 `<a>`）。
- **C02**（19 断言）：6 个合法 URL（http/https/大写协议/站内路径/`/`/带 query+fragment）精确输出；
  10 个非法 URL（协议相对、`javascript:` 及其大小写、`ftp:`、TAB/LF/0x01 控制字符、反斜杠、空串）
  均抛 `InvalidUrlError`；并验证 `TemplateCompileError` 与 `RenderError` 层级互不相交。
- **C03**（11 断言）：跨引号、跨标签 if/else 编译与双分支渲染；上下文冲突、无 else 跨边界、
  未闭合 if、游离 else 的编译错误及错误位置（`offset` 指向 `{{/if}}`/`{{#if}}`）。
- **C04**（11 断言）：动态标签名/属性名、事件属性（静态与插值）、script/style 插值均编译期拒绝；
  恶意渲染值 `"><script>alert(1)</script><div x="` 被完全转义、不引入新标签或属性引号；
  `src` 同属 URL 属性。

## 可重放样本

```js
import { compile } from './src/html-template.mjs';

// 同一字段三种上下文（C01 样本）
compile('<a class="link" href="{{x}}" title="{{x}}">{{x}}</a>')
  .render({ x: '/p?a=1&b="2"<3>' });
// → <a class="link" href="/p?a=1&amp;b=&quot;2&quot;&lt;3&gt;"
//     title="/p?a=1&amp;b=&quot;2&quot;&lt;3&gt;">/p?a=1&amp;b="2"&lt;3&gt;</a>

// 跨标签分支（C03 样本）
compile('{{#if a}}<b>{{else}}<i>{{/if}}x{{#if a}}</b>{{else}}</i>{{/if}}')
  .render({ a: true });   // → <b>x</b>；a 为假 → <i>x</i>
```

## 未覆盖边界（已知限制）

- 不支持 HTML 注释 `<!---->`、doctype、CDATA；`<` 后非字母/`/` 一律视为模板错误。
- 不支持无引号属性（一律拒绝）；属性值内不支持的指令（如 `{{#each}}`）视为未知指令。
- URL 属性仅 `href`/`src`；`srcset`、`formaction`、`action`、`poster` 等按普通属性处理（不校验 URL）。
- `style` 属性按普通属性转义，不做 CSS 语义校验；`<script>`/`<style>` 仅允许纯静态内容。
- 不校验标签配对/嵌套合法性（如 `</div>` 无对应开标签不报错）；rawtext 结束标签仅识别 `</script>`/`</style>`（允许空白后 `>`）。
- 条件判断为 JS 真值；字段点路径拒绝 `__proto__`/`constructor`/`prototype` 段（返回空串）。
- 渲染值只做转义与 URL 白名单校验，不做 HTML 规范化；模板本身被视为可信输入。
