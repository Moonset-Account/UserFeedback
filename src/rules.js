// 规范化规则与注册表优先级的选择、冲突识别。
//
// 冲突规范化规则必须“单独识别”：同一生态存在多套规则集时，每套都要实际跑一遍，
// 并标记结论是否随规则集变化（ruleSensitive）。单套规则内部若同时出现
// keep/drop 两种作用域步骤，也作为冲突诊断上报，执行时以首个 scope 步骤为准。

/**
 * @param {Object} normalizationConfig { [ecosystem]: [ ruleSet, ... ] | ruleSet }
 * @returns {Map<ecosystem, { ruleSets, conflicts, diagnostics }>}
 */
export function prepareEcosystemRules(normalizationConfig) {
  const out = new Map();
  for (const [ecosystem, value] of Object.entries(normalizationConfig || {})) {
    const ruleSets = Array.isArray(value) ? value : [value];
    const diagnostics = [];

    if (ruleSets.length > 1) {
      diagnostics.push({
        code: 'CONFLICTING_NORMALIZATION_RULES',
        ecosystem,
        ruleSetIds: ruleSets.map((r) => r.id),
        message: `生态 ${ecosystem} 配置了多套相互冲突的规范化规则，将分别求值并比较结论`,
      });
    }

    for (const rs of ruleSets) {
      const scopeModes = new Set();
      for (const step of rs.steps || []) {
        if (step && step.op === 'scope') scopeModes.add(step.mode);
        if (step && !['scope', 'case-fold', 'strip-separators'].includes(step.op)) {
          diagnostics.push({
            code: 'UNKNOWN_NORMALIZATION_OP',
            ecosystem,
            ruleSetId: rs.id,
            op: step.op,
            message: `规则集 ${rs.id} 含未知操作 "${step.op}"，执行时跳过`,
          });
        }
      }
      if (scopeModes.size > 1) {
        diagnostics.push({
          code: 'CONTRADICTORY_SCOPE_STEPS',
          ecosystem,
          ruleSetId: rs.id,
          modes: [...scopeModes],
          message: `规则集 ${rs.id} 同时要求保留与删除作用域，执行时以首个 scope 步骤为准`,
        });
      }
    }

    out.set(ecosystem, { ruleSets, conflicts: diagnostics, diagnostics });
  }
  return out;
}

/**
 * 校验注册表优先级中引用的注册表是否存在、生态是否匹配。
 */
export function preparePriority(priorityConfig, registries) {
  const byId = new Map((registries || []).map((r) => [r.id, r]));
  const result = new Map();
  const diagnostics = [];

  for (const [ecosystem, order] of Object.entries(priorityConfig || {})) {
    const chain = [];
    for (const registryId of order || []) {
      const reg = byId.get(registryId);
      if (!reg) {
        diagnostics.push({
          code: 'UNKNOWN_REGISTRY_REFERENCE',
          ecosystem,
          registryId,
          message: `生态 ${ecosystem} 的优先级引用了不存在的注册表 ${registryId}，已跳过`,
        });
        continue;
      }
      if (reg.ecosystem !== ecosystem) {
        diagnostics.push({
          code: 'REGISTRY_ECOSYSTEM_MISMATCH',
          ecosystem,
          registryId,
          registryEcosystem: reg.ecosystem,
          message: `注册表 ${registryId} 属于生态 ${reg.ecosystem}，不能参与 ${ecosystem} 的解析顺序`,
        });
        continue;
      }
      chain.push(registryId);
    }
    result.set(ecosystem, chain);
  }
  return { chainByEcosystem: result, diagnostics };
}
