// 覆盖判定与快照对比的单元测试。
// 每个用例标注稳定 case_id，断言内容、输入、实际输出与期望值。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  STATUS,
  loadSnapshot,
  normalizeSnapshot,
  analyzeSnapshot,
  diffSnapshots,
  evaluateSink,
  resolveEntryModules,
} from '../lib/analyzer.mjs';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const dirA = path.join(root, 'fixtures', 'snapshot-a');
const dirB = path.join(root, 'fixtures', 'snapshot-b');

async function analyzeBoth() {
  const [snapA, snapB] = await Promise.all([loadSnapshot(dirA), loadSnapshot(dirB)]);
  const anaA = analyzeSnapshot(snapA);
  const anaB = analyzeSnapshot(snapB);
  const diff = diffSnapshots(snapA, snapB, anaA, anaB);
  return { snapA, snapB, anaA, anaB, diff };
}

function sinkStatus(analysis, entryName, sinkId) {
  return analysis.entries[entryName].sinks.find((s) => s.sinkId === sinkId)?.status;
}

test('TT-001 CSP 仅报告模式: checkout 在快照 A 中全部写入点判为 report_only 并给出修复点', async () => {
  const { anaA } = await analyzeBoth();
  for (const id of ['s1', 's2', 's5', 's6']) {
    assert.equal(sinkStatus(anaA, 'checkout', id), STATUS.REPORT_ONLY, `sink ${id}`);
  }
  const finding = anaA.findings.find((f) => f.id === 'report_only:checkout:ui-core:s1');
  assert.ok(finding, '应存在 report_only finding');
  assert.equal(finding.severity, 'warning');
  assert.deepEqual(finding.entries, ['checkout']);
  assert.ok(finding.loadPaths[0].startsWith('/checkout'), `加载路径: ${finding.loadPaths[0]}`);
  assert.match(finding.fixPoint, /report-only.*enforce/s, `修复点: ${finding.fixPoint}`);
});

test('TT-002 策略名称未获授权: home-lazy 使用未列入 trusted-types 指令的 home-inline', async () => {
  const { anaB } = await analyzeBoth();
  assert.equal(sinkStatus(anaB, 'home', 's3'), STATUS.POLICY_NOT_AUTHORIZED);
  const finding = anaB.findings.find((f) => f.id === 'policy_not_authorized:home:home-lazy:s3');
  assert.ok(finding);
  assert.equal(finding.severity, 'error');
  assert.match(finding.fixPoint, /allowedPolicies/);
  assert.match(finding.fixPoint, /home-inline/);
  assert.deepEqual(finding.location, { file: 'home-lazy/src/banner.js', line: 7 });
});

test('TT-003 实际类型与写入要求不符: legacy-dom 已获准但不创建 TrustedScript', async () => {
  const { anaB } = await analyzeBoth();
  assert.equal(sinkStatus(anaB, 'home', 's4'), STATUS.TYPE_MISMATCH);
  const finding = anaB.findings.find((f) => f.id === 'type_mismatch:home:promo-widget:s4');
  assert.ok(finding);
  assert.match(finding.detail, /TrustedScript/);
  assert.match(finding.fixPoint, /legacy-dom/);
  assert.match(finding.fixPoint, /TrustedScript/);
});

test('TT-004 清单信息不足: 写入点缺少 requiredType 标注判为 insufficient_info', async () => {
  const { anaB } = await analyzeBoth();
  assert.equal(sinkStatus(anaB, 'home', 's7'), STATUS.INSUFFICIENT_INFO);
  const finding = anaB.findings.find((f) => f.id === 'insufficient_info:home:home-lazy:s7');
  assert.ok(finding);
  assert.equal(finding.severity, 'warning');
  assert.match(finding.fixPoint, /s7/);
});

test('TT-005 覆盖判定: 已获准策略且类型匹配的写入点判为 covered', async () => {
  const { anaB } = await analyzeBoth();
  assert.equal(sinkStatus(anaB, 'home', 's1'), STATUS.COVERED);
  assert.equal(sinkStatus(anaB, 'home', 's2'), STATUS.COVERED);
  assert.equal(sinkStatus(anaB, 'checkout', 's5'), STATUS.COVERED);
  assert.equal(anaB.summary.covered, 5, `实际 covered 数: ${anaB.summary.covered}`);
  assert.equal(anaB.summary.sinkEvaluations ?? anaB.summary.total, 12);
});

test('TT-006 未经任何策略的写入点判为 no_policy', async () => {
  const { anaB } = await analyzeBoth();
  assert.equal(sinkStatus(anaB, 'checkout', 's6'), STATUS.NO_POLICY);
  const finding = anaB.findings.find((f) => f.id === 'no_policy:checkout:pay-widget:s6');
  assert.ok(finding);
  assert.equal(finding.severity, 'error');
  assert.match(finding.fixPoint, /pay-widget\/src\/widget\.js/);
});

test('TT-007 未强制 require-trusted-types-for 判为 not_enforced（合成内存快照）', () => {
  const snap = normalizeSnapshot({
    entries: [{ name: 'app', loadPath: '/app', sharedModules: ['m'], lazyModules: [] }],
    csp: { app: { mode: 'enforce', requireTrustedTypesForScript: false, allowedPolicies: ['p'] } },
    policies: { p: { creates: ['TrustedHTML'] } },
    sinks: [{ id: 'x', module: 'm', sink: 'innerHTML', requiredType: 'TrustedHTML', actualPolicy: 'p' }],
    dynamicDeps: {},
  });
  const ana = analyzeSnapshot(snap);
  assert.equal(sinkStatus(ana, 'app', 'x'), STATUS.NOT_ENFORCED);
  assert.match(ana.findings[0].fixPoint, /requireTrustedTypesForScript/);
});

test('TT-008 缺少 CSP 记录的入口判为 insufficient_info（合成内存快照）', () => {
  const snap = normalizeSnapshot({
    entries: [{ name: 'ghost', loadPath: '/ghost', sharedModules: ['m'], lazyModules: [] }],
    csp: {},
    policies: {},
    sinks: [{ id: 'x', module: 'm', sink: 'innerHTML', requiredType: 'TrustedHTML', actualPolicy: 'p' }],
    dynamicDeps: {},
  });
  const ana = analyzeSnapshot(snap);
  assert.equal(sinkStatus(ana, 'ghost', 'x'), STATUS.INSUFFICIENT_INFO);
  assert.match(ana.findings[0].fixPoint, /csp\.json/);
});

test('TT-009 动态依赖传递展开: home 入口经 home-lazy 加载 promo-widget', async () => {
  const { snapB } = await analyzeBoth();
  const home = snapB.entries.find((e) => e.name === 'home');
  const modules = resolveEntryModules(home, snapB.dynamicDeps);
  const promo = modules.find((m) => m.name === 'promo-widget');
  assert.ok(promo, 'promo-widget 应出现在加载序列');
  assert.equal(promo.kind, 'dynamic');
  assert.deepEqual(promo.chain, ['home-lazy', 'promo-widget']);
});

test('TT-010 共享模块跨入口保护状态: vendor 在 B 中 home 受保护而 checkout/account 未受保护', async () => {
  const { diff } = await analyzeBoth();
  const vendor = diff.sharedModules.find((m) => m.module === 'vendor');
  assert.ok(vendor);
  assert.equal(vendor.inconsistentInB, true);
  const finding = diff.findings.find((f) => f.id === 'shared_module_inconsistent:vendor');
  assert.ok(finding, '应存在共享模块不一致 finding');
  assert.deepEqual([...finding.entries].sort(), ['account', 'checkout']);
  assert.ok(finding.loadPaths.includes('/checkout'));
  assert.match(finding.fixPoint, /csp\.json/);
});

test('TT-011 共享模块不一致收敛: ui-core 在 A 不一致、B 已对齐，标记 resolved 且不再告警', async () => {
  const { diff } = await analyzeBoth();
  const uiCore = diff.sharedModules.find((m) => m.module === 'ui-core');
  assert.ok(uiCore);
  assert.equal(uiCore.inconsistentInA, true, '快照 A 中 checkout 为 report-only，应不一致');
  assert.equal(uiCore.inconsistentInB, false);
  assert.equal(uiCore.resolved, true);
  assert.ok(!diff.findings.some((f) => f.id === 'shared_module_inconsistent:ui-core'));
});

test('TT-012 动态模块新增写入点: s8 未获授权策略覆盖告警，s9 已覆盖仅记录', async () => {
  const { diff } = await analyzeBoth();
  const s8 = diff.newDynamicSinks.find((s) => s.sinkId === 's8');
  const s9 = diff.newDynamicSinks.find((s) => s.sinkId === 's9');
  assert.ok(s8 && s9, '两个新增动态写入点都应被识别');
  assert.equal(s8.covered, false);
  assert.equal(s9.covered, true);
  const finding = diff.findings.find((f) => f.id === 'new_dynamic_sink_uncovered:s8');
  assert.ok(finding);
  assert.equal(finding.severity, 'error');
  assert.deepEqual(finding.entries, ['account']);
  assert.ok(finding.loadPaths[0].includes('account-lazy → stats-widget'), `加载路径: ${finding.loadPaths[0]}`);
  assert.match(finding.fixPoint, /stats-tt/);
  assert.ok(!diff.findings.some((f) => f.id === 'new_dynamic_sink_uncovered:s9'));
});

test('TT-013 覆盖回归检测: 同一写入点由 covered 退化即告警（合成内存快照）', () => {
  const mk = (actualPolicy) => normalizeSnapshot({
    entries: [{ name: 'app', loadPath: '/app', sharedModules: ['m'], lazyModules: [] }],
    csp: { app: { mode: 'enforce', requireTrustedTypesForScript: true, allowedPolicies: ['p'] } },
    policies: { p: { creates: ['TrustedHTML'] } },
    sinks: [{ id: 'x', module: 'm', sink: 'innerHTML', requiredType: 'TrustedHTML', actualPolicy }],
    dynamicDeps: {},
  });
  const snapA = mk('p');
  const snapB = mk(null);
  const anaA = analyzeSnapshot(snapA);
  const anaB = analyzeSnapshot(snapB);
  const diff = diffSnapshots(snapA, snapB, anaA, anaB);
  assert.equal(diff.regressions.length, 1);
  assert.equal(diff.regressions[0].statusAfter, STATUS.NO_POLICY);
  const finding = diff.findings.find((f) => f.id === 'coverage_regression:app:x');
  assert.ok(finding);
  assert.equal(finding.severity, 'error');
});

test('TT-014 真实 fixtures 无覆盖回归: A→B 仅改善不退化', async () => {
  const { diff } = await analyzeBoth();
  assert.deepEqual(diff.regressions, []);
});

test('TT-015 evaluateSink 判定顺序: report-only 优先于策略授权检查', async () => {
  const ev = evaluateSink(
    { id: 'z', module: 'm', sink: 'innerHTML', requiredType: 'TrustedHTML', actualPolicy: 'nope' },
    { mode: 'report-only', requireTrustedTypesForScript: true, allowedPolicies: [] },
    {},
    'entry',
  );
  assert.equal(ev.status, STATUS.REPORT_ONLY);
});
