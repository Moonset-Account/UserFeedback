/**
 * 清单加载与校验。
 *
 * 清单 schema（JSON）：
 * {
 *   "builds": [
 *     { "id": "linux-x64", "budgetMinutes": 60, "requiredFixes": ["FIX-A", "FIX-B"] }
 *   ],
 *   "cases": [
 *     {
 *       "id": "SEC-001",                      // 用例编号；允许重号，但重号条目的适用构建必须互不相交
 *       "durationMinutes": 5,                 // 运行耗时（非负数值）
 *       "builds": ["linux-x64", "arm64"],     // 适用构建
 *       "fixes": ["FIX-A"],                   // 可验证的修复要求
 *       "mandatory": true,                    // 是否必须保留（仅对适用构建生效）
 *       "dependencies": ["SEC-002"]           // 测试数据/前置用例依赖（依赖必须同样适用该构建）
 *     }
 *   ],
 *   "defaults": { "budgetMinutes": 60, "exhaustiveLimit": 20 }
 * }
 */

const CASE_FIELDS = ['id', 'durationMinutes', 'builds', 'fixes', 'mandatory', 'dependencies'];

function isNonEmptyString(v) {
  return typeof v === 'string' && v.trim().length > 0;
}

function uniqueSorted(values) {
  return [...new Set(values)].sort();
}

/**
 * 读取并校验清单。
 * @param {string} rawText 清单原始文本
 * @returns {{builds: object[], cases: object[], defaults: object}} 归一化后的清单
 * @throws {{code: string, message: string, details?: any[]}} 校验失败时抛出结构化错误
 */
export function loadManifest(rawText) {
  let raw;
  try {
    raw = JSON.parse(rawText);
  } catch (err) {
    throw { code: 'INVALID_JSON', message: `清单不是合法 JSON：${err.message}` };
  }
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    throw { code: 'INVALID_SCHEMA', message: '清单根节点必须是对象' };
  }

  const errors = [];

  // ---- 构建配置 ----
  if (!Array.isArray(raw.builds) || raw.builds.length === 0) {
    errors.push({ code: 'INVALID_SCHEMA', message: 'builds 必须是非空数组' });
  }
  const builds = [];
  const buildIds = new Set();
  for (const b of raw.builds ?? []) {
    if (!b || typeof b !== 'object' || Array.isArray(b)) {
      errors.push({ code: 'INVALID_SCHEMA', message: `构建条目必须是对象：${JSON.stringify(b)}` });
      continue;
    }
    if (!isNonEmptyString(b.id)) {
      errors.push({ code: 'INVALID_SCHEMA', message: '构建缺少非空 id' });
      continue;
    }
    if (buildIds.has(b.id)) {
      errors.push({ code: 'DUPLICATE_BUILD', message: `构建 id 重复：${b.id}`, details: [b.id] });
      continue;
    }
    const budget = Number(b.budgetMinutes ?? raw.defaults?.budgetMinutes);
    if (!Number.isFinite(budget) || budget < 0) {
      errors.push({ code: 'INVALID_SCHEMA', message: `构建 ${b.id} 的 budgetMinutes 必须是非负数` });
      continue;
    }
    const requiredFixes = Array.isArray(b.requiredFixes) ? b.requiredFixes : [];
    if (!requiredFixes.every(isNonEmptyString)) {
      errors.push({ code: 'INVALID_SCHEMA', message: `构建 ${b.id} 的 requiredFixes 必须是字符串数组` });
      continue;
    }
    buildIds.add(b.id);
    builds.push({ id: b.id, budgetMinutes: budget, requiredFixes: uniqueSorted(requiredFixes) });
  }

  // ---- 用例条目 ----
  if (!Array.isArray(raw.cases) || raw.cases.length === 0) {
    errors.push({ code: 'INVALID_SCHEMA', message: 'cases 必须是非空数组' });
  }
  const cases = [];
  const caseIndex = new Map(); // id -> 归一化条目数组（重号时多条）
  for (const c of raw.cases ?? []) {
    if (!c || typeof c !== 'object' || Array.isArray(c)) {
      errors.push({ code: 'INVALID_SCHEMA', message: `用例条目必须是对象：${JSON.stringify(c)}` });
      continue;
    }
    const unknown = Object.keys(c).filter((k) => !CASE_FIELDS.includes(k));
    if (!isNonEmptyString(c.id)) {
      errors.push({ code: 'INVALID_SCHEMA', message: '用例缺少非空 id' });
      continue;
    }
    if (!Number.isFinite(c.durationMinutes) || c.durationMinutes < 0) {
      errors.push({ code: 'INVALID_SCHEMA', message: `用例 ${c.id} 的 durationMinutes 必须是非负数` });
      continue;
    }
    if (!Array.isArray(c.builds) || c.builds.length === 0 || !c.builds.every(isNonEmptyString)) {
      errors.push({ code: 'INVALID_SCHEMA', message: `用例 ${c.id} 的 builds 必须是非空字符串数组` });
      continue;
    }
    if (!Array.isArray(c.fixes) || !c.fixes.every(isNonEmptyString)) {
      errors.push({ code: 'INVALID_SCHEMA', message: `用例 ${c.id} 的 fixes 必须是字符串数组` });
      continue;
    }
    if (!Array.isArray(c.dependencies ?? []) || !(c.dependencies ?? []).every(isNonEmptyString)) {
      errors.push({ code: 'INVALID_SCHEMA', message: `用例 ${c.id} 的 dependencies 必须是字符串数组` });
      continue;
    }
    const normalized = {
      id: c.id,
      durationMinutes: c.durationMinutes,
      builds: uniqueSorted(c.builds),
      fixes: uniqueSorted(c.fixes),
      mandatory: c.mandatory === true,
      dependencies: uniqueSorted(c.dependencies ?? []),
      unknownFields: unknown,
    };
    cases.push(normalized);
    if (!caseIndex.has(normalized.id)) caseIndex.set(normalized.id, []);
    caseIndex.get(normalized.id).push(normalized);
  }

  // ---- 重号用例：适用构建范围必须互不相交（“只在某构建有效的重复用例”合法） ----
  for (const [id, entries] of caseIndex) {
    if (entries.length < 2) continue;
    const seen = new Set();
    for (const entry of entries) {
      for (const b of entry.builds) {
        if (seen.has(b)) {
          errors.push({
            code: 'DUPLICATE_CASE_SCOPE_OVERLAP',
            message: `用例编号 ${id} 出现多次且在构建 ${b} 上同时有效；同一构建内编号必须唯一`,
            details: [{ id, build: b }],
          });
        }
        seen.add(b);
      }
    }
  }

  // ---- 依赖完整性（构建范围内依赖必须存在且同样适用） ----
  for (const c of cases) {
    for (const depId of c.dependencies) {
      const depEntries = caseIndex.get(depId);
      if (!depEntries) {
        errors.push({
          code: 'MISSING_DEPENDENCY',
          message: `用例 ${c.id} 依赖不存在的用例 ${depId}`,
          details: [{ case: c.id, dependency: depId }],
        });
        continue;
      }
      for (const b of c.builds) {
        const ok = depEntries.some((d) => d.builds.includes(b));
        if (!ok) {
          errors.push({
            code: 'DEPENDENCY_NOT_APPLICABLE',
            message: `用例 ${c.id} 在构建 ${b} 上依赖 ${depId}，但后者不适用于该构建`,
            details: [{ case: c.id, dependency: depId, build: b }],
          });
        }
      }
    }
  }

  // ---- 构建 id 引用校验 ----
  for (const c of cases) {
    for (const b of c.builds) {
      if (!buildIds.has(b)) {
        errors.push({
          code: 'UNKNOWN_BUILD_REF',
          message: `用例 ${c.id} 引用了未在 builds 中声明的构建 ${b}`,
          details: [{ case: c.id, build: b }],
        });
      }
    }
  }

  if (errors.length > 0) {
    throw {
      code: 'VALIDATION_FAILED',
      message: `清单校验失败，共 ${errors.length} 个问题`,
      details: errors,
    };
  }

  const defaults = {
    budgetMinutes: Number(raw.defaults?.budgetMinutes) || 0,
    exhaustiveLimit: Number.isFinite(raw.defaults?.exhaustiveLimit)
      ? Math.max(0, Math.floor(raw.defaults.exhaustiveLimit))
      : 20,
  };

  return { builds, cases, defaults, caseIndex };
}
