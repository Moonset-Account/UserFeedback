/**
 * 用例选择器。
 *
 * 每个构建独立求解一个“带强制项与传递依赖的加权集合覆盖”问题：
 *   1. 强制用例（mandatory）及其传递依赖构成必选基线；
 *   2. 从其余适用用例中选择根集合，使其与基线的依赖闭包覆盖全部必测修复要求；
 *   3. 最小化总耗时，并与构建预算比较。
 *
 * 小规模（可选根数量 <= exhaustiveLimit）穷举全部子集，保证全局最优；
 * 大规模使用确定性贪心（单位新增覆盖耗时最小，多重确定性平局规则）+ 反向冗余剪枝。
 * 两条路径都不使用随机数或时间戳，同一输入永远得到同一输出。
 */

import { casesForBuild, dependencyClosure } from './graph.js';

/**
 * @typedef {Object} Reason
 * @property {string} caseId
 * @property {'mandatory'|'coverage'|'dependency'} code
 * @property {string} detail 中文理由
 * @property {string[]} [fixes] coverage 理由下该根边际覆盖的修复要求
 * @property {string[]} [addedBy] dependency 理由下拉入该用例的根用例
 */

function sumDuration(entries, idSet) {
  let total = 0;
  for (const e of entries) if (idSet.has(e.id)) total += e.durationMinutes;
  return total;
}

/**
 * 为最终入选用例生成逐条理由。
 * roots = 强制根 + 主动选择的覆盖根；其余入选用例都是依赖。
 */
function buildReasons(entries, caseIndex, buildId, rootIds, mandatoryRootIds, marginalFixes) {
  const rootSet = new Set(rootIds);
  const reasons = new Map();

  for (const id of rootIds) {
    if (mandatoryRootIds.has(id)) {
      reasons.set(id, {
        caseId: id,
        code: 'mandatory',
        detail: '强制保留用例（mandatory=true），必须入选',
      });
    } else {
      reasons.set(id, {
        caseId: id,
        code: 'coverage',
        detail: `主动选择以覆盖修复要求：${marginalFixes.get(id)?.join('、') || '（与其依赖共同覆盖）'}`,
        fixes: marginalFixes.get(id) ?? [],
      });
    }
  }

  // 非根入选用例：找出哪些根的依赖闭包拉入了它
  const rootClosures = new Map();
  for (const rootId of rootIds) {
    const { closure } = dependencyClosure(caseIndex, [rootId], buildId);
    rootClosures.set(rootId, closure);
  }
  for (const entry of entries) {
    if (rootSet.has(entry.id)) continue;
    const addedBy = [...rootIds].filter((r) => rootClosures.get(r).has(entry.id)).sort();
    reasons.set(entry.id, {
      caseId: entry.id,
      code: 'dependency',
      detail: `未直接选择，作为 ${addedBy.join('、')} 的传递依赖（测试数据/前置用例）被带入`,
      addedBy,
    });
  }
  return [...reasons.values()].sort((a, b) => a.caseId.localeCompare(b.caseId));
}

/**
 * 穷举求解器：枚举可选根的全部子集，保证全局最优。
 * 平局规则：总耗时相同时，主动选择的根数量更少者胜（剔除被其他根闭包包含的冗余根），
 * 再按根编号列表字典序决胜。
 */
function solveExhaustive(entries, caseIndex, buildId, requiredFixes, mandatoryClosure) {
  const ids = entries.map((e) => e.id);
  const indexById = new Map(ids.map((id, i) => [id, i]));
  const n = ids.length;

  // BigInt 位掩码：依赖闭包掩码、修复要求掩码
  const closureMaskById = new Map();
  for (const id of ids) {
    const { closure } = dependencyClosure(caseIndex, [id], buildId);
    let mask = 0n;
    for (const depId of closure) {
      if (indexById.has(depId)) mask |= 1n << BigInt(indexById.get(depId));
    }
    closureMaskById.set(id, mask);
  }
  const fixBit = new Map(requiredFixes.map((f, i) => [f, 1n << BigInt(i)]));
  const fixMaskById = new Map();
  for (const entry of entries) {
    let mask = 0n;
    for (const f of entry.fixes) if (fixBit.has(f)) mask |= fixBit.get(f);
    fixMaskById.set(entry.id, mask);
  }

  const mandatoryRootIds = entries.filter((e) => e.mandatory).map((e) => e.id);
  let baselineMask = 0n;
  let baselineFixMask = 0n;
  for (const id of mandatoryClosure) {
    baselineMask |= 1n << BigInt(indexById.get(id));
    baselineFixMask |= fixMaskById.get(id);
  }
  const requiredMask = requiredFixes.reduce((acc, f) => acc | fixBit.get(f), 0n);

  const optionalIds = ids.filter((id) => !mandatoryClosure.has(id));
  const optCount = optionalIds.length;

  // Gray-code 增量枚举：相邻两个子集只相差一个可选根。
  // 加入根时增量合并其闭包成员；移除根时按剩余根重建闭包，并重算耗时/覆盖
  // （修复要求可能被多个用例共享，不能直接做掩码减法）。
  const recompute = (mask) => {
    let totalCost = 0;
    let fixMask = baselineFixMask;
    for (let i = 0; i < n; i += 1) {
      if ((mask >> BigInt(i)) & 1n) {
        totalCost += entries[i].durationMinutes;
        fixMask |= fixMaskById.get(entries[i].id);
      }
    }
    return { totalCost, fixMask };
  };

  // 同耗时平局规则（依次）：
  // 1. 根数量更少者胜（被其他根闭包包含的冗余根不应算作主动选择）；
  // 2. 主动选择的根集合字典序更小者胜（强制根两边相同，不参与比较）。
  const lexicographicallySmaller = (rootsA, rootsB) => {
    if (rootsA.length !== rootsB.length) return rootsA.length < rootsB.length;
    for (let i = 0; i < rootsA.length; i += 1) {
      if (rootsA[i] < rootsB[i]) return true;
      if (rootsA[i] > rootsB[i]) return false;
    }
    return false;
  };

  let selectedMask = baselineMask;
  let state = recompute(selectedMask);
  let best = null; // { cost, selectedMask, optionalRoots }
  const remember = (optionalRoots) => {
    if ((state.fixMask & requiredMask) !== requiredMask) return;
    const candidate = { cost: state.totalCost, selectedMask, optionalRoots };
    if (
      best === null ||
      candidate.cost < best.cost ||
      (candidate.cost === best.cost && lexicographicallySmaller(candidate.optionalRoots, best.optionalRoots))
    ) {
      best = candidate;
    }
  };
  remember([]); // 空子集（仅强制基线）

  // 维护当前枚举到的可选根布尔向量，翻转位直接从向量变化推出，
  // 不使用 Math.log2（diff >= 2^32 时浮点精度不足）。
  const chosen = new Array(optCount).fill(false);
  const total = 2 ** optCount;
  let prevGray = 0;
  for (let k = 1; k < total; k += 1) {
    const gray = k ^ (k >> 1);
    let bit = 0;
    let diff = gray ^ prevGray;
    while (diff > 1) {
      diff >>= 1;
      bit += 1;
    }
    const id = optionalIds[bit];
    if (!chosen[bit]) {
      chosen[bit] = true;
      selectedMask |= closureMaskById.get(id);
    } else {
      chosen[bit] = false;
      let rebuilt = baselineMask;
      for (let j = 0; j < optCount; j += 1) {
        if (chosen[j]) rebuilt |= closureMaskById.get(optionalIds[j]);
      }
      selectedMask = rebuilt;
    }
    state = recompute(selectedMask);
    prevGray = gray;
    const currentRoots = optionalIds.filter((_, j) => chosen[j]);
    remember(currentRoots);
  }
  if (best !== null) {
    best.rootIds = [...mandatoryRootIds, ...best.optionalRoots].sort();
  }
  return best;
}

/**
 * 确定性贪心求解器 + 反向冗余剪枝。
 * 每步选择“新增单位修复覆盖耗时”最小的可选根；
 * 平局依次按新增耗时升序、编号字典序，保证可复现。
 */
function solveGreedy(entries, caseIndex, buildId, requiredFixes, mandatoryClosure) {
  const entryById = new Map(entries.map((e) => [e.id, e]));
  const mandatoryRootIds = new Set(entries.filter((e) => e.mandatory).map((e) => e.id));

  const roots = [...mandatoryRootIds].sort();
  let closure = new Set(mandatoryClosure);
  const covered = new Set();
  for (const id of closure) for (const f of entryById.get(id).fixes) covered.add(f);

  const pickTrace = []; // { rootId, marginalFixes }
  const required = new Set(requiredFixes);

  const isCovered = () => [...required].every((f) => covered.has(f));

  while (!isCovered()) {
    let bestPick = null;
    for (const entry of entries) {
      if (closure.has(entry.id)) continue;
      const { closure: addClosure } = dependencyClosure(caseIndex, [entry.id], buildId);
      const addedIds = [...addClosure].filter((id) => !closure.has(id));
      const addedCost = addedIds.reduce((acc, id) => acc + entryById.get(id).durationMinutes, 0);
      const newFixes = [...addClosure]
        .flatMap((id) => entryById.get(id).fixes)
        .filter((f) => required.has(f) && !covered.has(f));
      const newFixCount = new Set(newFixes).size;
      if (newFixCount === 0) continue;
      const ratio = addedCost / newFixCount;
      const candidate = { entry, addedIds, addedCost, newFixes: [...new Set(newFixes)].sort(), ratio };
      if (
        bestPick === null ||
        candidate.ratio < bestPick.ratio ||
        (candidate.ratio === bestPick.ratio && candidate.addedCost < bestPick.addedCost) ||
        (candidate.ratio === bestPick.ratio &&
          candidate.addedCost === bestPick.addedCost &&
          candidate.entry.id < bestPick.entry.id)
      ) {
        bestPick = candidate;
      }
    }
    if (bestPick === null) return null; // 存在任何用例都无法覆盖的修复要求
    roots.push(bestPick.entry.id);
    for (const id of bestPick.addedIds) closure.add(id);
    for (const f of bestPick.newFixes) covered.add(f);
    pickTrace.push({ rootId: bestPick.entry.id, marginalFixes: bestPick.newFixes });
  }

  // 反向冗余剪枝：按选择顺序逆序尝试移除覆盖根（强制根不可移），
  // 移除后重新闭包仍覆盖全部要求且耗时不增则剪枝。
  const finalRoots = new Set(roots);
  for (let i = pickTrace.length - 1; i >= 0; i -= 1) {
    const candidateRoot = pickTrace[i].rootId;
    const trial = new Set(finalRoots);
    trial.delete(candidateRoot);
    const { closure: trialClosure } = dependencyClosure(caseIndex, [...trial], buildId);
    const trialCovered = new Set();
    for (const id of trialClosure) for (const f of entryById.get(id).fixes) trialCovered.add(f);
    if ([...required].every((f) => trialCovered.has(f))) {
      finalRoots.delete(candidateRoot);
      closure = trialClosure;
    }
  }

  const marginalFixes = new Map(pickTrace.map((t) => [t.rootId, t.marginalFixes]));
  return {
    rootIds: [...finalRoots].sort(),
    selectedIds: [...closure].sort(),
    marginalFixes,
    optimal: false,
  };
}

/**
 * 对单个构建执行选择。
 * @param {object} manifest loadManifest 的返回值
 * @param {string} buildId
 * @param {{exhaustiveLimit?: number}} [opts]
 */
export function selectForBuild(manifest, buildId, opts = {}) {
  const { cases, caseIndex, builds, defaults } = manifest;
  const build = builds.find((b) => b.id === buildId);
  if (!build) throw new Error(`未知构建：${buildId}`);
  const exhaustiveLimit = opts.exhaustiveLimit ?? defaults.exhaustiveLimit ?? 20;

  const entries = casesForBuild(cases, buildId);
  const entryById = new Map(entries.map((e) => [e.id, e]));

  const conflicts = [];

  // 1. 强制根与必选基线（含传递依赖）
  const mandatoryRootIds = new Set(entries.filter((e) => e.mandatory).map((e) => e.id));
  const baseline = dependencyClosure(caseIndex, [...mandatoryRootIds], buildId);
  for (const m of baseline.missing) {
    conflicts.push({
      code: 'MISSING_DEPENDENCY',
      message: `强制用例的依赖 ${m.dependency} 在构建 ${buildId} 上不可用`,
      details: m,
    });
  }
  const baselineCost = sumDuration(entries, baseline.closure);

  // 2. 覆盖可行性：全部适用用例的修复要求并集
  const providableFixes = new Set(entries.flatMap((e) => e.fixes));
  const uncoverable = build.requiredFixes.filter((f) => !providableFixes.has(f));
  if (uncoverable.length > 0) {
    conflicts.push({
      code: 'REQUIREMENT_UNCOVERABLE',
      message: `构建 ${buildId} 的必测修复要求 ${uncoverable.join('、')} 没有任何适用用例可以验证`,
      details: { requiredFixes: build.requiredFixes, uncoverable },
    });
    return {
      build: buildId,
      status: 'INFEASIBLE',
      optimal: null,
      strategy: 'none',
      budgetMinutes: build.budgetMinutes,
      totalMinutes: null,
      selectedCases: [],
      reasons: [],
      mandatoryRoots: [...mandatoryRootIds].sort(),
      baselineCost,
      conflicts,
    };
  }

  // 3. 求解（穷举阈值按“可选根”数量计算：基线内的强制用例/依赖不参与枚举）
  const optionalCount = entries.filter((e) => !baseline.closure.has(e.id)).length;
  const useExhaustive = optionalCount <= exhaustiveLimit;

  let selectedIds;
  let rootIds;
  let marginalFixes = new Map();
  let optimal;

  if (useExhaustive) {
    const best = solveExhaustive(entries, caseIndex, buildId, build.requiredFixes, baseline.closure);
    if (best === null) {
      conflicts.push({
        code: 'REQUIREMENT_UNCOVERABLE',
        message: `构建 ${buildId} 穷举后仍无法覆盖全部必测修复要求`,
      });
      return {
        build: buildId,
        status: 'INFEASIBLE',
        optimal: null,
        strategy: 'exhaustive',
        budgetMinutes: build.budgetMinutes,
        totalMinutes: null,
        selectedCases: [],
        reasons: [],
        mandatoryRoots: [...mandatoryRootIds].sort(),
        baselineCost,
        conflicts,
      };
    }
    selectedIds = entries
      .map((e, i) => ((best.selectedMask >> BigInt(i)) & 1n ? e.id : null))
      .filter(Boolean);
    rootIds = best.rootIds;
    optimal = true;

    // 穷举路径下按根编号顺序计算边际覆盖，作为入选理由
    let coveredSoFar = new Set();
    for (const rootId of rootIds) {
      const { closure } = dependencyClosure(caseIndex, [rootId], buildId);
      const marginal = [...closure]
        .flatMap((id) => entryById.get(id).fixes)
        .filter((f) => build.requiredFixes.includes(f) && !coveredSoFar.has(f));
      marginalFixes.set(rootId, [...new Set(marginal)].sort());
      for (const f of marginal) coveredSoFar.add(f);
    }
  } else {
    const greedy = solveGreedy(entries, caseIndex, buildId, build.requiredFixes, baseline.closure);
    if (greedy === null) {
      conflicts.push({
        code: 'REQUIREMENT_UNCOVERABLE',
        message: `构建 ${buildId} 贪心求解中断：存在无法覆盖的必测修复要求`,
      });
      return {
        build: buildId,
        status: 'INFEASIBLE',
        optimal: null,
        strategy: 'greedy',
        budgetMinutes: build.budgetMinutes,
        totalMinutes: null,
        selectedCases: [],
        reasons: [],
        mandatoryRoots: [...mandatoryRootIds].sort(),
        baselineCost,
        conflicts,
      };
    }
    selectedIds = greedy.selectedIds;
    rootIds = greedy.rootIds;
    marginalFixes = greedy.marginalFixes;
    optimal = false;
  }

  const totalMinutes = sumDuration(entries, new Set(selectedIds));
  const reasons = buildReasons(
    entries.filter((e) => selectedIds.includes(e.id)),
    caseIndex,
    buildId,
    rootIds,
    mandatoryRootIds,
    marginalFixes,
  );

  const selectedCases = entries
    .filter((e) => selectedIds.includes(e.id))
    .map((e) => ({
      id: e.id,
      durationMinutes: e.durationMinutes,
      fixes: e.fixes,
      mandatory: e.mandatory,
      dependencies: e.dependencies,
    }));

  // 4. 预算判定：覆盖要求与预算无法同时满足时给出冲突诊断
  let status = 'FEASIBLE';
  if (totalMinutes > build.budgetMinutes) {
    status = 'OVER_BUDGET';
    const coveredByBaseline = new Set();
    for (const id of baseline.closure) for (const f of entryById.get(id).fixes) coveredByBaseline.add(f);
    const extraRequirements = build.requiredFixes.filter((f) => !coveredByBaseline.has(f));
    conflicts.push({
      code: 'OPTIMAL_OVER_BUDGET',
      message:
        `构建 ${buildId} 满足全部必测修复要求的最短组合仍超出预算：` +
        `最短 ${totalMinutes} 分钟，预算 ${build.budgetMinutes} 分钟，超支 ${totalMinutes - build.budgetMinutes} 分钟`,
      details: {
        budgetMinutes: build.budgetMinutes,
        minimumMinutes: totalMinutes,
        overrunMinutes: totalMinutes - build.budgetMinutes,
        baselineMinutes: baselineCost,
        requirementsOutsideBaseline: extraRequirements,
        optimal,
      },
    });
  }

  return {
    build: buildId,
    status,
    optimal,
    strategy: useExhaustive ? 'exhaustive' : 'greedy',
    budgetMinutes: build.budgetMinutes,
    totalMinutes,
    remainingBudgetMinutes: build.budgetMinutes - totalMinutes,
    selectedCases,
    reasons,
    mandatoryRoots: [...mandatoryRootIds].sort(),
    baselineCost,
    conflicts,
  };
}

/**
 * 对清单中的全部构建执行选择并汇总。
 */
export function selectAll(manifest, opts = {}) {
  const results = manifest.builds.map((b) => selectForBuild(manifest, b.id, opts));
  const totals = results.reduce(
    (acc, r) => {
      if (r.status === 'FEASIBLE') acc.feasible += 1;
      if (r.status === 'OVER_BUDGET') acc.overBudget += 1;
      if (r.status === 'INFEASIBLE') acc.infeasible += 1;
      if (typeof r.totalMinutes === 'number') acc.minutes += r.totalMinutes;
      return acc;
    },
    { feasible: 0, overBudget: 0, infeasible: 0, minutes: 0 },
  );
  return {
    builds: results,
    summary: totals,
    globalBudgetMinutes: manifest.builds.reduce((acc, b) => acc + b.budgetMinutes, 0),
  };
}
