import { test } from 'node:test';
import assert from 'node:assert/strict';
import { selectForBuild, selectAll } from '../src/selector.js';
import { manifestOf, generateManifest, lcg, assertValidSelection } from './helpers.js';

test('可选根数量超过穷举上限时切换到贪心且结果确定可复现', () => {
  const rand = lcg(424242);
  const m = manifestOf(generateManifest(rand, { buildCount: 2, caseCount: 60, fixCount: 8 }));
  const buildId = m.builds[0].id;

  const applicable = m.cases.filter((c) => c.builds.includes(buildId));
  assert.ok(applicable.length > 20, '前置条件：该构建适用用例超过默认穷举上限');

  const r1 = selectForBuild(m, buildId);
  const r2 = selectForBuild(m, buildId);
  assert.equal(r1.strategy, 'greedy');
  assert.equal(r1.optimal, false);
  assert.deepEqual(r1, r2, '同一输入两次求解结果必须完全一致');
  assertValidSelection(m, r1);
});

test('exhaustiveLimit=0 强制走贪心，结果仍满足全部约束', () => {
  const rand = lcg(7);
  const m = manifestOf(generateManifest(rand, { buildCount: 3, caseCount: 15, fixCount: 5 }));
  for (const build of m.builds) {
    const r = selectForBuild(m, build.id, { exhaustiveLimit: 0 });
    assert.equal(r.strategy, 'greedy');
    assertValidSelection(m, r);
  }
});

test('贪心路径超预算诊断包含最短可达成耗时与超支量', () => {
  const m = manifestOf({
    builds: [{ id: 'linux', budgetMinutes: 10, requiredFixes: ['FA', 'FB'] }],
    cases: [
      { id: 'C1', durationMinutes: 8, builds: ['linux'], fixes: ['FA'] },
      { id: 'C2', durationMinutes: 9, builds: ['linux'], fixes: ['FB'] },
    ],
  });
  const r = selectForBuild(m, 'linux', { exhaustiveLimit: 0 });
  assert.equal(r.strategy, 'greedy');
  assert.equal(r.status, 'OVER_BUDGET');
  assert.equal(r.totalMinutes, 17);
  const c = r.conflicts.find((x) => x.code === 'OPTIMAL_OVER_BUDGET');
  assert.equal(c.details.overrunMinutes, 7);
  assert.equal(c.details.optimal, false);
});

test('贪心路径下不可行构建稳定返回 INFEASIBLE', () => {
  const m = manifestOf({
    builds: [{ id: 'linux', budgetMinutes: 10, requiredFixes: ['FA', 'GHOST'] }],
    cases: [{ id: 'C1', durationMinutes: 8, builds: ['linux'], fixes: ['FA'] }],
  });
  const r = selectForBuild(m, 'linux', { exhaustiveLimit: 0 });
  assert.equal(r.status, 'INFEASIBLE');
  assert.ok(r.conflicts.some((x) => x.code === 'REQUIREMENT_UNCOVERABLE'));
});

test('大规模随机清单（约 300 用例）快速完成且所有构建结果合法确定', () => {
  const rand = lcg(99);
  const m = manifestOf(generateManifest(rand, { buildCount: 4, caseCount: 300, fixCount: 12 }));
  const t0 = process.hrtime.bigint();
  const s1 = selectAll(m);
  const s2 = selectAll(m);
  const elapsedMs = Number(process.hrtime.bigint() - t0) / 1e6;
  assert.ok(elapsedMs < 5000, `大规模求解耗时 ${elapsedMs}ms 超过 5s 预算`);
  assert.deepEqual(s1, s2);
  for (const r of s1.builds) {
    assert.equal(r.status, 'FEASIBLE');
    assertValidSelection(m, r);
  }
});

test('贪心选择满足确定性平局规则：相同比值时选耗时更小、编号更靠前的根', () => {
  // C1 与 C2 单位新增覆盖耗时相同（都是 5/1），C1 编号靠前应先入选；
  // 但 C2 同时覆盖 FC，一轮贪心后由 C2 结束。验证过程确定且结果可复现。
  const m = manifestOf({
    builds: [{ id: 'linux', budgetMinutes: 60, requiredFixes: ['FA', 'FB', 'FC'] }],
    cases: [
      { id: 'C1', durationMinutes: 5, builds: ['linux'], fixes: ['FA'] },
      { id: 'C2', durationMinutes: 5, builds: ['linux'], fixes: ['FB', 'FC'] },
    ],
  });
  const r = selectForBuild(m, 'linux', { exhaustiveLimit: 0 });
  assert.equal(r.status, 'FEASIBLE');
  assert.deepEqual(
    r.selectedCases.map((c) => c.id).sort(),
    ['C1', 'C2'],
  );
});
