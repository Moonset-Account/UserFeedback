/**
 * 依赖图工具：按构建作用域计算依赖闭包与检测依赖环。
 *
 * 同一用例编号在不同构建上可能对应不同条目（重号且构建范围不相交），
 * 因此所有图操作都以“构建 id + 用例编号”为作用域。
 */

/**
 * 取某构建下指定编号的用例条目。
 * @returns {object|undefined}
 */
export function entryForBuild(caseIndex, caseId, buildId) {
  const entries = caseIndex.get(caseId);
  if (!entries) return undefined;
  return entries.find((c) => c.builds.includes(buildId));
}

/**
 * 列出适用于某构建的全部用例，按 id 排序，保证后续所有处理确定。
 */
export function casesForBuild(cases, buildId) {
  return cases.filter((c) => c.builds.includes(buildId)).sort((a, b) => a.id.localeCompare(b.id));
}

/**
 * 计算根用例集合的传递依赖闭包（包含根本身）。
 * 依赖目标若在该构建不存在会被跳过；清单校验阶段已保证不会发生，
 * 这里仍做防御处理并通过 missing 返回异常。
 *
 * @param {string[]} rootIds
 * @returns {{closure: Set<string>, order: string[], missing: Array<{case:string, dependency:string}>}}
 */
export function dependencyClosure(caseIndex, rootIds, buildId) {
  const closure = new Set();
  const order = [];
  const missing = [];
  const stack = [...rootIds].sort();
  while (stack.length > 0) {
    const id = stack.pop();
    if (closure.has(id)) continue;
    const entry = entryForBuild(caseIndex, id, buildId);
    if (!entry) {
      missing.push({ case: '<root>', dependency: id });
      continue;
    }
    closure.add(id);
    order.push(id);
    // 逆序压栈，使依赖按字典序展开，保持确定性
    const deps = [...entry.dependencies].sort().reverse();
    for (const dep of deps) {
      if (!closure.has(dep)) {
        if (!entryForBuild(caseIndex, dep, buildId)) {
          missing.push({ case: id, dependency: dep });
        }
        stack.push(dep);
      }
    }
  }
  return { closure, order: order.sort(), missing };
}

/**
 * 在构建作用域内检测依赖环。
 * @returns {cycles: string[][]} 每个环为按编号排序的用例 id 列表；无环返回空数组
 */
export function detectCycles(cases, caseIndex, buildId) {
  const applicable = casesForBuild(cases, buildId);
  const WHITE = 0;
  const GRAY = 1;
  const BLACK = 2;
  const color = new Map(applicable.map((c) => [c.id, WHITE]));
  const stack = [];
  const cycles = [];
  const seenCycleKeys = new Set();

  function visit(id) {
    color.set(id, GRAY);
    stack.push(id);
    const entry = entryForBuild(caseIndex, id, buildId);
    for (const dep of [...(entry?.dependencies ?? [])].sort()) {
      if (!color.has(dep)) continue; // 不适用/不存在的依赖由校验负责
      if (color.get(dep) === GRAY) {
        const start = stack.indexOf(dep);
        const cycle = stack.slice(start).sort();
        const key = cycle.join('|');
        if (!seenCycleKeys.has(key)) {
          seenCycleKeys.add(key);
          cycles.push(cycle);
        }
      } else if (color.get(dep) === WHITE) {
        visit(dep);
      }
    }
    stack.pop();
    color.set(id, BLACK);
  }

  for (const c of applicable) {
    if (color.get(c.id) === WHITE) visit(c.id);
  }
  return cycles;
}

/**
 * 检测全部构建上的依赖环，返回带构建信息的确定性列表。
 */
export function detectAllCycles(cases, caseIndex, builds) {
  const out = [];
  for (const b of builds) {
    for (const cycle of detectCycles(cases, caseIndex, b.id)) {
      out.push({ build: b.id, cases: cycle });
    }
  }
  return out;
}
