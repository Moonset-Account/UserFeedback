#!/usr/bin/env node
// local_verify.mjs — 本地核验入口。
//
// 用法：
//   node tools/local_verify.mjs --list          列出全部用例
//   node tools/local_verify.mjs --case C01      运行单个用例（C01..C04）
//   node tools/local_verify.mjs                 运行全部用例
//
// 每个用例输出一行 JSON 记录：case、断言数、passed/failed/skipped、
// 样本标识与逐条期望/观察摘要。任一断言失败、断言数为零或整组跳过，
// 进程以非零码退出。

import { readFile } from 'node:fs/promises';
import { analyze } from '../src/index.mjs';

const O = 'https://app.example';
const S = 'winA';
const O1 = 'https://app.example';
const S1 = 'winA';

// ---------------------------------------------------------------------------
// 断言工具
// ---------------------------------------------------------------------------

function canon(v) {
  if (Array.isArray(v)) return `[${v.map(canon).join(',')}]`;
  if (v && typeof v === 'object') {
    return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(',')}}`;
  }
  return JSON.stringify(v);
}

function makeChecker() {
  const assertions = [];
  return {
    assertions,
    check(name, expected, observed) {
      assertions.push({ name, expected, observed, pass: canon(expected) === canon(observed) });
    },
  };
}

async function loadSample(name) {
  const url = new URL(`../samples/${name}`, import.meta.url);
  return JSON.parse(await readFile(url, 'utf8'));
}

function oneCall(checker, result, label) {
  checker.check(`${label}: exactly one call`, 1, result.calls.length);
  return result.calls[0];
}

// ---------------------------------------------------------------------------
// 合成 CFG 构造器（确定性，无随机源；样本名即种子）
// ---------------------------------------------------------------------------

function buildC01({ doOrigin = true, doSource = true } = {}) {
  const nodes = {
    r0: { kind: 'read', target: 'o', object: 'm', field: 'origin', next: doOrigin ? 'cO' : 'r1' },
    r1: { kind: 'read', target: 's', object: 'm', field: 'source', next: doSource ? 'cS' : 'r2' },
    r2: { kind: 'read', target: 'd', object: 'm', field: 'data', next: 'call' },
    call: { kind: 'call', api: 'extension.openTab', args: { payload: 'd' }, require: { origin: O, source: S }, next: 'end' },
    end: { kind: 'nop', next: null },
  };
  if (doOrigin) nodes.cO = { kind: 'checkOrigin', value: 'o', expected: O, pass: 'r1', fail: 'end' };
  if (doSource) nodes.cS = { kind: 'checkSource', value: 's', expected: S, pass: 'r2', fail: 'end' };
  return { messages: ['m'], entry: 'r0', nodes };
}

function buildC02Override() {
  return {
    messages: ['m1', 'm2'],
    entry: 'rO',
    nodes: {
      rO: { kind: 'read', target: 'o', object: 'm1', field: 'origin', next: 'cO' },
      cO: { kind: 'checkOrigin', value: 'o', expected: O1, pass: 'rS', fail: 'end' },
      rS: { kind: 'read', target: 's', object: 'm1', field: 'source', next: 'cS' },
      cS: { kind: 'checkSource', value: 's', expected: S1, pass: 'rD2', fail: 'end' },
      rD2: { kind: 'read', target: 'd2', object: 'm2', field: 'data', next: 'mg' },
      mg: { kind: 'merge', target: 'obj', base: 'm1', overrides: { data: 'd2' }, next: 'rD' },
      rD: { kind: 'read', target: 'd', object: 'obj', field: 'data', next: 'rM' },
      rM: { kind: 'read', target: 'mo', object: 'obj', field: 'origin', next: 'call' },
      call: { kind: 'call', api: 'extension.openTab', args: { payload: 'd', meta: 'mo' }, require: { origin: O1, source: S1 }, next: 'end' },
      end: { kind: 'nop', next: null },
    },
  };
}

function buildC03Unrelated() {
  return {
    messages: ['m'],
    entry: 'rO',
    nodes: {
      rO: { kind: 'read', target: 'o', object: 'm', field: 'origin', next: 'cO' },
      cO: { kind: 'checkOrigin', value: 'o', expected: O, pass: 'rS', fail: 'end' },
      rS: { kind: 'read', target: 's', object: 'm', field: 'source', next: 'cS' },
      cS: { kind: 'checkSource', value: 's', expected: S, pass: 'rc', fail: 'end' },
      rc: { kind: 'const', target: 'c', value: 'label', next: 'mg' },
      mg: { kind: 'merge', target: 'obj', base: 'm', overrides: { note: 'c' }, next: 'rD' },
      rD: { kind: 'read', target: 'd', object: 'obj', field: 'data', next: 'call' },
      call: { kind: 'call', api: 'extension.openTab', args: { payload: 'd' }, require: { origin: O, source: S }, next: 'end' },
      end: { kind: 'nop', next: null },
    },
  };
}

function buildC03Branch() {
  return {
    messages: ['m'],
    entry: 'r0',
    nodes: {
      r0: { kind: 'read', target: 'f', object: 'm', field: 'flag', next: 'br' },
      br: { kind: 'branch', cond: 'f', true: 'rO', false: 'rD' },
      rO: { kind: 'read', target: 'o', object: 'm', field: 'origin', next: 'cO' },
      cO: { kind: 'checkOrigin', value: 'o', expected: O, pass: 'rS', fail: 'end' },
      rS: { kind: 'read', target: 's', object: 'm', field: 'source', next: 'cS' },
      cS: { kind: 'checkSource', value: 's', expected: S, pass: 'rD', fail: 'end' },
      rD: { kind: 'read', target: 'd', object: 'm', field: 'data', next: 'call' },
      call: { kind: 'call', api: 'extension.openTab', args: { payload: 'd' }, require: { origin: O, source: S }, next: 'end' },
      end: { kind: 'nop', next: null },
    },
  };
}

function buildC03LoopUnchecked() {
  return {
    messages: ['m'],
    entry: 'r0',
    nodes: {
      r0: { kind: 'read', target: 'f', object: 'm', field: 'flag', next: 'head' },
      head: { kind: 'branch', cond: 'f', true: 'rO', false: 'rD' },
      rO: { kind: 'read', target: 'o', object: 'm', field: 'origin', next: 'cO' },
      cO: { kind: 'checkOrigin', value: 'o', expected: O, pass: 'rS', fail: 'end' },
      rS: { kind: 'read', target: 's', object: 'm', field: 'source', next: 'cS' },
      cS: { kind: 'checkSource', value: 's', expected: S, pass: 'head', fail: 'end' },
      rD: { kind: 'read', target: 'd', object: 'm', field: 'data', next: 'call' },
      call: { kind: 'call', api: 'extension.openTab', args: { payload: 'd' }, require: { origin: O, source: S }, next: 'end' },
      end: { kind: 'nop', next: null },
    },
  };
}

// ---------------------------------------------------------------------------
// 用例
// ---------------------------------------------------------------------------

async function runC01() {
  const r = makeChecker();
  const sample = 'samples/c01-protected.json + synthetic:c01-{origin-only,source-only}';

  // 完整满足：同一消息的 origin 与 source 都检查通过。
  const full = analyze(await loadSample('c01-protected.json'));
  r.check('full: converged', true, full.stats.converged);
  const cFull = oneCall(r, full, 'full');
  r.check('full: verdict', 'protected', cFull.verdict);
  r.check('full: usedMessages', ['m'], cFull.usedMessages);
  r.check('full: missing', [], cFull.missing);

  // 只满足 origin：缺少 source 检查，必须判定失败并给出路径与参数。
  const noSrc = analyze(buildC01({ doSource: false }));
  const cNoSrc = oneCall(r, noSrc, 'origin-only');
  r.check('origin-only: verdict', 'violated', cNoSrc.verdict);
  r.check('origin-only: missing', [{ message: 'm', condition: 'source', expected: S }], cNoSrc.missing);
  r.check('origin-only: witness affectedParams', ['payload'], cNoSrc.witness.affectedParams);
  r.check('origin-only: witness ends at call', 'call', cNoSrc.witness.path[cNoSrc.witness.path.length - 1].node);

  // 只满足 source：缺少 origin 检查。
  const noOrigin = analyze(buildC01({ doOrigin: false }));
  const cNoOrigin = oneCall(r, noOrigin, 'source-only');
  r.check('source-only: verdict', 'violated', cNoOrigin.verdict);
  r.check('source-only: missing', [{ message: 'm', condition: 'origin', expected: O }], cNoOrigin.missing);
  r.check('source-only: witness paramSources', { payload: ['msg:m.data'] }, cNoOrigin.witness.paramSources);

  return { sample, assertions: r.assertions };
}

async function runC02() {
  const r = makeChecker();
  const sample = 'samples/c02-wrong-message.json + synthetic:c02-merge-override';

  // 反例一：检查的是另一条消息 m2，调用用的是 m1 —— 不算保护。
  const wrong = analyze(await loadSample('c02-wrong-message.json'));
  r.check('wrong-message: converged', true, wrong.stats.converged);
  const cA = oneCall(r, wrong, 'wrong-message');
  r.check('wrong-message: verdict', 'violated', cA.verdict);
  r.check('wrong-message: usedMessages', ['m1'], cA.usedMessages);
  r.check('wrong-message: missing', [
    { message: 'm1', condition: 'origin', expected: O1 },
    { message: 'm1', condition: 'source', expected: S1 },
  ], cA.missing);
  r.check('wrong-message: paramSources', { payload: ['msg:m1.data'] }, cA.paramSources);
  r.check('wrong-message: witness traverses m2 check', true,
    cA.witness.path.some((p) => p.node === 'cO2'));
  r.check('wrong-message: witness affectedParams', ['payload'], cA.witness.affectedParams);

  // 反例二：已检查的 m1 对象被合并覆盖了敏感字段 data（来自未检查的 m2），
  // 被覆盖字段的来源必须重新追踪到 m2；未覆盖字段仍追踪到 m1。
  const over = analyze(buildC02Override());
  const cB = oneCall(r, over, 'merge-override');
  r.check('merge-override: verdict', 'violated', cB.verdict);
  r.check('merge-override: usedMessages', ['m1', 'm2'], cB.usedMessages);
  r.check('merge-override: missing', [
    { message: 'm2', condition: 'origin', expected: O1 },
    { message: 'm2', condition: 'source', expected: S1 },
  ], cB.missing);
  r.check('merge-override: overridden param re-tracked', ['msg:m2.data'], cB.paramSources.payload);
  r.check('merge-override: untouched field still m1', ['msg:m1.origin'], cB.paramSources.meta);
  r.check('merge-override: affectedParams', ['payload'], cB.affectedParams);
  r.check('merge-override: witness path goes through merge', true,
    cB.witness.path.some((p) => p.node === 'mg'));

  return { sample, assertions: r.assertions };
}

async function runC03() {
  const r = makeChecker();
  const sample = 'samples/c03-loop-checked.json + synthetic:c03-{unrelated,branch-join,loop-unchecked}';

  // 无关字段变化（merge 覆盖 note，调用用 data）不撤销保护。
  const unrel = analyze(buildC03Unrelated());
  r.check('unrelated: converged', true, unrel.stats.converged);
  const cU = oneCall(r, unrel, 'unrelated');
  r.check('unrelated: verdict', 'protected', cU.verdict);
  r.check('unrelated: paramSources', { payload: ['msg:m.data'] }, cU.paramSources);

  // 分支合流：检查只在 true 分支，合流后不得错误授信。
  const br = analyze(buildC03Branch());
  r.check('branch-join: converged', true, br.stats.converged);
  const cB = oneCall(r, br, 'branch-join');
  r.check('branch-join: verdict', 'violated', cB.verdict);
  r.check('branch-join: missing', [
    { message: 'm', condition: 'origin', expected: O },
    { message: 'm', condition: 'source', expected: S },
  ], cB.missing);
  const rDStep = cB.witness.path.find((p) => p.node === 'rD');
  r.check('branch-join: witness skips checks via false edge', 'false', rDStep && rDStep.edgeIn);

  // 循环：检查在循环之前完成，循环体内的 merge/read 收敛且不撤销保护。
  const loop = analyze(await loadSample('c03-loop-checked.json'));
  r.check('loop-checked: converged', true, loop.stats.converged);
  const cL = oneCall(r, loop, 'loop-checked');
  r.check('loop-checked: verdict', 'protected', cL.verdict);
  r.check('loop-checked: usedMessages', ['m'], cL.usedMessages);

  // 循环二：检查只在循环体内（可能一次都不执行），不得错误授信。
  const loopBad = analyze(buildC03LoopUnchecked());
  r.check('loop-unchecked: converged', true, loopBad.stats.converged);
  const cLB = oneCall(r, loopBad, 'loop-unchecked');
  r.check('loop-unchecked: verdict', 'violated', cLB.verdict);
  r.check('loop-unchecked: witness bypasses loop body', 'false',
    (cLB.witness.path.find((p) => p.node === 'rD') || {}).edgeIn);

  return { sample, assertions: r.assertions };
}

async function runC04() {
  const r = makeChecker();
  const sample = 'samples/c04-unknown-result.json';

  const res = analyze(await loadSample('c04-unknown-result.json'));
  r.check('converged', true, res.stats.converged);
  r.check('call count', 4, res.calls.length);
  r.check('call order', ['c1u', 'c2p', 'c3v', 'c4x'], res.calls.map((c) => c.nodeId));
  const [c1u, c2p, c3v, c4x] = res.calls;

  // c1u：参数完全来自无法解析的调用结果 —— 全路径结论为未知，证据可复核。
  r.check('c1u: verdict', 'unknown', c1u.verdict);
  r.check('c1u: unknownParams', ['payload'], c1u.unknownParams);
  r.check('c1u: paramSources', { payload: ['unknown'] }, c1u.paramSources);
  r.check('c1u: missing', [], c1u.missing);

  // c2p：同一消息完整检查 —— 全路径受保护。
  r.check('c2p: verdict', 'protected', c2p.verdict);
  r.check('c2p: usedMessages', ['m'], c2p.usedMessages);

  // c3v：另一条消息 m2 从未检查 —— 失败并给出路径与受影响参数。
  r.check('c3v: verdict', 'violated', c3v.verdict);
  r.check('c3v: missing', [
    { message: 'm2', condition: 'origin', expected: 'https://other.example' },
    { message: 'm2', condition: 'source', expected: 'winB' },
  ], c3v.missing);
  r.check('c3v: witness affectedParams', ['payload'], c3v.witness.affectedParams);
  r.check('c3v: witness ends at call', 'c3v', c3v.witness.path[c3v.witness.path.length - 1].node);

  // c4x：参数在一条路径上来自已检查消息、另一条路径上来自未知调用结果，
  // 全路径结论必须为未知而非受保护。
  r.check('c4x: verdict', 'unknown', c4x.verdict);
  r.check('c4x: paramSources', { payload: ['msg:m.data', 'unknown'] }, c4x.paramSources);
  r.check('c4x: unknownParams', ['payload'], c4x.unknownParams);

  return { sample, assertions: r.assertions };
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

const CASES = {
  C01: { description: '同一消息的 origin/source 检查：完整满足与只满足其一', run: runC01 },
  C02: { description: '反例：检查另一条消息不算保护；合并覆盖后重新追踪字段来源', run: runC02 },
  C03: { description: '无关字段变化不撤销保护；分支合流与循环收敛且不错误授信', run: runC03 },
  C04: { description: '未知调用结果：逐敏感调用的全路径结论与可复核证据', run: runC04 },
};

async function runCase(id) {
  const c = CASES[id];
  try {
    const { sample, assertions } = await c.run();
    const passed = assertions.filter((a) => a.pass).length;
    return {
      case: id,
      description: c.description,
      sample,
      assertions: assertions.length,
      passed,
      failed: assertions.length - passed,
      skipped: 0,
      results: assertions,
    };
  } catch (err) {
    return {
      case: id,
      description: c.description,
      sample: null,
      assertions: 0,
      passed: 0,
      failed: 1,
      skipped: 0,
      error: String((err && err.message) || err),
      results: [],
    };
  }
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes('--list')) {
    console.log(JSON.stringify(
      Object.entries(CASES).map(([id, c]) => ({ case: id, description: c.description })),
      null,
      2,
    ));
    return 0;
  }
  let selected;
  const ci = args.indexOf('--case');
  if (ci >= 0) {
    const id = args[ci + 1];
    if (!CASES[id]) {
      console.error(JSON.stringify({ error: `unknown case: ${String(id)}`, known: Object.keys(CASES) }));
      return 2;
    }
    selected = [id];
  } else if (args.length === 0) {
    selected = Object.keys(CASES);
  } else {
    console.error(JSON.stringify({ error: 'usage: local_verify.mjs [--list] [--case C01|C02|C03|C04]' }));
    return 2;
  }

  let bad = 0;
  for (const id of selected) {
    const record = await runCase(id);
    console.log(JSON.stringify(record));
    if (record.failed > 0 || record.assertions === 0 || record.skipped >= record.assertions) bad += 1;
  }
  return bad > 0 ? 1 : 0;
}

process.exitCode = await main();
