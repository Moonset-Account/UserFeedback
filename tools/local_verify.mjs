#!/usr/bin/env node
/**
 * local_verify.mjs — 本地核验入口。
 *
 * 用法：
 *   node tools/local_verify.mjs --list          列出全部用例
 *   node tools/local_verify.mjs --case C01      运行单个用例（C01..C04）
 *   node tools/local_verify.mjs --all           运行全部用例
 *
 * 每个用例输出一条 JSON 记录：case、断言总数、passed/failed/skipped、
 * 合成样本（seed/sample）以及每条断言的期望/观察摘要。
 * 任一断言失败、用例缺失、零断言或整组跳过均以非零码退出。
 * 所有样本均为本地合成数据，不包含凭据或真实账户内容。
 */
import {
  compile,
  render,
  TemplateCompileError,
  RenderError,
  InvalidUrlError,
} from '../src/html-template.mjs';

const summ = (v) => {
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  if (s === undefined) return String(v);
  return s.length > 160 ? `${s.slice(0, 157)}...` : s;
};

const check = (name, pass, expected, observed) => ({
  name,
  pass: Boolean(pass),
  expected: summ(expected),
  observed: summ(observed),
});

/** 期望 fn 抛出 cls 的实例；返回 {threw, instance, error}。 */
const expectThrow = (fn, cls) => {
  try {
    fn();
  } catch (e) {
    return { threw: true, instance: e instanceof cls, error: e };
  }
  return { threw: false, instance: false, error: null };
};

const count = (s, re) => (s.match(re) || []).length;

/* ---------------------------------------------------------------- C01 */
function runC01() {
  const checks = [];
  const tpl = '<a class="link" href="{{x}}" title="{{x}}">{{x}}</a>';
  const x = '/p?a=1&b="2"<3>';
  const t = compile(tpl);
  const out = t.render({ x });

  const expected =
    '<a class="link" href="/p?a=1&amp;b=&quot;2&quot;&lt;3&gt;" ' +
    'title="/p?a=1&amp;b=&quot;2&quot;&lt;3&gt;">/p?a=1&amp;b="2"&lt;3&gt;</a>';

  checks.push(check('同一字段在三种上下文渲染结果与期望完全一致', out === expected, expected, out));
  checks.push(check(
    '结构保持：恰好一个 <a> 开始标签与一个 </a>',
    count(out, /<a /g) === 1 && count(out, /<\/a>/g) === 1,
    '1 个 <a + 1 个 </a>',
    `${count(out, /<a /g)} 个 <a + ${count(out, /<\/a>/g)} 个 </a>`,
  ));

  const hrefValue = /href="([^]*)"\s+title=/.exec(out)?.[1] ?? '';
  const textValue = />([^]*)<\/a>/.exec(out)?.[1] ?? '';
  checks.push(check(
    'URL 属性上下文：引号被属性转义（&quot;）',
    hrefValue.includes('&quot;') && hrefValue.includes('&amp;') && hrefValue.includes('&lt;'),
    'href 含 &quot; &amp; &lt;',
    hrefValue,
  ));
  checks.push(check(
    '文本上下文：引号不转义、& 与 < 转义（上下文专用编码不同）',
    textValue === '/p?a=1&amp;b="2"&lt;3&gt;' && hrefValue !== textValue,
    '/p?a=1&amp;b="2"&lt;3&gt;',
    textValue,
  ));
  checks.push(check(
    '普通属性上下文（title）与 URL 属性使用相同的属性转义',
    out.includes('title="/p?a=1&amp;b=&quot;2&quot;&lt;3&gt;"'),
    'title 属性转义与 href 一致',
    out,
  ));

  const bad = expectThrow(() => t.render({ x: '"><script>alert(1)</script>' }), InvalidUrlError);
  checks.push(check(
    '同一编译产物：URL 上下文拒绝非法渲染值（InvalidUrlError）',
    bad.threw && bad.instance,
    'throw InvalidUrlError',
    bad.threw ? bad.error.name : 'no throw',
  ));
  checks.push(check(
    '渲染期 URL 错误不属于模板结构错误（错误层级可区分）',
    bad.threw && bad.instance && !(bad.error instanceof TemplateCompileError) && bad.error instanceof RenderError,
    'InvalidUrlError instanceof RenderError, not TemplateCompileError',
    bad.threw ? bad.error.name : 'no throw',
  ));

  return {
    sample: { seed: 'C01-synthetic', template: tpl, data: { x } },
    checks,
  };
}

/* ---------------------------------------------------------------- C02 */
function runC02() {
  const checks = [];
  const tpl = '<a href="{{u}}">go</a>';
  const t = compile(tpl);

  const valid = [
    ['http://example.com/a?b=1&c=2', '<a href="http://example.com/a?b=1&amp;c=2">go</a>'],
    ['https://example.com/x', '<a href="https://example.com/x">go</a>'],
    ['HTTPS://EXAMPLE.COM/OK', '<a href="HTTPS://EXAMPLE.COM/OK">go</a>'],
    ['/path/to/page', '<a href="/path/to/page">go</a>'],
    ['/', '<a href="/">go</a>'],
    ['/p?q=1#frag', '<a href="/p?q=1#frag">go</a>'],
  ];
  for (const [u, expected] of valid) {
    let out;
    let err = null;
    try {
      out = t.render({ u });
    } catch (e) {
      err = e;
    }
    checks.push(check(`合法 URL 接受并保持结构: ${u}`, !err && out === expected, expected, err ? err.name : out));
  }

  const invalid = [
    ['//evil.com/x', 'protocol-relative'],
    ['javascript:alert(1)', 'js scheme'],
    ['JavaScript:alert(1)', 'js scheme 大小写'],
    ['ftp://example.com', '非白名单协议'],
    ['ht\ttp://example.com', '控制字符 TAB'],
    ['https://ok/\nnext', '控制字符 LF'],
    ['\x01http://example.com', '控制字符 0x01'],
    ['https:\\evil.com', '反斜杠'],
    ['', '空字符串'],
    ['/\\back', '路径含反斜杠'],
  ];
  for (const [u, why] of invalid) {
    const r = expectThrow(() => t.render({ u }), InvalidUrlError);
    checks.push(check(
      `非法 URL 拒绝（${why}）`,
      r.threw && r.instance,
      'throw InvalidUrlError',
      r.threw ? `${r.error.name}: ${r.error.message}` : 'no throw',
    ));
  }

  const badTpl = '<a href={{u}}>go</a>';
  const c = expectThrow(() => compile(badTpl), TemplateCompileError);
  checks.push(check(
    '模板结构错误抛 TemplateCompileError（无引号属性）',
    c.threw && c.instance,
    'throw TemplateCompileError',
    c.threw ? c.error.name : 'no throw',
  ));
  checks.push(check(
    '模板错误带位置信息（offset/line/col）',
    c.threw && c.instance && Number.isInteger(c.error.offset) && c.error.line >= 1 && c.error.col >= 1,
    'integer offset, line>=1, col>=1',
    c.threw && c.instance ? `offset=${c.error.offset} line=${c.error.line} col=${c.error.col}` : 'n/a',
  ));
  checks.push(check(
    '两类错误可分：TemplateCompileError 不是 RenderError，InvalidUrlError 反之',
    c.threw && c.instance && !(c.error instanceof RenderError) && !(new InvalidUrlError('u', 'x') instanceof TemplateCompileError),
    'disjoint error hierarchies',
    'disjoint',
  ));

  return {
    sample: { seed: 'C02-synthetic', template: tpl, validCount: valid.length, invalidCount: invalid.length },
    checks,
  };
}

/* ---------------------------------------------------------------- C03 */
function runC03() {
  const checks = [];

  // 跨引号边界：两条分支都结束于同一属性值上下文
  const t1src = '{{#if a}}<div title="{{else}}<div title="b{{/if}}">x</div>';
  const t1 = compile(t1src);
  checks.push(check('跨引号 if/else（分支结束上下文一致）编译成功', typeof t1.render === 'function', 'compiled', 'compiled'));
  const t1a = t1.render({ a: true });
  const t1b = t1.render({ a: false });
  checks.push(check('分支 A 渲染结果', t1a === '<div title="">x</div>', '<div title="">x</div>', t1a));
  checks.push(check('分支 B 渲染结果', t1b === '<div title="b">x</div>', '<div title="b">x</div>', t1b));

  // 跨标签边界
  const t2src = '{{#if a}}<b>{{else}}<i>{{/if}}x{{#if a}}</b>{{else}}</i>{{/if}}';
  const t2 = compile(t2src);
  checks.push(check('跨标签 if/else 编译成功', typeof t2.render === 'function', 'compiled', 'compiled'));
  const t2a = t2.render({ a: true });
  const t2b = t2.render({ a: 0 });
  checks.push(check('跨标签分支 A 渲染', t2a === '<b>x</b>', '<b>x</b>', t2a));
  checks.push(check('跨标签分支 B 渲染（假值）', t2b === '<i>x</i>', '<i>x</i>', t2b));

  // 分支结束上下文冲突：then 结束于属性值内，else 结束于文本
  const t3src = '{{#if a}}<div title="{{else}}<div title="x">{{/if}}';
  const c3 = expectThrow(() => compile(t3src), TemplateCompileError);
  checks.push(check(
    '分支上下文冲突被拒绝（TemplateCompileError）',
    c3.threw && c3.instance,
    'throw TemplateCompileError',
    c3.threw ? `${c3.error.name}: ${c3.error.message}` : 'no throw',
  ));
  checks.push(check(
    '冲突错误位置指向 {{/if}}',
    c3.threw && c3.instance && c3.error.offset === t3src.indexOf('{{/if}}'),
    `offset=${t3src.indexOf('{{/if}}')}`,
    c3.threw && c3.instance ? `offset=${c3.error.offset} line=${c3.error.line} col=${c3.error.col}` : 'n/a',
  ));

  // 无 else 时：then 分支结束上下文必须与 {{#if}} 处一致
  const t4src = '{{#if a}}<div title="x{{/if}}';
  const c4 = expectThrow(() => compile(t4src), TemplateCompileError);
  checks.push(check(
    '无 else 的跨边界 if（结束上下文不一致）被拒绝',
    c4.threw && c4.instance,
    'throw TemplateCompileError',
    c4.threw ? c4.error.message : 'no throw',
  ));

  // 未闭合 if
  const t5src = '{{#if a}}x';
  const c5 = expectThrow(() => compile(t5src), TemplateCompileError);
  checks.push(check(
    '未闭合 {{#if}} 报错且位置指向 {{#if}}',
    c5.threw && c5.instance && c5.error.offset === t5src.indexOf('{{#if'),
    `offset=${t5src.indexOf('{{#if')}`,
    c5.threw && c5.instance ? `offset=${c5.error.offset}` : 'n/a',
  ));

  // 游离 else
  const c6 = expectThrow(() => compile('a{{else}}b'), TemplateCompileError);
  checks.push(check(
    '游离 {{else}} 报错',
    c6.threw && c6.instance,
    'throw TemplateCompileError',
    c6.threw ? c6.error.message : 'no throw',
  ));

  return {
    sample: { seed: 'C03-synthetic', templates: [t1src, t2src, t3src, t4src, t5src, 'a{{else}}b'] },
    checks,
  };
}

/* ---------------------------------------------------------------- C04 */
function runC04() {
  const checks = [];

  const rejected = [
    ['<{{tag}}>x</{{tag}}>', '动态标签名'],
    ['<div {{attr}}="x">y</div>', '动态属性名'],
    ['<div onclick="f()">y</div>', '静态事件属性'],
    ['<div onmouseover="{{x}}">y</div>', '带插值的事件属性'],
    ['<script>{{x}}</script>', 'script 内插值'],
    ['<style>{{x}}</style>', 'style 内插值'],
  ];
  for (const [src, why] of rejected) {
    const r = expectThrow(() => compile(src), TemplateCompileError);
    checks.push(check(
      `编译期拒绝：${why}`,
      r.threw && r.instance,
      'throw TemplateCompileError',
      r.threw ? `${r.error.name}: ${r.error.message}` : 'no throw',
    ));
  }

  // 静态 script（无插值）可编译并原样渲染
  const okSrc = '<script>if(a<b){c();}</script>';
  const ok = compile(okSrc);
  const okOut = ok.render({});
  checks.push(check('无插值的 <script> 可编译并原样渲染', okOut === okSrc, okSrc, okOut));

  // 渲染值不能突破已编译结构
  const tpl = '<div title="{{x}}">{{x}}</div>';
  const t = compile(tpl);
  const payload = '"><script>alert(1)</script><div x="';
  const out = t.render({ x: payload });
  const expected =
    '<div title="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;&lt;div x=&quot;">' +
    '"&gt;&lt;script&gt;alert(1)&lt;/script&gt;&lt;div x="</div>';
  checks.push(check('恶意渲染值被完全转义（精确输出）', out === expected, expected, out));
  checks.push(check(
    '渲染值未引入新标签：输出中无 <script，且 <div 仅一个',
    !out.includes('<script') && count(out, /<div/g) === 1,
    '0 个 <script, 1 个 <div',
    `${count(out, /<script/g)} 个 <script, ${count(out, /<div/g)} 个 <div`,
  ));
  checks.push(check(
    '渲染值未引入新属性引号：属性值中的 " 均被转义',
    !/title="[^"]*"/.exec(out)?.[0].includes('"><'),
    'title 属性值内无裸引号',
    /title="[^"]*"/.exec(out)?.[0] ?? 'n/a',
  ));

  const img = compile('<img src="{{u}}">');
  const r = expectThrow(() => img.render({ u: 'javascript:alert(1)' }), InvalidUrlError);
  checks.push(check(
    'src 同属 URL 属性：javascript: 渲染值被拒绝',
    r.threw && r.instance,
    'throw InvalidUrlError',
    r.threw ? r.error.name : 'no throw',
  ));

  return {
    sample: { seed: 'C04-synthetic', template: tpl, data: { x: payload } },
    checks,
  };
}

/* ------------------------------------------------------------- runner */
const CASES = {
  C01: { desc: '同一字段进入文本/普通属性/URL 属性：上下文专用编码与结构保持', run: runC01 },
  C02: { desc: 'URL 规则：http/https/站内路径/协议相对/控制字符，区分非法 URL 与模板错误', run: runC02 },
  C03: { desc: '跨标签/引号的 if/else：分支结束上下文一致性与错误位置', run: runC03 },
  C04: { desc: '拒绝动态标签/属性、事件属性、script/style 插值；渲染值不突破已编译结构', run: runC04 },
};

function runCase(id) {
  const def = CASES[id];
  if (!def) {
    console.error(JSON.stringify({ case: id ?? null, error: `unknown case (expect one of ${Object.keys(CASES).join('/')})` }));
    return 2;
  }
  let result;
  try {
    result = def.run();
  } catch (e) {
    console.log(JSON.stringify({
      case: id,
      description: def.desc,
      assertions: 0,
      passed: 0,
      failed: 1,
      skipped: 0,
      error: `case threw unexpectedly: ${e && e.name}: ${e && e.message}`,
    }, null, 2));
    return 1;
  }
  const checks = result.checks || [];
  const passed = checks.filter((c) => c.pass).length;
  const failed = checks.length - passed;
  const record = {
    case: id,
    description: def.desc,
    sample: result.sample,
    assertions: checks.length,
    passed,
    failed,
    skipped: 0,
    checks,
  };
  console.log(JSON.stringify(record, null, 2));
  if (checks.length === 0 || failed > 0) return 1;
  return 0;
}

function main() {
  const args = process.argv.slice(2);
  if (args.includes('--list')) {
    console.log(JSON.stringify(
      Object.entries(CASES).map(([id, d]) => ({ case: id, description: d.desc })),
      null,
      2,
    ));
    process.exit(0);
  }
  if (args.includes('--all')) {
    let code = 0;
    for (const id of Object.keys(CASES)) {
      const c = runCase(id);
      if (c !== 0) code = c;
    }
    process.exit(code);
  }
  const ci = args.indexOf('--case');
  if (ci !== -1) {
    process.exit(runCase(args[ci + 1]));
  }
  console.error('usage: node tools/local_verify.mjs --list | --case C01|C02|C03|C04 | --all');
  process.exit(2);
}

main();
