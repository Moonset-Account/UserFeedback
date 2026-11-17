#!/usr/bin/env node
// 本地核验入口：直接调用本次实现并执行真实断言。
// 用法：
//   node tools/local_verify.mjs --list
//   node tools/local_verify.mjs --case C01 [--case C02 ...]
// 任一断言失败、入口缺失、未实现、零断言或整组跳过均以非零码退出。

import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

import { reviewDependencies, normalizeAndSkeleton, loadConfusableMap } from '../src/index.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_DIR = path.join(HERE, '..', 'test', 'fixtures');

const CASES = {
  C01: 'C01-priority-resolution.json',
  C02: 'C02-identity-scope-ecosystem.json',
  C03: 'C03-confusable-skeleton.json',
  C04: 'C04-duplicates-conflicts-ordering.json',
};

async function loadFixture(file) {
  const raw = await readFile(path.join(FIXTURE_DIR, file), 'utf8');
  return JSON.parse(raw);
}

function buildInput(fx, scenario) {
  return {
    directory: fx.directory,
    declarations: scenario.declarations,
    normalization: fx.normalization,
    registryPriority: scenario.registryPriority,
    registries: fx.registries,
    confusables: fx.confusables,
  };
}

function byId(findings, declarationId) {
  return findings.filter((f) => f.declarationId === declarationId);
}

// ---- 真实断言集合 -----------------------------------------------------------

async function assertC01() {
  const fx = await loadFixture(CASES.C01);
  const checks = [];
  const run = (sid) => {
    const scenario = fx.scenarios.find((s) => s.id === sid);
    return { scenario, result: reviewDependencies(buildInput(fx, scenario)) };
  };

  // 场景 A：内部注册表优先
  const a = run('internal-first');
  {
    const f = byId(a.result.findings, 'd1');
    assert.equal(f.length, 1, 'C01 internal-first: d1 应有一条 shadow hint');
    assert.equal(f[0].severity, 'hint');
    assert.equal(f[0].kind, 'SHADOWED_EXTERNAL_LOOKALIKE');
    assert.equal(f[0].resolutionPath.selectedRegistry, 'npm-internal');
    assert.equal(f[0].resolutionPath.selectedExternal, false);
    assert.ok(
      f[0].shadowedExternal.some((s) => s.registryId === 'npm-public' && s.matchType === 'exact-display-name'),
      'C01: hint 必须记录外部影子包及其注册表身份',
    );
    checks.push('d1 internal-first -> hint + shadow path recorded');
  }
  {
    const f = byId(a.result.findings, 'd2');
    assert.equal(f.length, 1, 'C01: payrollsdk 与内部 payroll-sdk 仅规范名相似，保留提示');
    assert.equal(f[0].severity, 'hint');
    assert.equal(f[0].resolutionPath.selectedRegistry, 'npm-internal');
    assert.equal(f[0].kind, 'SHADOWED_EXTERNAL_LOOKALIKE');
    assert.equal(f[0].internalCandidate.matchType, 'same-normalized-name');
    assert.ok(
      f[0].shadowedExternal.some((s) => s.registryId === 'npm-public'),
      'C01: 提示必须保留外部同名影子包的注册表身份',
    );
    checks.push('d2 similar normalized name stays hint; internal registry wins but shadow recorded');
  }
  {
    const f = byId(a.result.findings, 'd3');
    assert.equal(f.length, 0, 'C01: data-pipeline 无内部候选，属正常外部依赖，不产生风险项');
    checks.push('d3 unrelated external dependency yields no finding');
  }

  // 场景 B：公共注册表优先 -> 真正可错误解析 -> block
  const b = run('public-first');
  {
    const f = byId(b.result.findings, 'd1');
    assert.equal(f.length, 1);
    assert.equal(f[0].severity, 'block', 'C01: 外部同名被优先选中必须阻断');
    assert.equal(f[0].kind, 'EXTERNAL_SAME_NAME');
    assert.equal(f[0].resolutionPath.selectedRegistry, 'npm-public');
    assert.equal(f[0].resolutionPath.selectedExternal, true);
    assert.equal(f[0].internalCandidate.name, 'payroll-sdk');
    const first = f[0].resolutionPath.checkedRegistries[0];
    assert.equal(first.registryId, 'npm-public');
    assert.equal(first.external, true);
    assert.equal(first.found, true);
    assert.equal(first.matchedBy, 'exact-display-name', 'C01: 解析路径必须给出具体命中方式');
    assert.equal(first.matchedPackage, 'payroll-sdk');
    checks.push('d1 public-first -> block with full resolution path');
  }
  {
    const f = byId(b.result.findings, 'd2');
    assert.equal(f.length, 1, 'C01: 公共优先也不能把纯规范名相似升级为阻断');
    assert.equal(f[0].severity, 'hint');
    assert.equal(f[0].kind, 'SIMILAR_NORMALIZED_NAME');
    assert.equal(f[0].resolutionPath.selectedRegistry, 'npm-public');
    checks.push('d2 stays hint under public-first (only exact name/confusable blocks)');
  }
  {
    const f = byId(b.result.findings, 'd3');
    assert.equal(f.length, 0, 'C01: 即使公共优先，无内部同名/混淆候选也不阻断');
    checks.push('d3 public-first still no finding without internal candidate');
  }
  assert.equal(b.result.summary.blocks, 1);
  assert.equal(a.result.summary.blocks, 0, 'C01: 仅优先级不同，结论必须从 hint 变为 block');
  checks.push('priority order flips hint to block');

  return { checks, samples: ['internal-first', 'public-first'] };
}

async function assertC02() {
  const fx = await loadFixture(CASES.C02);
  const checks = [];
  const run = (sid) => {
    const scenario = fx.scenarios.find((s) => s.id === sid);
    return reviewDependencies(buildInput(fx, scenario));
  };

  const a = run('scope-is-identity');
  {
    const f = byId(a.findings, 's1');
    assert.equal(f.length, 0, 'C02: @vendor/logger 与 @acme/logger 显示名相同但作用域不同，不得合并');
    assert.equal(f[0], undefined);
    checks.push('@vendor/logger not merged with @acme/logger by display name');
  }
  {
    const f = byId(a.findings, 's2');
    assert.equal(f.length, 0, 'C02: 无作用域 Logger 与 @acme/logger 是不同身份');
    checks.push('unscoped Logger kept distinct from scoped packages');
  }
  // 直接核对规范化输出保留了作用域身份
  const map = loadConfusableMap(fx.confusables);
  const npmRule = fx.normalization.npm;
  const scoped = normalizeAndSkeleton('@acme/logger', npmRule, map);
  const other = normalizeAndSkeleton('@vendor/logger', npmRule, map);
  const unscoped = normalizeAndSkeleton('Logger', npmRule, map);
  assert.notEqual(scoped.normalized, other.normalized);
  assert.notEqual(scoped.normalized, unscoped.normalized);
  assert.equal(scoped.normalized, '@acme/logger');
  assert.equal(unscoped.normalized, 'logger');
  checks.push('normalization preserves scope as part of identity');

  const b = run('ecosystem-and-registry-identity');
  {
    const f = byId(b.findings, 's3');
    assert.equal(f.length, 1, 'C02: pypi public 优先且内部有同名 -> block');
    assert.equal(f[0].severity, 'block');
    assert.equal(f[0].ecosystem, 'pypi');
    assert.equal(f[0].resolutionPath.selectedRegistry, 'pypi-public');
    checks.push('pypi logger blocked independently of npm identity');
  }
  {
    const f = byId(b.findings, 's4');
    assert.equal(f.length, 1, 'C02: npm @acme/logger 被公共注册表同名抢先 -> block');
    assert.equal(f[0].severity, 'block');
    assert.equal(f[0].resolutionPath.selectedRegistry, 'npm-public');
    assert.equal(f[0].internalCandidate.name, '@acme/logger');
    checks.push('npm scoped block carries registry identity, no cross-ecosystem merge');
  }
  // 两个 block 必须携带不同生态与注册表，证明没有按显示名合并
  const blocks = b.findings.filter((x) => x.severity === 'block');
  const ecosystems = new Set(blocks.map((x) => x.ecosystem));
  const registries = new Set(blocks.map((x) => x.resolutionPath.selectedRegistry));
  assert.equal(ecosystems.size, 2);
  assert.equal(registries.size, 2);
  checks.push('blocks keyed by ecosystem + registry, not display name');

  return { checks, samples: ['scope-is-identity', 'ecosystem-and-registry-identity'] };
}

async function assertC03() {
  const fx = await loadFixture(CASES.C03);
  const checks = [];
  const map = loadConfusableMap(fx.confusables);
  const npmRule = fx.normalization.npm;

  // 先直接核对骨架与字符位置（不经过完整评审，确保映射/位置逻辑本身被断言）
  const declSkel = normalizeAndSkeleton('payrоll-sdk', npmRule, map);
  const internalSkel = normalizeAndSkeleton('payroll-sdk', npmRule, map);
  assert.equal(declSkel.skeleton, internalSkel.skeleton, 'C03: 西里尔 о 折叠后骨架必须相同');
  assert.equal(declSkel.normalized, 'payrоllsdk');
  assert.equal(internalSkel.normalized, 'payrollsdk');
  const confSpans = declSkel.spans.filter((s) => s.kind === 'confusable');
  assert.equal(confSpans.length, 1);
  assert.equal(confSpans[0].originalIndex, fx.expectedPositions['payrоll-sdk']);
  assert.equal(confSpans[0].mappedTo, 'o');
  checks.push('skeleton + raw code-point position asserted for payrоll-sdk');

  const cSkel = normalizeAndSkeleton('сache-client', npmRule, map);
  const cSpans = cSkel.spans.filter((s) => s.kind === 'confusable');
  assert.equal(cSpans[0].originalIndex, fx.expectedPositions['сache-client']);
  assert.equal(cSpans[0].mappedTo, 'c');
  checks.push('skeleton + position asserted for сache-client');

  // 完整评审：public-first -> block，且每条风险附解析路径与混淆位置
  const result = reviewDependencies(buildInput(fx, {
    registryPriority: fx.registryPriority,
    declarations: fx.declarations,
  }));
  for (const id of ['cyr-block', 'cyr-block-2']) {
    const f = byId(result.findings, id);
    assert.equal(f.length, 1);
    assert.equal(f[0].severity, 'block');
    assert.equal(f[0].kind, 'EXTERNAL_CONFUSABLE');
    assert.equal(f[0].resolutionPath.selectedRegistry, 'npm-public');
    assert.equal(f[0].resolutionPath.registryChain[0], 'npm-public');
    assert.ok(f[0].confusablePositions.length >= 1, 'C03: block 必须附混淆字符位置');
    const pos = f[0].confusablePositions[0];
    assert.equal(typeof pos.absoluteIndex, 'number');
    assert.match(pos.declarationCodePoint, /^U\+[0-9A-F]{4,}$/);
    assert.notEqual(pos.declarationCodePoint, pos.internalCodePoint, 'C03: 位置上的码点必须确实不同');
    checks.push(`${id} block carries resolution path and confusable position`);
  }
  assert.equal(result.summary.blocks, 2);

  // 同样的声明改为内部优先：外部包只能作为影子包，结论降级为 hint
  const hint = reviewDependencies(buildInput(fx, {
    registryPriority: fx.hintScenario.registryPriority,
    declarations: fx.hintScenario.declarations,
  }));
  const hf = byId(hint.findings, 'cyr-hint');
  assert.equal(hf.length, 1);
  assert.equal(hf[0].severity, 'hint');
  assert.equal(hf[0].resolutionPath.selectedRegistry, 'npm-internal');
  assert.ok(
    hf[0].shadowedExternal.some((s) => s.registryId === 'npm-public'
      && ['exact-display-name', 'confusable-skeleton'].includes(s.matchType)),
    'C03: hint 也必须保留外部混淆影子包的具体解析路径与匹配类型',
  );
  // 提示仍需指出声明与内部 ASCII 包之间的混淆字符位置（骨架相同、码点不同）。
  assert.ok(hf[0].confusablePositions.length >= 1, 'C03: hint 同样附混淆字符位置');
  assert.equal(hf[0].confusablePositions[0].absoluteIndex, fx.expectedPositions['payrоll-sdk']);
  assert.notEqual(
    hf[0].confusablePositions[0].declarationCodePoint,
    hf[0].confusablePositions[0].internalCodePoint,
  );
  assert.equal(hint.summary.blocks, 0);
  checks.push('internal-first downgrades confusable block to hint with shadow path + positions');

  return { checks, samples: ['public-first', 'internal-first'], seeds: ['payrоll-sdk', 'сache-client'] };
}

async function assertC04() {
  const fx = await loadFixture(CASES.C04);
  const checks = [];
  const run = (sid) => {
    const scenario = fx.scenarios.find((s) => s.id === sid);
    return { scenario, result: reviewDependencies(buildInput(fx, scenario)) };
  };

  // 重复目录项必须被单独诊断，且两个条目都保留参与匹配
  const a = run('npm-conflict-public-first');
  const dup = a.result.diagnostics.filter((d) => d.code === 'DUPLICATE_DIRECTORY_ENTRY');
  assert.equal(dup.length, 1, 'C04: 重复目录项应恰好产生一条诊断');
  assert.equal(dup[0].name, '@acme/metrics-lib');
  assert.equal(dup[0].firstPath, 'packages/npm/metrics-lib/a');
  assert.equal(dup[0].duplicatePath, 'packages/npm/metrics-lib/b');
  checks.push('duplicate directory entry diagnosed independently and retained');

  // 冲突规范化规则诊断
  const conflicts = a.result.diagnostics.filter((d) => d.code === 'CONFLICTING_NORMALIZATION_RULES');
  assert.ok(conflicts.some((c) => c.ecosystem === 'npm' && c.ruleSetIds.includes('npm-drop-scope-first')));
  checks.push('conflicting normalization rule sets flagged per ecosystem');

  // drop-scope 下：@acme/metrics-lib 规范化为 metricslib，与公共 metrics-lib 同骨架同名 -> block
  // keep-scope 下：保留 @acme/ 前缀，无法匹配无作用域公共包 -> 无 block
  const m1 = byId(a.result.findings, 'm1');
  const dropFinding = m1.find((f) => f.ruleSetId === 'npm-drop-scope-first');
  const keepFinding = m1.find((f) => f.ruleSetId === 'npm-keep-scope-first');
  assert.ok(dropFinding, 'C04: drop-scope 规则集下必须产生 finding');
  assert.equal(dropFinding.severity, 'block');
  assert.equal(dropFinding.ruleSensitive, true, 'C04: 结论随规则集变化必须标记 ruleSensitive');
  assert.equal(keepFinding, undefined, 'C04: keep-scope 规则集下不应匹配无作用域公共包');
  const comp = Object.fromEntries(dropFinding.ruleSetComparison.map((p) => [p.ruleSetId, p.severity]));
  assert.equal(comp['npm-drop-scope-first'], 'block');
  assert.equal(comp['npm-keep-scope-first'], 'none');
  checks.push('rule order/scope choice changes block conclusion (ruleSensitive=true)');

  // 内部优先：同一声明两个规则集都不应阻断，drop-scope 下仅保留影子提示
  const b = run('npm-conflict-internal-first');
  const m2 = byId(b.result.findings, 'm2');
  assert.ok(m2.every((f) => f.severity !== 'block'), 'C04: 内部优先时不得阻断');
  const dropHint = m2.find((f) => f.ruleSetId === 'npm-drop-scope-first');
  assert.equal(dropHint.severity, 'hint');
  assert.ok(dropHint.shadowedExternal.some((s) => s.registryId === 'npm-public'));
  checks.push('internal-first: drop-scope yields hint, keep-scope yields nothing');

  // pypi 两套规则仅步骤次序不同，结论必须一致（对纯 ASCII 小写名次序不变）
  const c = run('pypi-order-invariant');
  const m3 = byId(c.result.findings, 'm3');
  assert.equal(m3.length, 2, 'C04: 两套 pypi 规则集各产出一条结论');
  assert.ok(m3.every((f) => f.severity === 'block'));
  assert.ok(m3.every((f) => f.ruleSensitive === false), 'C04: 次序不改变结论时 ruleSensitive=false');
  assert.deepEqual(
    [...new Set(m3.map((f) => f.kind))],
    ['EXTERNAL_SAME_NAME'],
  );
  checks.push('pypi step ordering invariant for this sample (both block, not sensitive)');

  // 单套规则内部 scope keep/drop 自相矛盾 -> 专门诊断，执行以首个 scope 步骤为准
  const d = run('maven-contradiction');
  const contra = d.result.diagnostics.filter((x) => x.code === 'CONTRADICTORY_SCOPE_STEPS');
  assert.equal(contra.length, 1);
  assert.deepEqual(contra[0].modes.sort(), ['drop', 'keep']);
  assert.equal(byId(d.result.findings, 'm4').length, 0, 'C04: 内部包正常解析且无外部同名，不应有风险项');
  checks.push('contradictory scope steps diagnosed; first scope step applied');

  // 诊断总数至少覆盖：1 重复 + 2 生态冲突（npm/pypi）+ 1 矛盾步骤
  assert.ok(a.result.diagnostics.length >= 2);
  assert.ok(c.result.diagnostics.some((x) => x.code === 'CONFLICTING_NORMALIZATION_RULES' && x.ecosystem === 'pypi'));
  checks.push('diagnostics distinguish duplicates, rule-set conflicts and in-rule contradictions');

  return {
    checks,
    samples: ['npm-conflict-public-first', 'npm-conflict-internal-first', 'pypi-order-invariant', 'maven-contradiction'],
  };
}

const RUNNERS = { C01: assertC01, C02: assertC02, C03: assertC03, C04: assertC04 };

function parseArgs(argv) {
  const selected = [];
  const unknown = [];
  let listOnly = false;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--list') {
      listOnly = true;
    } else if (arg.startsWith('--case=')) {
      const v = arg.slice('--case='.length);
      (CASES[v] ? selected : unknown).push(v);
    } else if (arg === '--case') {
      const v = argv[i + 1];
      if (v === undefined) unknown.push('(missing value)');
      else (CASES[v] ? selected : unknown).push(v);
      i += 1;
    } else if (CASES[arg]) {
      selected.push(arg);
    } else {
      unknown.push(arg);
    }
  }
  return { listOnly, selected: [...new Set(selected)], unknown: [...new Set(unknown)] };
}

async function main() {
  const { listOnly, selected, unknown } = parseArgs(process.argv.slice(2));

  if (listOnly) {
    process.stdout.write(`${JSON.stringify({ cases: Object.keys(CASES) }, null, 2)}\n`);
    return unknown.length ? 1 : 0;
  }
  const records = [];
  let failed = 0;

  for (const bad of unknown) {
    records.push({
      case: bad,
      assertions: 0,
      passed: 0,
      failed: 1,
      skipped: 0,
      samples: [],
      seeds: [],
      expected: `one of ${Object.keys(CASES).join(', ')}`,
      observed: null,
      error: `UNKNOWN_CASE_OR_FLAG: ${bad}`,
    });
    failed += 1;
  }

  const targets = selected.length ? selected : Object.keys(CASES);

  for (const id of targets) {
    const runner = RUNNERS[id];
    const record = {
      case: id,
      assertions: 0,
      passed: 0,
      failed: 0,
      skipped: 0,
      samples: [],
      seeds: [],
      expected: null,
      observed: null,
      error: null,
    };
    if (!runner) {
      record.error = `UNKNOWN_CASE: ${id}`;
      record.failed = 1;
      records.push(record);
      failed += 1;
      continue;
    }
    try {
      const out = await runner();
      const n = out.checks.length;
      if (n === 0) {
        // 零断言视为失败：包装器不能只打印成功文字。
        record.error = 'ZERO_ASSERTIONS';
        record.failed = 1;
      } else {
        record.assertions = n;
        record.passed = n;
        record.samples = out.samples || [];
        record.seeds = out.seeds || [];
        record.expected = out.expected || 'see fixture + inline assertions';
        record.observed = out.observed || `${n} assertions evaluated against reviewDependencies output`;
      }
    } catch (err) {
      record.failed = 1;
      record.assertions = record.assertions || 0;
      record.error = `${err.code || 'ASSERTION_FAILED'}: ${err.message}`;
      record.observed = err.stack ? err.stack.split('\n').slice(0, 3).join(' | ') : String(err);
    }
    if (record.failed > 0) failed += 1;
    records.push(record);
  }

  process.stdout.write(`${JSON.stringify(records, null, 2)}\n`);
  if (records.length === 0 || records.every((r) => r.skipped > 0 && r.passed === 0)) {
    process.stderr.write('整组跳过或无有效用例，按失败处理\n');
    return 2;
  }
  return failed > 0 ? 1 : 0;
}

main().then((code) => process.exit(code)).catch((err) => {
  process.stderr.write(`VERIFIER_CRASH: ${err.stack || err}\n`);
  process.exit(3);
});
