// Trusted Types 策略覆盖审计 —— 核心分析逻辑。
// 输入：各入口构建清单(entries.json)、CSP 策略记录(csp.json)、
//       可信类型策略定义(policies.json)、已标注 DOM 写入点(sinks.json)、
//       动态模块依赖(dynamic-deps.json)。
// 输出：按入口的覆盖判定、跨快照对比结果与可机读 findings。

import { readFile } from 'node:fs/promises';
import path from 'node:path';

export const STATUS = Object.freeze({
  COVERED: 'covered',
  REPORT_ONLY: 'report_only',
  NOT_ENFORCED: 'not_enforced',
  POLICY_NOT_AUTHORIZED: 'policy_not_authorized',
  TYPE_MISMATCH: 'type_mismatch',
  NO_POLICY: 'no_policy',
  INSUFFICIENT_INFO: 'insufficient_info',
});

export const SEVERITY = Object.freeze({
  [STATUS.REPORT_ONLY]: 'warning',
  [STATUS.NOT_ENFORCED]: 'error',
  [STATUS.POLICY_NOT_AUTHORIZED]: 'error',
  [STATUS.TYPE_MISMATCH]: 'error',
  [STATUS.NO_POLICY]: 'error',
  [STATUS.INSUFFICIENT_INFO]: 'warning',
  shared_module_inconsistent: 'warning',
  new_dynamic_sink_uncovered: 'error',
  coverage_regression: 'error',
});

async function readJson(file) {
  const text = await readFile(file, 'utf8');
  return JSON.parse(text);
}

/** 从目录加载一份构建快照（5 个清单文件）。 */
export async function loadSnapshot(dir, label = null) {
  const [entriesRaw, cspRaw, policiesRaw, sinksRaw, depsRaw] = await Promise.all([
    readJson(path.join(dir, 'entries.json')),
    readJson(path.join(dir, 'csp.json')),
    readJson(path.join(dir, 'policies.json')),
    readJson(path.join(dir, 'sinks.json')),
    readJson(path.join(dir, 'dynamic-deps.json')),
  ]);
  return normalizeSnapshot({
    label: label ?? path.basename(path.resolve(dir)),
    entries: entriesRaw.entries,
    csp: cspRaw.entries,
    policies: policiesRaw.policies,
    sinks: sinksRaw.sinks,
    dynamicDeps: depsRaw.deps,
  });
}

/** 校验并规范化快照对象，建立索引。 */
export function normalizeSnapshot({ label = 'snapshot', entries, csp, policies, sinks, dynamicDeps } = {}) {
  if (!Array.isArray(entries) || entries.length === 0) {
    throw new Error('entries.json: "entries" 必须是非空数组');
  }
  if (!Array.isArray(sinks)) {
    throw new Error('sinks.json: "sinks" 必须是数组');
  }
  const seenEntries = new Set();
  for (const e of entries) {
    if (!e || typeof e.name !== 'string' || e.name === '') {
      throw new Error('entries.json: 每个入口必须包含非空 name');
    }
    if (seenEntries.has(e.name)) throw new Error(`entries.json: 入口 "${e.name}" 重复`);
    seenEntries.add(e.name);
    e.loadPath = e.loadPath ?? `/${e.name}`;
    e.sharedModules = e.sharedModules ?? [];
    e.lazyModules = e.lazyModules ?? [];
  }
  const sinksById = new Map();
  const sinksByModule = new Map();
  for (const s of sinks) {
    if (!s || typeof s.id !== 'string' || s.id === '') {
      throw new Error('sinks.json: 每个写入点必须包含非空 id');
    }
    if (sinksById.has(s.id)) throw new Error(`sinks.json: 写入点 id "${s.id}" 重复`);
    sinksById.set(s.id, s);
    const mod = s.module ?? '(unknown)';
    if (!sinksByModule.has(mod)) sinksByModule.set(mod, []);
    sinksByModule.get(mod).push(s);
  }
  return {
    label,
    entries,
    csp: csp ?? {},
    policies: policies ?? {},
    sinks,
    sinksById,
    sinksByModule,
    dynamicDeps: dynamicDeps ?? {},
  };
}

/**
 * 解析入口实际加载的模块序列：
 * 共享模块直接加载；懒加载模块经 dynamic-deps 传递展开动态依赖。
 * 返回 [{ name, kind: 'shared'|'lazy'|'dynamic', chain: [...] }]。
 */
export function resolveEntryModules(entry, dynamicDeps) {
  const result = [];
  const seen = new Set();
  for (const m of entry.sharedModules) {
    if (seen.has(m)) continue;
    seen.add(m);
    result.push({ name: m, kind: 'shared', chain: [m] });
  }
  const visit = (mod, chain) => {
    if (seen.has(mod)) return; // 防环
    seen.add(mod);
    result.push({ name: mod, kind: chain.length > 1 ? 'dynamic' : 'lazy', chain });
    for (const dep of dynamicDeps[mod] ?? []) visit(dep, [...chain, dep]);
  };
  for (const m of entry.lazyModules) visit(m, [m]);
  return result;
}

/**
 * 判定单个写入点在指定入口下的覆盖状态。
 * 判定顺序：清单完整性 → CSP 记录存在性 → 仅报告模式 → 未强制 →
 *          未使用策略 → 策略名未授权 → 策略定义缺失 → 类型不符 → 覆盖。
 */
export function evaluateSink(sink, csp, policies, entryName) {
  const loc = sink.location ? `${sink.location.file}:${sink.location.line ?? '?'}` : '(unknown)';
  if (!sink.requiredType || !sink.sink || !sink.module) {
    return {
      status: STATUS.INSUFFICIENT_INFO,
      reason: `写入点 ${sink.id} 标注不完整（缺少 requiredType/sink/module），无法判定所需可信类型`,
      fixPoint: `sinks.json → sinks["${sink.id}"]（${loc}）: 补全 requiredType/sink/module 标注`,
    };
  }
  if (!csp) {
    return {
      status: STATUS.INSUFFICIENT_INFO,
      reason: `入口 "${entryName}" 缺少 CSP 策略记录，无法确认 Trusted Types 配置`,
      fixPoint: `csp.json → entries["${entryName}"]: 补充该入口的 CSP 策略记录`,
    };
  }
  if (csp.mode === 'report-only') {
    return {
      status: STATUS.REPORT_ONLY,
      reason: `入口 "${entryName}" 的 CSP 为 Report-Only，仅上报不拦截，写入点不受强制保护`,
      fixPoint: `csp.json → entries["${entryName}"].mode: "report-only" → "enforce"`,
    };
  }
  if (csp.requireTrustedTypesForScript !== true) {
    return {
      status: STATUS.NOT_ENFORCED,
      reason: `入口 "${entryName}" 未设置 require-trusted-types-for 'script'，DOM 写入不受约束`,
      fixPoint: `csp.json → entries["${entryName}"].requireTrustedTypesForScript: 设为 true（require-trusted-types-for 'script'）`,
    };
  }
  if (sink.actualPolicy == null) {
    return {
      status: STATUS.NO_POLICY,
      reason: `写入点 ${sink.id}（${loc}）未经由任何 Trusted Types 策略，直接写入 ${sink.sink}`,
      fixPoint: `源码 ${loc}: 将 ${sink.sink} 写入改为经由已获准的 Trusted Types 策略（创建 ${sink.requiredType}）`,
    };
  }
  const allowed = Array.isArray(csp.allowedPolicies) ? csp.allowedPolicies : [];
  if (!allowed.includes(sink.actualPolicy)) {
    return {
      status: STATUS.POLICY_NOT_AUTHORIZED,
      reason: `策略 "${sink.actualPolicy}" 未列入入口 "${entryName}" 的 trusted-types 指令白名单`,
      fixPoint: `csp.json → entries["${entryName}"].allowedPolicies: 在 trusted-types 指令中增加 "${sink.actualPolicy}"`,
    };
  }
  const policyDef = policies[sink.actualPolicy];
  if (!policyDef || !Array.isArray(policyDef.creates)) {
    return {
      status: STATUS.INSUFFICIENT_INFO,
      reason: `策略 "${sink.actualPolicy}" 缺少策略定义记录，无法确认其创建的可信类型`,
      fixPoint: `policies.json → policies["${sink.actualPolicy}"]: 补充策略定义及其 creates 列表`,
    };
  }
  if (!policyDef.creates.includes(sink.requiredType)) {
    return {
      status: STATUS.TYPE_MISMATCH,
      reason: `策略 "${sink.actualPolicy}" 创建 ${JSON.stringify(policyDef.creates)}，但写入点 ${sink.id} 需要 ${sink.requiredType}`,
      fixPoint: `policies.json → policies["${sink.actualPolicy}"].creates: 增加 "${sink.requiredType}"，或将 ${loc} 改用能创建该类型的已获准策略`,
    };
  }
  return { status: STATUS.COVERED, reason: '写入点由已获准策略覆盖，类型匹配', fixPoint: null };
}

function summarizeStatuses(records) {
  const counts = {};
  for (const r of records) counts[r.status] = (counts[r.status] ?? 0) + 1;
  return {
    total: records.length,
    covered: counts[STATUS.COVERED] ?? 0,
    uncovered: records.length - (counts[STATUS.COVERED] ?? 0),
    byStatus: counts,
  };
}

/** 分析单份快照：逐入口、逐模块、逐写入点判定覆盖状态。 */
export function analyzeSnapshot(snapshot) {
  const entries = {};
  const findings = [];
  for (const entry of snapshot.entries) {
    const modules = resolveEntryModules(entry, snapshot.dynamicDeps);
    const csp = snapshot.csp[entry.name] ?? null;
    const sinkResults = [];
    for (const m of modules) {
      for (const sink of snapshot.sinksByModule.get(m.name) ?? []) {
        const ev = evaluateSink(sink, csp, snapshot.policies, entry.name);
        const rec = {
          sinkId: sink.id,
          module: m.name,
          moduleKind: m.kind,
          loadPath: `${entry.loadPath} → ${m.chain.join(' → ')}`,
          sink: sink.sink ?? null,
          requiredType: sink.requiredType ?? null,
          actualPolicy: sink.actualPolicy ?? null,
          location: sink.location ?? null,
          status: ev.status,
          reason: ev.reason,
        };
        sinkResults.push(rec);
        if (ev.status !== STATUS.COVERED) {
          findings.push({
            id: `${ev.status}:${entry.name}:${m.name}:${sink.id}`,
            category: ev.status,
            severity: SEVERITY[ev.status],
            entries: [entry.name],
            loadPaths: [rec.loadPath],
            module: m.name,
            sinkId: sink.id,
            location: sink.location ?? null,
            detail: ev.reason,
            fixPoint: ev.fixPoint,
          });
        }
      }
    }
    entries[entry.name] = {
      loadPath: entry.loadPath,
      modules,
      sinks: sinkResults,
      summary: summarizeStatuses(sinkResults),
    };
  }
  const all = Object.values(entries).flatMap((e) => e.sinks);
  return { label: snapshot.label, entries, findings, summary: summarizeStatuses(all) };
}

/** 模块 → 入口 → { kinds, statuses } 矩阵。 */
function moduleEntryMatrix(analysis) {
  const matrix = new Map();
  for (const [entryName, entry] of Object.entries(analysis.entries)) {
    for (const m of entry.modules) {
      if (!matrix.has(m.name)) matrix.set(m.name, new Map());
      const cell = matrix.get(m.name).get(entryName) ?? { kinds: new Set(), statuses: new Set() };
      cell.kinds.add(m.kind);
      matrix.get(m.name).set(entryName, cell);
    }
    for (const s of entry.sinks) {
      const cell = matrix.get(s.module)?.get(entryName);
      if (cell) cell.statuses.add(s.status);
    }
  }
  return matrix;
}

function protectedByEntry(matrix, module) {
  const perEntry = matrix.get(module);
  if (!perEntry) return {};
  const out = {};
  for (const [entryName, cell] of perEntry) {
    out[entryName] = cell.statuses.size > 0 && [...cell.statuses].every((s) => s === STATUS.COVERED);
  }
  return out;
}

function isInconsistent(protectionMap) {
  const values = Object.values(protectionMap);
  return values.length > 1 && new Set(values).size > 1;
}

/**
 * 对照两版快照：
 * 1) 共享模块在不同入口的保护状态是否一致（A→B 是否收敛）；
 * 2) 动态模块新增写入点及其获准策略覆盖判定；
 * 3) 同一入口同一写入点由 covered 退化为未覆盖的回归。
 */
export function diffSnapshots(snapA, snapB, anaA, anaB) {
  const findings = [];
  const matrixA = moduleEntryMatrix(anaA);
  const matrixB = moduleEntryMatrix(anaB);

  const sharedIn = (snap) => {
    const set = new Set();
    for (const e of snap.entries) for (const m of e.sharedModules) set.add(m);
    return set;
  };
  const sharedModules = [...sharedIn(snapA)].filter((m) => sharedIn(snapB).has(m)).sort();

  const sharedModuleReports = [];
  for (const mod of sharedModules) {
    const protA = protectedByEntry(matrixA, mod);
    const protB = protectedByEntry(matrixB, mod);
    const inconsistentA = isInconsistent(protA);
    const inconsistentB = isInconsistent(protB);
    sharedModuleReports.push({
      module: mod,
      protectedByEntry: { [anaA.label]: protA, [anaB.label]: protB },
      inconsistentInA: inconsistentA,
      inconsistentInB: inconsistentB,
      resolved: inconsistentA && !inconsistentB,
    });
    if (inconsistentB) {
      const unprotected = Object.entries(protB).filter(([, ok]) => !ok).map(([e]) => e);
      findings.push({
        id: `shared_module_inconsistent:${mod}`,
        category: 'shared_module_inconsistent',
        severity: SEVERITY.shared_module_inconsistent,
        entries: unprotected,
        loadPaths: unprotected.map((e) => anaB.entries[e]?.loadPath ?? `/${e}`),
        module: mod,
        sinkId: null,
        location: null,
        detail: `共享模块 "${mod}" 在入口 ${JSON.stringify(protB)} 间保护状态不一致（快照 ${anaB.label}）`,
        fixPoint: `csp.json: 对齐各入口 trusted-types 指令与 require-trusted-types-for 设置，使共享模块 "${mod}" 在所有入口获得同等保护`,
      });
    }
  }

  // 动态模块（lazy/dynamic）集合（以当前快照 B 为准）
  const dynamicModulesB = new Set();
  for (const entry of Object.values(anaB.entries)) {
    for (const m of entry.modules) if (m.kind !== 'shared') dynamicModulesB.add(m.name);
  }
  const newDynamicSinks = [];
  for (const sink of snapB.sinks) {
    if (snapA.sinksById.has(sink.id)) continue;
    if (!dynamicModulesB.has(sink.module)) continue;
    const perEntry = [];
    const underlyingFixPoints = [];
    for (const [entryName, entry] of Object.entries(anaB.entries)) {
      const rec = entry.sinks.find((s) => s.sinkId === sink.id);
      if (!rec) continue;
      perEntry.push({ entry: entryName, status: rec.status, loadPath: rec.loadPath });
      if (rec.status !== STATUS.COVERED) {
        const f = anaB.findings.find((x) => x.sinkId === sink.id && x.entries.includes(entryName));
        if (f?.fixPoint) underlyingFixPoints.push(f.fixPoint);
      }
    }
    const covered = perEntry.length > 0 && perEntry.every((p) => p.status === STATUS.COVERED);
    newDynamicSinks.push({
      sinkId: sink.id,
      module: sink.module,
      location: sink.location ?? null,
      entries: perEntry,
      covered,
    });
    if (!covered) {
      findings.push({
        id: `new_dynamic_sink_uncovered:${sink.id}`,
        category: 'new_dynamic_sink_uncovered',
        severity: SEVERITY.new_dynamic_sink_uncovered,
        entries: perEntry.filter((p) => p.status !== STATUS.COVERED).map((p) => p.entry),
        loadPaths: perEntry.filter((p) => p.status !== STATUS.COVERED).map((p) => p.loadPath),
        module: sink.module,
        sinkId: sink.id,
        location: sink.location ?? null,
        detail: `动态模块 "${sink.module}" 在快照 ${anaB.label} 新增写入点 ${sink.id}，未完全被获准策略覆盖`,
        fixPoint: underlyingFixPoints.length > 0
          ? underlyingFixPoints.join('；')
          : `csp.json / policies.json: 为新增写入点 ${sink.id} 配置获准的 Trusted Types 策略`,
      });
    }
  }

  // 覆盖回归：同一入口同一写入点 A 覆盖、B 未覆盖
  const regressions = [];
  for (const [entryName, entryB] of Object.entries(anaB.entries)) {
    const entryA = anaA.entries[entryName];
    if (!entryA) continue;
    const statusA = new Map(entryA.sinks.map((s) => [s.sinkId, s.status]));
    for (const rec of entryB.sinks) {
      if (statusA.get(rec.sinkId) === STATUS.COVERED && rec.status !== STATUS.COVERED) {
        regressions.push({
          sinkId: rec.sinkId,
          module: rec.module,
          entry: entryName,
          statusBefore: STATUS.COVERED,
          statusAfter: rec.status,
          loadPath: rec.loadPath,
        });
        findings.push({
          id: `coverage_regression:${entryName}:${rec.sinkId}`,
          category: 'coverage_regression',
          severity: SEVERITY.coverage_regression,
          entries: [entryName],
          loadPaths: [rec.loadPath],
          module: rec.module,
          sinkId: rec.sinkId,
          location: rec.location,
          detail: `写入点 ${rec.sinkId} 在入口 "${entryName}" 由 covered 退化为 ${rec.status}`,
          fixPoint: `csp.json → entries["${entryName}"] 与 policies.json: 恢复对写入点 ${rec.sinkId} 的策略授权/类型匹配`,
        });
      }
    }
  }

  return {
    base: anaA.label,
    current: anaB.label,
    sharedModules: sharedModuleReports,
    newDynamicSinks,
    regressions,
    findings,
  };
}

/** 汇总 CI 可读的最终报告。 */
export function buildReport({ analysis, diff = null, baseLabel = null }) {
  const findings = [...analysis.findings, ...(diff?.findings ?? [])];
  const byCategory = {};
  for (const f of findings) byCategory[f.category] = (byCategory[f.category] ?? 0) + 1;
  return {
    tool: 'trusted-types-coverage-audit',
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    snapshots: { base: baseLabel, current: analysis.label },
    summary: {
      entries: Object.keys(analysis.entries).length,
      sinkEvaluations: analysis.summary.total,
      covered: analysis.summary.covered,
      uncovered: analysis.summary.uncovered,
      byStatus: analysis.summary.byStatus,
      findings: findings.length,
      byCategory,
    },
    entries: analysis.entries,
    diff,
    findings,
  };
}
