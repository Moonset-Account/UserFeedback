import assert from 'node:assert/strict';
import { loadManifest } from '../src/manifest.js';

/** 由普通对象构造已校验清单 */
export function manifestOf(obj) {
  return loadManifest(JSON.stringify(obj));
}

/** 确定性伪随机数（LCG），仅测试使用；生产代码不含随机源 */
export function lcg(seed) {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

/**
 * 随机生成结构合法的清单：
 * - 依赖只指向构建范围为其超集的既有条目，保证通过校验；
 * - 每个构建都保证存在“万能覆盖”用例，使问题可行。
 */
export function generateManifest(rand, { buildCount = 3, caseCount = 12, fixCount = 5 } = {}) {
  const buildIds = Array.from({ length: buildCount }, (_, i) => `B${i}`);
  const fixes = Array.from({ length: fixCount }, (_, i) => `F${i}`);
  const cases = [];

  for (let i = 0; i < caseCount; i += 1) {
    const builds = buildIds.filter(() => rand() < 0.6);
    if (builds.length === 0) builds.push(buildIds[Math.floor(rand() * buildIds.length)]);
    const caseFixes = fixes.filter(() => rand() < 0.35);
    cases.push({
      id: `C${String(i).padStart(3, '0')}`,
      durationMinutes: 1 + Math.floor(rand() * 12),
      builds,
      fixes: caseFixes,
      mandatory: rand() < 0.2,
      dependencies: [],
    });
  }

  // 每个构建一个万能用例，确保可行
  for (const b of buildIds) {
    cases.push({
      id: `ALL-${b}`,
      durationMinutes: 30 + Math.floor(rand() * 20),
      builds: [b],
      fixes: [...fixes],
      mandatory: false,
      dependencies: [],
    });
  }

  // 依赖：仅选构建范围超集的既有条目
  for (const c of cases) {
    if (rand() < 0.4) {
      const candidates = cases.filter(
        (other) =>
          other.id !== c.id &&
          c.builds.every((b) => other.builds.includes(b)) &&
          !c.dependencies.includes(other.id),
      );
      if (candidates.length > 0 && rand() < 0.5) {
        c.dependencies.push(candidates[Math.floor(rand() * candidates.length)].id);
      }
    }
  }

  const builds = buildIds.map((id) => ({
    id,
    budgetMinutes: 9999,
    requiredFixes: fixes.filter(() => rand() < 0.7),
  }));
  return { builds, cases };
}

/** 独立参考实现：朴素集合穷举（与 src/selector.js 风格完全不同），用于核对最优耗时 */
export function referenceOptimal(manifest, buildId) {
  const build = manifest.builds.find((b) => b.id === buildId);
  const applicable = manifest.cases.filter((c) => c.builds.includes(buildId));
  const byId = new Map(applicable.map((c) => [c.id, c]));

  function close(rootIds) {
    const out = new Set();
    const stack = [...rootIds];
    while (stack.length) {
      const id = stack.pop();
      if (out.has(id)) continue;
      out.add(id);
      const entry = byId.get(id);
      if (entry) stack.push(...entry.dependencies.filter((d) => byId.has(d)));
    }
    return out;
  }

  const mandatory = applicable.filter((c) => c.mandatory).map((c) => c.id);
  const base = close(mandatory);
  const optional = applicable.filter((c) => !base.has(c.id)).map((c) => c.id);

  const covers = (set) => {
    const got = new Set();
    for (const id of set) for (const f of byId.get(id).fixes) got.add(f);
    return build.requiredFixes.every((f) => got.has(f));
  };
  const cost = (set) => [...set].reduce((acc, id) => acc + byId.get(id).durationMinutes, 0);

  if (!covers(close([...applicable.map((c) => c.id)]))) return null;

  let best = null;
  const n = optional.length;
  for (let mask = 0; mask < 2 ** n; mask += 1) {
    const roots = [...mandatory];
    for (let i = 0; i < n; i += 1) if (mask & (1 << i)) roots.push(optional[i]);
    const set = close(roots);
    if (!covers(set)) continue;
    const c = cost(set);
    if (best === null || c < best.cost) best = { cost: c, ids: [...set].sort() };
  }
  return best;
}

/** 校验选择结果的通用不变量 */
export function assertValidSelection(manifest, result, { exhaustive = false } = {}) {
  const applicable = manifest.cases.filter((c) => c.builds.includes(result.build));
  const byId = new Map(applicable.map((c) => [c.id, c]));
  const selectedIds = result.selectedCases.map((c) => c.id);
  const selectedSet = new Set(selectedIds);

  // 强制用例全部入选
  for (const c of applicable) {
    if (c.mandatory) assert.ok(selectedSet.has(c.id), `强制用例 ${c.id} 未入选`);
  }
  // 依赖闭包完整
  for (const id of selectedIds) {
    for (const dep of byId.get(id).dependencies) {
      assert.ok(selectedSet.has(dep), `${id} 的依赖 ${dep} 未入选`);
    }
  }
  // 全部必测修复要求被覆盖
  const covered = new Set();
  for (const id of selectedIds) for (const f of byId.get(id).fixes) covered.add(f);
  const build = manifest.builds.find((b) => b.id === result.build);
  for (const f of build.requiredFixes) assert.ok(covered.has(f), `修复要求 ${f} 未覆盖`);
  // 耗时求和一致
  const expectedTotal = selectedIds.reduce((acc, id) => acc + byId.get(id).durationMinutes, 0);
  assert.equal(result.totalMinutes, expectedTotal);
  // 每条入选用例都有理由
  assert.deepEqual(
    result.reasons.map((r) => r.caseId).sort(),
    selectedIds.sort(),
  );
  if (exhaustive) assert.equal(result.optimal, true);
}
