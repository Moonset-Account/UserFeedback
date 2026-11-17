/**
 * html-template.mjs — 小型安全 HTML 模板编译模块。
 *
 * 用法：
 *   import { compile, render } from './src/html-template.mjs';
 *   const tpl = compile('<a href="{{u}}" title="{{t}}">{{t}}</a>');
 *   const html = tpl.render({ u: 'https://example.com', t: 'hi' });
 *
 * 模板语法：
 *   - 普通标签（静态标签名）、带引号的属性（attr="..." 或 attr='...'）
 *   - {{field}} 插值（支持 a.b 点路径），可出现在文本节点与属性值中
 *   - {{#if field}} ... {{else}} ... {{/if}} 布尔条件分支，可跨标签/引号边界
 *
 * 上下文处理规则：
 *   - 文本节点中的插值：HTML 转义（& < >）
 *   - 普通属性中的插值：属性转义（& < > " '）
 *   - href/src 的 URL 属性中的插值：先校验 URL，再做属性转义。
 *     URL 仅接受 http://、https:// 或以单个 / 开头的站内路径；
 *     控制字符（<0x20、0x7F）、反斜杠、协议相对地址（//host）一律拒绝。
 *
 * 编译期拒绝（TemplateCompileError，带模板位置）：
 *   - 动态标签名 <{{tag}}>、动态属性名 <div {{a}}="x">
 *   - 事件属性（on* 开头，如 onclick）
 *   - <script>/<style> 内的插值
 *   - if/else 两条分支结束时解析上下文不一致
 *
 * 渲染期错误（InvalidUrlError extends RenderError）：
 *   - 某次数据中 URL 字段非法。与模板结构错误（TemplateCompileError）分属
 *     不同的错误层级，调用方可据此区分。
 */

export class TemplateCompileError extends Error {
  constructor(message, offset, source) {
    const { line, col } = lineCol(source, offset);
    super(`${message} (line ${line}, col ${col})`);
    this.name = 'TemplateCompileError';
    this.offset = offset;
    this.line = line;
    this.col = col;
  }
}

export class RenderError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RenderError';
  }
}

export class InvalidUrlError extends RenderError {
  constructor(field, reason) {
    super(`invalid URL for field "${field}": ${reason}`);
    this.name = 'InvalidUrlError';
    this.field = field;
  }
}

function lineCol(src, offset) {
  let line = 1;
  let col = 1;
  const end = Math.min(offset, src.length);
  for (let k = 0; k < end; k++) {
    if (src[k] === '\n') {
      line++;
      col = 1;
    } else {
      col++;
    }
  }
  return { line, col };
}

const FIELD_RE = /^[A-Za-z_$][A-Za-z0-9_$]*(\.[A-Za-z_$][A-Za-z0-9_$]*)*$/;
const URL_ATTRS = new Set(['href', 'src']);
const RAWTEXT_TAGS = new Set(['script', 'style']);
const BLOCKED_PATH_PARTS = new Set(['__proto__', 'constructor', 'prototype']);

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;' };
const ATTR_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export const escapeHtml = (s) => s.replace(/[&<>]/g, (c) => HTML_ESCAPES[c]);
export const escapeAttr = (s) => s.replace(/[&<>"']/g, (c) => ATTR_ESCAPES[c]);

/**
 * 校验 URL 渲染值。合法则原样返回，否则抛 InvalidUrlError。
 * 规则：非空字符串；无控制字符（<0x20、0x7F）；无反斜杠；
 * 必须是 http(s):// 开头，或以单个 / 开头的站内路径（拒绝 // 协议相对地址）。
 */
export function validateUrl(value, field = '?') {
  if (typeof value !== 'string' || value.length === 0) {
    throw new InvalidUrlError(field, 'value must be a non-empty string');
  }
  for (let k = 0; k < value.length; k++) {
    const c = value.charCodeAt(k);
    if (c < 0x20 || c === 0x7f) {
      throw new InvalidUrlError(field, 'control characters are not allowed');
    }
  }
  if (value.includes('\\')) {
    throw new InvalidUrlError(field, 'backslash is not allowed');
  }
  if (/^https?:\/\//i.test(value)) return value;
  if (value.startsWith('//')) {
    throw new InvalidUrlError(field, 'protocol-relative URLs are not allowed');
  }
  if (value.startsWith('/')) return value;
  throw new InvalidUrlError(field, 'only http://, https:// or site-relative /path URLs are allowed');
}

function lookup(data, field) {
  let cur = data;
  for (const part of field.split('.')) {
    if (cur === null || cur === undefined) return undefined;
    if (BLOCKED_PATH_PARTS.has(part)) return undefined;
    cur = cur[part];
  }
  return cur;
}

function renderNodes(nodes, data) {
  let out = '';
  for (const n of nodes) {
    switch (n.t) {
      case 'raw':
        out += n.v;
        break;
      case 'echo': {
        const v = lookup(data, n.field);
        const s = v === null || v === undefined ? '' : String(v);
        if (n.kind === 'html') out += escapeHtml(s);
        else if (n.kind === 'attr') out += escapeAttr(s);
        else out += escapeAttr(validateUrl(s, n.field)); // kind === 'url'
        break;
      }
      case 'if':
        out += renderNodes(lookup(data, n.field) ? n.thenNodes : n.elseNodes, data);
        break;
      default:
        throw new RenderError(`unknown node type: ${n.t}`);
    }
  }
  return out;
}

/**
 * 编译模板。成功返回 { render(data), source, ast }；
 * 失败抛 TemplateCompileError（含 offset/line/col）。
 */
export function compile(source) {
  if (typeof source !== 'string') {
    throw new TypeError('template source must be a string');
  }

  const root = { nodes: [] };
  const ifStack = [];
  const currentNodes = () => {
    if (ifStack.length === 0) return root.nodes;
    const top = ifStack[ifStack.length - 1];
    return top.inElse ? top.elseNodes : top.thenNodes;
  };
  const fail = (msg, offset) => {
    throw new TemplateCompileError(msg, offset, source);
  };

  let i = 0;
  let state = 'text'; // text | tag | attr-value | rawtext
  let quote = null; // 当前属性值的引号字符
  let curAttr = null; // 当前属性名（原始大小写）
  let rawTag = null; // rawtext 对应的标签名
  let pendingTag = null; // 最近一个开始标签名（小写），用于进入 rawtext

  // 解析上下文快照：if/else 的两条分支在渲染时二选一，因此词法上
  // 两条分支都必须从 {{#if}} 处的上下文开始解析；{{else}} 处恢复快照。
  const snapshotCtx = () => ({ state, quote, curAttr, rawTag, pendingTag });
  const restoreCtx = (s) => {
    state = s.state;
    quote = s.quote;
    curAttr = s.curAttr;
    rawTag = s.rawTag;
    pendingTag = s.pendingTag;
  };
  // 当前解析上下文的可比较描述，用于 if/else 分支一致性检查
  const ctxDesc = () => {
    if (state === 'attr-value') return `attr-value(${quote}) of "${curAttr}"`;
    if (state === 'rawtext') return `rawtext(${rawTag})`;
    if (state === 'tag') return `tag(after <${pendingTag})`;
    return 'text';
  };

  // 解析 {{...}} 指令，返回结束后位置
  const readMustache = (start) => {
    const end = source.indexOf('}}', start + 2);
    if (end === -1) fail('unterminated {{...}} directive', start);
    const body = source.slice(start + 2, end).trim();
    let m;
    if ((m = /^#if\s+(\S+)$/.exec(body))) {
      if (!FIELD_RE.test(m[1])) fail(`invalid field name "${m[1]}" in {{#if}}`, start);
      ifStack.push({
        field: m[1],
        pos: start,
        ctxAtIf: snapshotCtx(),
        descAtIf: ctxDesc(),
        inElse: false,
        descAtElse: null,
        thenNodes: [],
        elseNodes: [],
      });
    } else if (body === 'else') {
      if (ifStack.length === 0) fail('{{else}} without matching {{#if}}', start);
      const top = ifStack[ifStack.length - 1];
      if (top.inElse) fail('duplicate {{else}} for the same {{#if}}', start);
      top.inElse = true;
      top.descAtElse = ctxDesc(); // then 分支结束时的解析上下文
      restoreCtx(top.ctxAtIf); // else 分支与 then 分支一样，从 {{#if}} 处的上下文开始
    } else if (body === '/if') {
      if (ifStack.length === 0) fail('{{/if}} without matching {{#if}}', start);
      const top = ifStack.pop();
      // 两条分支结束时的解析上下文必须一致：
      // 有 else 时，then 分支结束于 {{else}}（descAtElse），else 分支结束于当前位置；
      // 无 else 时，else 分支为空，等价于结束于 {{#if}} 处（descAtIf）。
      const expected = top.inElse ? top.descAtElse : top.descAtIf;
      const actual = ctxDesc();
      if (expected !== actual) {
        fail(
          `if/else branches end in different parse contexts ("${expected}" vs "${actual}")`,
          start,
        );
      }
      currentNodes().push({
        t: 'if',
        field: top.field,
        thenNodes: top.thenNodes,
        elseNodes: top.elseNodes,
      });
    } else if (FIELD_RE.test(body)) {
      if (state === 'text') {
        currentNodes().push({ t: 'echo', field: body, kind: 'html' });
      } else if (state === 'attr-value') {
        const kind = URL_ATTRS.has(curAttr.toLowerCase()) ? 'url' : 'attr';
        currentNodes().push({ t: 'echo', field: body, kind });
      } else {
        // 到达此处说明状态机放行有误
        fail('interpolation is not allowed in this position', start);
      }
    } else {
      fail(`unknown directive {{${body}}}`, start);
    }
    return end + 2;
  };

  while (i < source.length) {
    if (state === 'text') {
      const ch = source[i];
      if (ch === '<') {
        const next = source[i + 1];
        if (next === undefined) fail('unterminated tag at end of template', i);
        if (source.startsWith('{{', i + 1)) {
          fail('dynamic tag names are not allowed', i);
        }
        if (next === '/') {
          const m = /^<\/([A-Za-z][A-Za-z0-9-]*)\s*>/.exec(source.slice(i));
          if (!m) fail('malformed closing tag', i);
          currentNodes().push({ t: 'raw', v: m[0] });
          i += m[0].length;
          continue;
        }
        const m = /^<([A-Za-z][A-Za-z0-9-]*)/.exec(source.slice(i));
        if (!m) fail('malformed tag (only static tag names are allowed)', i);
        currentNodes().push({ t: 'raw', v: m[0] });
        pendingTag = m[1].toLowerCase();
        state = 'tag';
        i += m[0].length;
        continue;
      }
      if (ch === '{' && source.startsWith('{{', i)) {
        i = readMustache(i);
        continue;
      }
      let j = i;
      while (j < source.length && source[j] !== '<' && !(source[j] === '{' && source.startsWith('{{', j))) {
        j++;
      }
      currentNodes().push({ t: 'raw', v: source.slice(i, j) });
      i = j;
      continue;
    }

    if (state === 'tag') {
      const ws = /^\s+/.exec(source.slice(i));
      if (ws) {
        currentNodes().push({ t: 'raw', v: ws[0] });
        i += ws[0].length;
        continue;
      }
      const ch = source[i];
      if (ch === '>') {
        currentNodes().push({ t: 'raw', v: '>' });
        i++;
        if (RAWTEXT_TAGS.has(pendingTag)) {
          state = 'rawtext';
          rawTag = pendingTag;
        } else {
          state = 'text';
        }
        continue;
      }
      if (ch === '/' && source[i + 1] === '>') {
        currentNodes().push({ t: 'raw', v: '/>' });
        i += 2;
        state = 'text';
        continue;
      }
      if (source.startsWith('{{', i)) {
        fail('dynamic attribute names are not allowed', i);
      }
      const m = /^[A-Za-z_:][A-Za-z0-9_.:-]*/.exec(source.slice(i));
      if (!m) fail(`unexpected character "${ch}" inside tag`, i);
      const attrName = m[0];
      if (/^on/i.test(attrName)) {
        fail(`event handler attribute "${attrName}" is not allowed`, i);
      }
      let k = i + attrName.length;
      const ws2 = /^\s*/.exec(source.slice(k));
      k += ws2[0].length;
      if (source[k] !== '=') {
        fail(`attribute "${attrName}" must have a quoted value (name="...")`, i);
      }
      k++;
      const ws3 = /^\s*/.exec(source.slice(k));
      k += ws3[0].length;
      const q = source[k];
      if (q !== '"' && q !== "'") {
        fail(`attribute "${attrName}" must use a quoted value`, i);
      }
      currentNodes().push({ t: 'raw', v: source.slice(i, k + 1) }); // 属性名 + = + 开引号，原样保留
      curAttr = attrName;
      quote = q;
      state = 'attr-value';
      i = k + 1;
      continue;
    }

    if (state === 'attr-value') {
      const ch = source[i];
      if (ch === quote) {
        currentNodes().push({ t: 'raw', v: quote });
        i++;
        state = 'tag';
        quote = null;
        continue;
      }
      if (ch === '{' && source.startsWith('{{', i)) {
        i = readMustache(i);
        continue;
      }
      let j = i;
      while (j < source.length && source[j] !== quote && !(source[j] === '{' && source.startsWith('{{', j))) {
        j++;
      }
      currentNodes().push({ t: 'raw', v: source.slice(i, j) });
      i = j;
      continue;
    }

    // state === 'rawtext'（script/style 内容）
    {
      const closeRe = new RegExp(`</${rawTag}\\s*>`, 'ig');
      closeRe.lastIndex = i;
      const m = closeRe.exec(source);
      const endIdx = m ? m.index : source.length;
      const mustache = source.indexOf('{{', i);
      if (mustache !== -1 && mustache < endIdx) {
        fail(`interpolation inside <${rawTag}> is not allowed`, mustache);
      }
      if (!m) fail(`unterminated <${rawTag}> element`, i);
      if (endIdx > i) currentNodes().push({ t: 'raw', v: source.slice(i, endIdx) });
      currentNodes().push({ t: 'raw', v: m[0] });
      i = endIdx + m[0].length;
      state = 'text';
      rawTag = null;
      continue;
    }
  }

  if (state === 'tag' || state === 'attr-value') {
    fail('unterminated tag/attribute at end of template', i);
  }
  if (state === 'rawtext') {
    fail(`unterminated <${rawTag}> element`, i);
  }
  if (ifStack.length > 0) {
    const top = ifStack[ifStack.length - 1];
    fail(`unclosed {{#if ${top.field}}}`, top.pos);
  }

  const ast = root.nodes;
  return {
    source,
    ast,
    render(data) {
      return renderNodes(ast, data === null || data === undefined ? {} : data);
    },
  };
}

/** 便捷函数：render(compile(tpl), data) 或对已编译模板再次渲染。 */
export function render(compiled, data) {
  if (!compiled || typeof compiled.render !== 'function') {
    throw new TypeError('render() expects a compiled template returned by compile()');
  }
  return compiled.render(data);
}
