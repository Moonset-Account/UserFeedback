// 依赖评审核心：
// 1. 按生态的注册表优先级，模拟每条声明实际会选中的注册表与包（解析路径）；
// 2. 在每个候选规则集下分别做规范化与混淆骨架比较；
// 3. 只有“选中了外部同名 / 混淆名”才给出 block，其余相似情况给 hint；
// 4. 作用域、生态、注册表都是身份的一部分，索引与匹配始终带这些维度，不按显示名跨身份合并。

import { indexDirectory } from './directory.js';
import { prepareEcosystemRules, preparePriority } from './rules.js';
import { normalizeAndSkeleton, diffConfusables, loadConfusableMap } from './skeleton.js';
import { parseSpec } from './normalization.js';

const IDENTITY_RULE_SET = { id: 'identity-fallback', steps: [] };

function cpHex(ch) {
  return `U+${ch.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}`;
}

function toAbsolutePositions(hits, nameStart) {
  return hits.map((h) => ({
    ...h,
    absoluteIndex: h.position + nameStart,
    declarationCodePoint: cpHex(h.declarationChar),
    internalCodePoint: cpHex(h.internalChar),
  }));
}

/**
 * 主入口。
 *
 * input:
 * {
 *   directory: [{ ecosystem, name, path? }],
 *   declarations: [{ id?, ecosystem, spec }],
 *   normalization: { [ecosystem]: ruleSet | [ruleSet...] },
 *   registryPriority: { [ecosystem]: [registryId, ...] },
 *   registries: [{ id, ecosystem, external: boolean, packages: [string,...] }],
 *   confusables: { "а": "a", ... }
 * }
 */
export function reviewDependencies(input = {}) {
  const confusableMap = loadConfusableMap(input.confusables);
  const { grouped, diagnostics: dirDiagnostics } = indexDirectory(input.directory);
  const ruleInfo = prepareEcosystemRules(input.normalization);
  const { chainByEcosystem, diagnostics: priorityDiagnostics } = preparePriority(
    input.registryPriority,
    input.registries,
  );

  const registriesById = new Map((input.registries || []).map((r) => [r.id, r]));
  const diagnostics = [...dirDiagnostics, ...priorityDiagnostics];
  for (const { conflicts } of ruleInfo.values()) diagnostics.push(...conflicts);

  // 规则集 / 注册表包的骨架缓存，按 生态+规则集 维度，避免重复计算。
  const internalSkelCache = new Map();
  const registrySkelCache = new Map();

  // 身份键：
  //  - 保留作用域的规则集：作用域字符串本身就是身份，不同作用域 / 有无作用域绝不相等；
  //  - 删除作用域的规则集：作用域被规范化步骤显式移除，所有名称统一落到无作用域身份槽，
  //    此时是否同名完全交由规范化串/骨架判断（这正是 C04 要验证的规则次序效应）。
  function identityKey(specName, ecosystem, scopeKept) {
    const { scope } = parseSpec(specName);
    if (scopeKept) return scope === null ? `\u0000unscoped:${ecosystem}` : `scope:${scope}`;
    return `\u0000dropped:${ecosystem}`;
  }

  function getInternalSkel(ecosystem, ruleSet) {
    const key = `${ecosystem}\u0000${ruleSet.id}`;
    if (internalSkelCache.has(key)) return internalSkelCache.get(key);
    const entries = grouped.get(ecosystem) || [];
    const rows = entries.map((entry) => {
      const skel = normalizeAndSkeleton(entry.name, ruleSet, confusableMap);
      return {
        entry,
        ...skel,
        scopeKey: identityKey(entry.name, ecosystem, skel.scopeKept),
        rawName: entry.name,
      };
    });
    internalSkelCache.set(key, rows);
    return rows;
  }

  function getRegistrySkel(registryId, ruleSet) {
    const key = `${registryId}\u0000${ruleSet.id}`;
    if (registrySkelCache.has(key)) return registrySkelCache.get(key);
    const reg = registriesById.get(registryId);
    const rows = (reg?.packages || []).map((pkg) => {
      const skel = normalizeAndSkeleton(pkg, ruleSet, confusableMap);
      return {
        name: pkg,
        ...skel,
        scopeKey: identityKey(pkg, reg.ecosystem, skel.scopeKept),
      };
    });
    registrySkelCache.set(key, rows);
    return rows;
  }

  function matchInternals(declSpec, declSkel, ecosystem, ruleSet) {
    const rows = getInternalSkel(ecosystem, ruleSet);
    const declScopeKey = identityKey(declSpec, ecosystem, declSkel.scopeKept);
    const exact = [];
    const skeleton = [];
    const normalizedSimilar = [];
    for (const row of rows) {
      // 作用域是身份的一部分：身份键不同（不同作用域 / 有无作用域）禁止任何匹配。
      if (row.scopeKey !== declScopeKey) continue;
      if (row.rawName === declSpec) {
        // 阻断级“同名”只认原始显示名完全一致；分隔符差异不构成同名。
        exact.push(row);
      } else if (row.normalized === declSkel.normalized) {
        // 仅规范化后相似（如 payroll-sdk vs payrollsdk）：保留提示，绝不阻断。
        // 注意：规范化名相等时骨架必然相等，必须在骨架分支之前排除，避免误判为混淆。
        normalizedSimilar.push(row);
      } else if (row.skeleton === declSkel.skeleton) {
        skeleton.push(row);
      }
    }
    return { exact, skeleton, normalizedSimilar };
  }

  // 解析模拟必须在具体规则集语境下进行：
  // 先按原始显示名命中（最常见的路径），再按该规则集产出的规范名命中
  // （例如 drop-scope 后 @acme/metrics-lib 与无作用域 metrics-lib 可被同一注册表解析）。
  function resolveRegistry(ecosystem, spec, ruleSet, declSkel) {
    const chain = chainByEcosystem.get(ecosystem) || [];
    const checked = [];
    for (const registryId of chain) {
      const reg = registriesById.get(registryId);
      const rows = getRegistrySkel(registryId, ruleSet);
      const rawHit = rows.find((r) => r.name === spec);
      const normHit = !rawHit
        ? rows.find((r) => r.scopeKey === identityKey(spec, ecosystem, declSkel.scopeKept)
          && r.normalized === declSkel.normalized)
        : null;
      const hit = rawHit || normHit;
      checked.push({
        registryId,
        external: !!reg?.external,
        found: !!hit,
        matchedBy: rawHit ? 'exact-display-name' : normHit ? 'normalized-name' : null,
        matchedPackage: hit ? hit.name : null,
      });
      if (hit) {
        return {
          selected: true,
          registryId,
          external: !!reg?.external,
          chain,
          checked,
          matchedPackage: hit.name,
          matchedBy: rawHit ? 'exact-display-name' : 'normalized-name',
        };
      }
    }
    return { selected: false, registryId: null, external: false, chain, checked };
  }

  function scanExternalLookalikes(ecosystem, ruleSet, declSpec, declSkel, excludeRegistryId) {
    const chain = chainByEcosystem.get(ecosystem) || [];
    const declScopeKey = identityKey(declSpec, ecosystem, declSkel.scopeKept);
    const out = [];
    for (const registryId of chain) {
      if (registryId === excludeRegistryId) continue;
      for (const row of getRegistrySkel(registryId, ruleSet)) {
        if (row.scopeKey !== declScopeKey) continue;
        if (row.name === declSpec) {
          out.push({ registryId, name: row.name, matchType: 'exact-display-name' });
        } else if (row.normalized === declSkel.normalized) {
          // 规范化名相等时骨架必相等，先归入相似项，避免误报混淆。
          out.push({ registryId, name: row.name, matchType: 'same-normalized-name' });
        } else if (row.skeleton === declSkel.skeleton) {
          out.push({
            registryId,
            name: row.name,
            matchType: 'confusable-skeleton',
            confusablePositions: toAbsolutePositions(
              diffConfusables(declSkel, row),
              parseSpec(declSpec).nameStart,
            ),
          });
        }
      }
    }
    return out;
  }

  const findings = [];
  const declarations = input.declarations || [];

  declarations.forEach((rawDecl, declIdx) => {
    const declarationId = rawDecl.id || `decl-${declIdx + 1}`;
    const ecosystem = String(rawDecl.ecosystem);
    const spec = String(rawDecl.spec);
    const { nameStart } = parseSpec(spec);

    let ruleSets = ruleInfo.get(ecosystem)?.ruleSets;
    if (!ruleSets || ruleSets.length === 0) {
      diagnostics.push({
        code: 'MISSING_NORMALIZATION_RULES',
        ecosystem,
        declarationId,
        message: `生态 ${ecosystem} 没有规范化规则，声明 ${declarationId} 使用恒等回退规则`,
      });
      ruleSets = [IDENTITY_RULE_SET];
    }
    if (!chainByEcosystem.has(ecosystem)) {
      diagnostics.push({
        code: 'MISSING_REGISTRY_PRIORITY',
        ecosystem,
        declarationId,
        message: `生态 ${ecosystem} 未配置注册表优先级，声明 ${declarationId} 无法解析`,
      });
    }

    for (const ruleSet of ruleSets) {
      const declSkel = normalizeAndSkeleton(spec, ruleSet, confusableMap);
      declSkel.spec = spec;
      const resolution = resolveRegistry(ecosystem, spec, ruleSet, declSkel);
      const { exact, skeleton, normalizedSimilar } = matchInternals(spec, declSkel, ecosystem, ruleSet);
      const internalExact = exact[0] || null;
      const internalSkeleton = skeleton[0] || null;
      const internalSimilar = normalizedSimilar[0] || null;
      const shadowed = scanExternalLookalikes(
        ecosystem,
        ruleSet,
        spec,
        declSkel,
        resolution.registryId,
      );

      const base = {
        id: `${declarationId}:${ruleSet.id}`,
        declarationId,
        ecosystem,
        spec,
        ruleSetId: ruleSet.id,
        resolutionPath: {
          registryChain: resolution.chain,
          checkedRegistries: resolution.checked,
          selectedRegistry: resolution.registryId,
          selectedExternal: resolution.external,
        },
      };

      if (resolution.selected && resolution.external) {
        // 阻断仅在“外部注册表被实际选中”且与内部包同名（显示名一致）或混淆骨架相同时成立。
        if (internalExact) {
          findings.push({
            ...base,
            severity: 'block',
            kind: 'EXTERNAL_SAME_NAME',
            internalCandidate: {
              name: internalExact.entry.name,
              normalized: internalExact.normalized,
              matchType: 'exact-display-name',
            },
            externalCandidate: {
              registryId: resolution.registryId,
              name: spec,
              matchType: 'exact-display-name',
            },
            confusablePositions: [],
            shadowedExternal: shadowed,
            message: `声明 "${spec}" 按解析顺序优先命中外部注册表 ${resolution.registryId}，与内部包 "${internalExact.entry.name}" 显示名完全相同`,
          });
        } else if (internalSkeleton) {
          const hits = toAbsolutePositions(
            diffConfusables(declSkel, internalSkeleton),
            nameStart,
          );
          findings.push({
            ...base,
            severity: 'block',
            kind: 'EXTERNAL_CONFUSABLE',
            internalCandidate: {
              name: internalSkeleton.entry.name,
              normalized: internalSkeleton.normalized,
              matchType: 'confusable-skeleton',
            },
            externalCandidate: {
              registryId: resolution.registryId,
              name: spec,
              matchType: 'confusable-skeleton',
            },
            confusablePositions: hits,
            shadowedExternal: shadowed,
            message: `声明 "${spec}" 命中外部注册表 ${resolution.registryId}，其混淆骨架与内部包 "${internalSkeleton.entry.name}" 相同`,
          });
        } else if (internalSimilar) {
          // 只是规范化后相似（分隔符/大小写差异），不构成可错误解析的同名路径 -> 提示。
          findings.push({
            ...base,
            severity: 'hint',
            kind: 'SIMILAR_NORMALIZED_NAME',
            internalCandidate: {
              name: internalSimilar.entry.name,
              normalized: internalSimilar.normalized,
              matchType: 'same-normalized-name',
            },
            externalCandidate: {
              registryId: resolution.registryId,
              name: spec,
              matchType: 'same-normalized-name',
            },
            confusablePositions: [],
            shadowedExternal: shadowed,
            message: `声明 "${spec}" 与内部包 "${internalSimilar.entry.name}" 仅规范化名相近（显示名不同），保留提示而非阻断`,
          });
        }
        // 外部选中且无任何内部候选：正常外部依赖，不产生风险项。
        continue;
      }

      if (resolution.selected && !resolution.external) {
        // 选中内部注册表：本身安全；若链条上的外部注册表存在同名/混淆影子包，保留提示。
        if (shadowed.length > 0) {
          findings.push({
            ...base,
            severity: 'hint',
            kind: 'SHADOWED_EXTERNAL_LOOKALIKE',
            internalCandidate: internalExact
              ? { name: internalExact.entry.name, normalized: internalExact.normalized, matchType: 'exact-display-name' }
              : internalSkeleton
                ? { name: internalSkeleton.entry.name, normalized: internalSkeleton.normalized, matchType: 'confusable-skeleton' }
                : internalSimilar
                  ? { name: internalSimilar.entry.name, normalized: internalSimilar.normalized, matchType: 'same-normalized-name' }
                  : null,
            externalCandidate: null,
            confusablePositions: internalSkeleton
              ? toAbsolutePositions(diffConfusables(declSkel, internalSkeleton), nameStart)
              : shadowed.flatMap((s) => s.confusablePositions || []),
            shadowedExternal: shadowed,
            message: `声明 "${spec}" 当前由内部注册表 ${resolution.registryId} 满足，但解析链上的外部注册表存在同名/混淆影子包，优先级一旦变化即可能被错误解析`,
          });
        } else if (!internalExact && internalSkeleton) {
          findings.push({
            ...base,
            severity: 'hint',
            kind: 'INTERNAL_CONFUSABLE_NAME',
            internalCandidate: {
              name: internalSkeleton.entry.name,
              normalized: internalSkeleton.normalized,
              matchType: 'confusable-skeleton',
            },
            externalCandidate: null,
            confusablePositions: toAbsolutePositions(
              diffConfusables(declSkel, internalSkeleton),
              nameStart,
            ),
            shadowedExternal: [],
            message: `声明 "${spec}" 与内部包 "${internalSkeleton.entry.name}" 仅混淆骨架相同，建议核对字符`,
          });
        }
        continue;
      }

      // 未被任何注册表选中：若内部目录存在候选，保留提示（潜在错误声明/未来劫持面）。
      if (internalExact || internalSkeleton || internalSimilar) {
        const candidate = internalExact || internalSkeleton || internalSimilar;
        const matchType = internalExact
          ? 'exact-display-name'
          : internalSkeleton
            ? 'confusable-skeleton'
            : 'same-normalized-name';
        findings.push({
          ...base,
          severity: 'hint',
          kind: 'UNRESOLVED_WITH_INTERNAL_CANDIDATE',
          internalCandidate: {
            name: candidate.entry.name,
            normalized: candidate.normalized,
            matchType,
          },
          externalCandidate: null,
          confusablePositions: internalSkeleton
            ? toAbsolutePositions(diffConfusables(declSkel, internalSkeleton), nameStart)
            : [],
          shadowedExternal: shadowed,
          message: `声明 "${spec}" 在当前注册表链上无法解析，但内部目录存在${internalExact ? '同名' : internalSkeleton ? '混淆骨架相同的' : '规范名相近的'}候选 "${candidate.entry.name}"`,
        });
      } else {
        diagnostics.push({
          code: 'UNRESOLVED_DECLARATION',
          ecosystem,
          declarationId,
          spec,
          ruleSetId: ruleSet.id,
          message: `声明 "${spec}" 在生态 ${ecosystem} 的注册表链上无任何匹配，且内部目录无候选`,
        });
      }
    }
  });

  // 规则敏感性：同一声明在多套规则集下 block/hint/无 结论是否变化。
  const byDecl = new Map();
  for (const f of findings) {
    if (!byDecl.has(f.declarationId)) byDecl.set(f.declarationId, []);
    byDecl.get(f.declarationId).push(f);
  }
  declarations.forEach((rawDecl, declIdx) => {
    const declarationId = rawDecl.id || `decl-${declIdx + 1}`;
    const group = byDecl.get(declarationId) || [];
    // 该生态下实际评估过的全部规则集（未产生 finding 的记为 none）。
    const evaluatedRuleSets = (ruleInfo.get(String(rawDecl.ecosystem))?.ruleSets
      || [IDENTITY_RULE_SET]).map((r) => r.id);
    const perRuleSet = evaluatedRuleSets.map((id) => {
      const f = group.find((g) => g.ruleSetId === id);
      return { ruleSetId: id, severity: f ? f.severity : 'none', kind: f ? f.kind : null };
    });
    const severities = new Set(perRuleSet.map((p) => p.severity));
    const ruleSensitive = evaluatedRuleSets.length > 1 && severities.size > 1;
    for (const f of group) {
      f.ruleSensitive = ruleSensitive;
      f.ruleSetComparison = perRuleSet;
    }
  });

  const summary = {
    declarations: declarations.length,
    blocks: findings.filter((f) => f.severity === 'block').length,
    hints: findings.filter((f) => f.severity === 'hint').length,
    diagnostics: diagnostics.length,
    duplicateDirectoryEntries: dirDiagnostics.length,
    conflictingRuleSets: diagnostics.filter((d) => d.code === 'CONFLICTING_NORMALIZATION_RULES').length,
  };

  return { findings, diagnostics, summary };
}
