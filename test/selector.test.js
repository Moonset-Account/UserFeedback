import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { loadManifest } from '../src/manifest.js';
import { selectForBuild, selectAll } from '../src/selector.js';
import { detectAllCycles } from '../src/graph.js';
import { manifestOf, referenceOptimal, assertValidSelection } from './helpers.js';

const here = dirname(fileURLToPath(import.meta.url));

function simpleManifest(overrides = {}) {
  return manifestOf({
    builds: [{ id: 'linux', budgetMinutes: 30, requiredFixes: ['FA', 'FB', 'FC'] }],
    cases: [
      { id: 'M1', durationMinutes: 4, builds: ['linux'], fixes: ['FA'], mandatory: true, dependencies: ['D1'] },
      { id: 'D1', durationMinutes: 2, builds: ['linux'], fixes: [], mandatory: false, dependencies: [] },
      { id: 'C2', durationMinutes: 6, builds: ['linux'], fixes: ['FB'], mandatory: false, dependencies: [] },
      { id: 'C3', durationMinutes: 11, builds: ['linux'], fixes: ['FB', 'FC'], mandatory: false, dependencies: [] },
      { id: 'C4', durationMinutes: 4, builds: ['linux'], fixes: ['FC'], mandatory: false, dependencies: ['D2'] },
      { id: 'D2', durationMinutes: 3, builds: ['linux'], fixes: [], mandatory: false, dependencies: [] },
    ],
    ...overrides,
  });
}

test('强制用例及其传递依赖被保留，且选择最短覆盖组合', () => {
  const m = simpleManifest();
  const r = selectForBuild(m, 'linux');
  assert.equal(r.status, 'FEASIBLE');
  assert.equal(r.strategy, 'exhaustive');
  assert.equal(r.optimal, true);

  // M1(4)+D1(2) 为强制基线；FB+FC 的最优选法是 C3(11)，
  // 优于 C2(6)+C4(4)+D2(3)=13；总计 6+11=17
  assert.deepEqual(
    r.selectedCases.map((c) => c.id).sort(),
    ['C3', 'D1', 'M1'],
  );
  assert.equal(r.totalMinutes, 17);
  assert.equal(r.remainingBudgetMinutes, 13);
  assertValidSelection(m, r, { exhaustive: true });

  const byId = new Map(r.reasons.map((x) => [x.caseId, x]));
  assert.equal(byId.get('M1').code, 'mandatory');
  assert.equal(byId.get('D1').code, 'dependency');
  assert.deepEqual(byId.get('D1').addedBy, ['M1']);
  assert.equal(byId.get('C3').code, 'coverage');
  assert.deepEqual(byId.get('C3').fixes, ['FB', 'FC']);
});

test('零必测要求时仅保留强制基线', () => {
  const m = simpleManifest({ builds: [{ id: 'linux', budgetMinutes: 30, requiredFixes: [] }] });
  const r = selectForBuild(m, 'linux');
  assert.deepEqual(
    r.selectedCases.map((c) => c.id).sort(),
    ['D1', 'M1'],
  );
  assert.equal(r.totalMinutes, 6);
});

test('必测要求无任何适用用例可验证时报 REQUIREMENT_UNCOVERABLE', () => {
  const m = simpleManifest({
    builds: [{ id: 'linux', budgetMinutes: 30, requiredFixes: ['FA', 'FX'] }],
  });
  const r = selectForBuild(m, 'linux');
  assert.equal(r.status, 'INFEASIBLE');
  assert.ok(r.conflicts.some((c) => c.code === 'REQUIREMENT_UNCOVERABLE'));
  assert.deepEqual(
    r.conflicts.find((c) => c.code === 'REQUIREMENT_UNCOVERABLE').details.uncoverable,
    ['FX'],
  );
});

test('最优组合仍超预算时报 OPTIMAL_OVER_BUDGET 并给出超支诊断', () => {
  const m = simpleManifest({ builds: [{ id: 'linux', budgetMinutes: 10, requiredFixes: ['FA', 'FB', 'FC'] }] });
  const r = selectForBuild(m, 'linux');
  assert.equal(r.status, 'OVER_BUDGET');
  const conflict = r.conflicts.find((c) => c.code === 'OPTIMAL_OVER_BUDGET');
  assert.ok(conflict);
  assert.equal(conflict.details.minimumMinutes, 17);
  assert.equal(conflict.details.overrunMinutes, 7);
  assert.equal(conflict.details.baselineMinutes, 6);
  // 基线之外仍需覆盖的要求
  assert.deepEqual(conflict.details.requirementsOutsideBaseline.sort(), ['FB', 'FC']);
});

test('强制基线本身超预算时同样给出冲突诊断', () => {
  const m = simpleManifest({ builds: [{ id: 'linux', budgetMinutes: 5, requiredFixes: [] }] });
  const r = selectForBuild(m, 'linux');
  assert.equal(r.status, 'OVER_BUDGET');
  assert.equal(r.totalMinutes, 6);
  assert.equal(r.conflicts[0].details.overrunMinutes, 1);
});

test('依赖环：可正常计算闭包并通过全局检测输出环警告', () => {
  const m = manifestOf({
    builds: [{ id: 'linux', budgetMinutes: 30, requiredFixes: ['FA'] }],
    cases: [
      { id: 'A', durationMinutes: 1, builds: ['linux'], fixes: ['FA'], dependencies: ['B'] },
      { id: 'B', durationMinutes: 2, builds: ['linux'], fixes: [], dependencies: ['A'] },
    ],
  });
  const cycles = detectAllCycles(m.cases, m.caseIndex, m.builds);
  assert.equal(cycles.length, 1);
  assert.deepEqual(cycles[0], { build: 'linux', cases: ['A', 'B'] });

  const r = selectForBuild(m, 'linux');
  assert.equal(r.status, 'FEASIBLE');
  assert.deepEqual(
    r.selectedCases.map((c) => c.id).sort(),
    ['A', 'B'],
  );
  assert.equal(r.totalMinutes, 3);
});

test('依赖环仅存在于其他构建时，当前构建不报告该环', () => {
  const m = manifestOf({
    builds: [
      { id: 'linux', budgetMinutes: 30, requiredFixes: ['FA'] },
      { id: 'arm', budgetMinutes: 30, requiredFixes: [] },
    ],
    cases: [
      // 重号条目：linux 上 A 不依赖任何用例；arm 上 A<->B 成环
      { id: 'A', durationMinutes: 1, builds: ['linux'], fixes: ['FA'], dependencies: [] },
      { id: 'A', durationMinutes: 1, builds: ['arm'], fixes: ['FA'], dependencies: ['B'] },
      { id: 'B', durationMinutes: 2, builds: ['arm'], fixes: [], dependencies: ['A'] },
    ],
  });
  const cycles = detectAllCycles(m.cases, m.caseIndex, m.builds);
  assert.deepEqual(cycles.map((c) => c.build), ['arm']);

  const linux = selectForBuild(m, 'linux');
  assert.deepEqual(linux.selectedCases.map((c) => c.id), ['A']);
  assert.equal(linux.totalMinutes, 1);
});

test('重号分构建条目在各自构建独立求解', () => {
  const m = manifestOf({
    builds: [
      { id: 'linux', budgetMinutes: 30, requiredFixes: ['FA'] },
      { id: 'arm', budgetMinutes: 30, requiredFixes: ['FA'] },
    ],
    cases: [
      { id: 'C1', durationMinutes: 5, builds: ['linux'], fixes: ['FA'] },
      { id: 'C1', durationMinutes: 9, builds: ['arm'], fixes: ['FA'] },
    ],
  });
  const linux = selectForBuild(m, 'linux');
  const arm = selectForBuild(m, 'arm');
  assert.equal(linux.totalMinutes, 5);
  assert.equal(arm.totalMinutes, 9);
});

test('示例清单端到端：linux 取最小组合、windows 超预算、环被报告', () => {
  const raw = readFileSync(join(here, '..', 'examples', 'manifest.example.json'), 'utf8');
  const m = loadManifest(raw);
  const selection = selectAll(m);

  const linux = selection.builds.find((b) => b.build === 'linux-x64');
  assert.equal(linux.status, 'FEASIBLE');
  // SEC-001(5)+SEC-008(3) 强制基线；FB+FC 最优为 SEC-003(11)，
  // 优于 SEC-002(6)+SEC-010(4)+SEC-011(4)=14；总计 8+11=19
  assert.deepEqual(
    linux.selectedCases.map((c) => c.id).sort(),
    ['SEC-001', 'SEC-003', 'SEC-008'],
  );
  assert.equal(linux.totalMinutes, 19);

  const arm = selection.builds.find((b) => b.build === 'arm64');
  assert.equal(arm.status, 'FEASIBLE');
  assert.deepEqual(
    arm.selectedCases.map((c) => c.id).sort(),
    ['SEC-001', 'SEC-002', 'SEC-008'],
  );
  assert.equal(arm.totalMinutes, 15);

  const win = selection.builds.find((b) => b.build === 'windows-x64');
  assert.equal(win.status, 'OVER_BUDGET');
  assert.equal(win.totalMinutes, 8);
  assert.equal(win.conflicts[0].details.overrunMinutes, 3);

  const cycles = detectAllCycles(m.cases, m.caseIndex, m.builds);
  assert.deepEqual(cycles, [{ build: 'linux-x64', cases: ['SEC-010', 'SEC-011'] }]);
});

test('穷举结果与独立参考实现逐例核对最优性（随机清单多轮）', () => {
  // 动态导入避免辅助模块顶层加载顺序问题
  return import('./helpers.js').then(({ generateManifest, lcg }) => {
    const rand = lcg(20261112);
    for (let round = 0; round < 60; round += 1) {
      const m = manifestOf(
        generateManifest(rand, { buildCount: 2 + Math.floor(rand() * 2), caseCount: 8 + Math.floor(rand() * 6), fixCount: 4 }),
      );
      for (const build of m.builds) {
        const result = selectForBuild(m, build.id);
        const ref = referenceOptimal(m, build.id);
        assert.ok(ref, `第 ${round} 轮 ${build.id}：参考实现判定可行但得到 null`);
        assert.equal(result.status, 'FEASIBLE', `第 ${round} 轮：预算 9999 不应超支`);
        assert.equal(result.strategy, 'exhaustive');
        assert.equal(result.totalMinutes, ref.cost, `第 ${round} 轮 ${build.id}：最优耗时不一致`);
        assertValidSelection(m, result, { exhaustive: true });
      }
    }
  });
});

test('穷举平局规则确定：同耗时选择编号字典序更小的方案', () => {
  const m = manifestOf({
    builds: [{ id: 'linux', budgetMinutes: 30, requiredFixes: ['FA'] }],
    cases: [
      { id: 'ZETA', durationMinutes: 5, builds: ['linux'], fixes: ['FA'] },
      { id: 'ALPHA', durationMinutes: 5, builds: ['linux'], fixes: ['FA'] },
    ],
  });
  const r1 = selectForBuild(m, 'linux');
  const r2 = selectForBuild(m, 'linux');
  assert.deepEqual(r1.selectedCases.map((c) => c.id), ['ALPHA']);
  assert.deepEqual(r1.selectedCases, r2.selectedCases);
});

test('同一清单上贪心结果的耗时不优于穷举最优，且两者都满足约束', () => {
  return import('./helpers.js').then(({ generateManifest, lcg }) => {
    const rand = lcg(31337);
    for (let round = 0; round < 25; round += 1) {
      const m = manifestOf(generateManifest(rand, { buildCount: 2, caseCount: 14, fixCount: 5 }));
      for (const build of m.builds) {
        const exhaustive = selectForBuild(m, build.id);
        const greedy = selectForBuild(m, build.id, { exhaustiveLimit: 0 });
        assert.equal(greedy.strategy, 'greedy');
        assert.ok(greedy.totalMinutes >= exhaustive.totalMinutes,
          `第 ${round} 轮 ${build.id}：贪心 ${greedy.totalMinutes} 不应优于穷举最优 ${exhaustive.totalMinutes}`);
        assertValidSelection(m, greedy);
      }
    }
  });
});

test('穷举阈值按可选根数量计算：基线内条目不占枚举名额', () => {
  // 1 个强制根在基线内 + 20 个可选根（恰好默认上限）=> 穷举
  const casesA = [
    { id: 'C00', durationMinutes: 1, builds: ['b'], fixes: ['F0'], mandatory: true },
  ];
  for (let i = 1; i <= 20; i += 1) {
    casesA.push({ id: `C${String(i).padStart(2, '0')}`, durationMinutes: 1, builds: ['b'], fixes: i === 1 ? ['F1'] : [] });
  }
  const mA = manifestOf({ builds: [{ id: 'b', budgetMinutes: 99, requiredFixes: ['F0', 'F1'] }], cases: casesA });
  const rA = selectForBuild(mA, 'b');
  assert.equal(rA.strategy, 'exhaustive');
  assert.equal(rA.optimal, true);

  // 无强制项、21 个可选根 => 贪心
  const casesB = casesA.map((c) => ({ ...c, mandatory: false }));
  const mB = manifestOf({ builds: [{ id: 'b', budgetMinutes: 99, requiredFixes: ['F0', 'F1'] }], cases: casesB });
  const rB = selectForBuild(mB, 'b');
  assert.equal(rB.strategy, 'greedy');
});

test('仅依赖拉入的零修复用例也会入选并给出 dependency 理由', () => {
  const m = manifestOf({
    builds: [{ id: 'linux', budgetMinutes: 30, requiredFixes: ['FA'] }],
    cases: [
      { id: 'ROOT', durationMinutes: 4, builds: ['linux'], fixes: ['FA'], dependencies: ['DATA'] },
      { id: 'DATA', durationMinutes: 2, builds: ['linux'], fixes: [], dependencies: ['SEED'] },
      { id: 'SEED', durationMinutes: 1, builds: ['linux'], fixes: [], dependencies: [] },
    ],
  });
  const r = selectForBuild(m, 'linux');
  assert.deepEqual(r.selectedCases.map((c) => c.id).sort(), ['DATA', 'ROOT', 'SEED']);
  assert.equal(r.totalMinutes, 7);
  const byId = new Map(r.reasons.map((x) => [x.caseId, x]));
  assert.equal(byId.get('DATA').code, 'dependency');
  assert.deepEqual(byId.get('DATA').addedBy, ['ROOT']);
  assert.deepEqual(byId.get('SEED').addedBy, ['ROOT']);
});
