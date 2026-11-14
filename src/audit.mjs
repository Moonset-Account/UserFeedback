// 豁免决策核心：纯函数，不访问时间之外的任何环境。
// 三分类：
//   carried     —— 豁免有效，告警可沿用（退出待办）
//   reopen      —— 旧豁免不能再隐藏当前告警，应重新打开进入审核
//   manual      —— 豁免状态可疑/失效，需要人工处理
//
// 每条告警先按 (project_id, rule_id, location_id) 精确匹配豁免；
// 精确匹配缺失时再退回 (project_id, rule_id) 范围匹配，
// 用于暴露“旧豁免把新位置告警也隐藏”的场景。

export const DECISION = Object.freeze({
  CARRIED: 'carried',
  REOPEN: 'reopen',
  MANUAL: 'manual',
});

export const REASON = Object.freeze({
  EXEMPTION_VALID: 'exemption_valid',
  NO_MATCHING_EXEMPTION: 'no_matching_exemption',
  LOCATION_MISMATCH: 'location_mismatch',
  EVIDENCE_REVISION_CHANGED: 'evidence_revision_changed',
  EVIDENCE_WITHDRAWN: 'evidence_withdrawn',
  RULE_VERSION_CHANGED: 'rule_version_changed_unreconfirmed',
  EXEMPTION_EXPIRED: 'exemption_expired',
  EXEMPTION_NOT_YET_VALID: 'exemption_not_yet_valid',
  CONFLICTING_EXEMPTIONS: 'conflicting_exemptions',
});

const REASON_TEXT = {
  [REASON.EXEMPTION_VALID]: '豁免五要素全部匹配且在有效期内，告警可沿用',
  [REASON.NO_MATCHING_EXEMPTION]: '没有适用于该项目/规则/位置的豁免记录，告警应进入审核',
  [REASON.LOCATION_MISMATCH]:
    '漏洞类型相同但代码位置标识不同，旧豁免不得隐藏新位置的告警，应重新打开',
  [REASON.EVIDENCE_REVISION_CHANGED]:
    '告警对应的代码证据修订号已变化，旧证据上的豁免失效，应重新打开',
  [REASON.EVIDENCE_WITHDRAWN]:
    '审查所依据的证据已被撤回，豁免失去事实基础，需要人工处理',
  [REASON.RULE_VERSION_CHANGED]:
    '规则版本已变化且豁免未按新版本重新确认，需要人工处理并显式报告失效原因',
  [REASON.EXEMPTION_EXPIRED]: '豁免已过有效期且未续期，需要人工处理',
  [REASON.EXEMPTION_NOT_YET_VALID]: '豁免尚未到生效时间，记录时间线异常，需要人工处理',
  [REASON.CONFLICTING_EXEMPTIONS]: '同一精确范围存在多条互相冲突的有效豁免，需要人工处理',
};

function compareScope(exemption, alert) {
  return {
    sameProject: exemption.project_id === alert.project_id,
    sameRule: exemption.rule_id === alert.rule_id,
    sameLocation: exemption.location_id === alert.location_id,
  };
}

function describeExemption(ex) {
  return {
    exemption_id: ex.exemption_id,
    rule_version: ex.rule_version,
    location_id: ex.location_id,
    evidence_revision: ex.evidence_revision,
    valid_from: ex.valid_from,
    valid_until: ex.valid_until,
    reconfirmed_rule_version: ex.reconfirmed_rule_version,
  };
}

function baseResult(alert, reason, extra = {}) {
  return {
    alert_id: alert.alert_id,
    project_id: alert.project_id,
    rule_id: alert.rule_id,
    rule_version: alert.rule_version,
    location_id: alert.location_id,
    vuln_type: alert.vuln_type,
    evidence_revision: alert.evidence_revision,
    evidence_status: alert.evidence_status,
    decision: null,
    reason_code: reason,
    reason: REASON_TEXT[reason] ?? reason,
    ...extra,
  };
}

/**
 * 评估单条告警。
 * @param {object} alert 已校验告警
 * @param {object[]} exemptions 已校验豁免
 * @param {number} auditTimeMs 核验时间（毫秒）
 */
export function evaluateAlert(alert, exemptions, auditTimeMs) {
  const sameProjectRule = exemptions.filter(
    (ex) => ex.project_id === alert.project_id && ex.rule_id === alert.rule_id,
  );

  if (sameProjectRule.length === 0) {
    return baseResult(alert, REASON.NO_MATCHING_EXEMPTION, {
      decision: DECISION.REOPEN,
      matched_exemptions: [],
    });
  }

  const exact = sameProjectRule.filter((ex) => ex.location_id === alert.location_id);

  if (exact.length === 0) {
    // 同项目同规则但位置不同：旧豁免覆盖的是旧位置，不能隐藏新位置告警。
    const candidate = sameProjectRule[0];
    const { sameProject, sameRule, sameLocation } = compareScope(candidate, alert);
    return baseResult(alert, REASON.LOCATION_MISMATCH, {
      decision: DECISION.REOPEN,
      matched_exemptions: sameProjectRule.map(describeExemption),
      scope_check: { same_project: sameProject, same_rule: sameRule, same_location: sameLocation },
    });
  }

  if (exact.length > 1) {
    return baseResult(alert, REASON.CONFLICTING_EXEMPTIONS, {
      decision: DECISION.MANUAL,
      matched_exemptions: exact.map(describeExemption),
    });
  }

  const exemption = exact[0];
  const matched = describeExemption(exemption);

  // 检查顺序固定，保证同一输入决策稳定：
  // 1 有效期窗口（边界含端点：核验时间恰好等于到期时间仍有效）
  // 2 证据撤回（人工）
  // 3 规则版本且未重新确认（人工，显式失效原因）
  // 4 证据修订号变化（重新打开）
  // 5 全部匹配（沿用）
  if (auditTimeMs < exemption.valid_from_ms) {
    return baseResult(alert, REASON.EXEMPTION_NOT_YET_VALID, {
      decision: DECISION.MANUAL,
      matched_exemptions: [matched],
      time_check: { audit_before_valid_from: true },
    });
  }
  if (auditTimeMs > exemption.valid_until_ms) {
    return baseResult(alert, REASON.EXEMPTION_EXPIRED, {
      decision: DECISION.MANUAL,
      matched_exemptions: [matched],
      time_check: { audit_after_valid_until: true },
    });
  }

  if (alert.evidence_status === 'withdrawn') {
    return baseResult(alert, REASON.EVIDENCE_WITHDRAWN, {
      decision: DECISION.MANUAL,
      matched_exemptions: [matched],
      evidence_check: { status: 'withdrawn' },
    });
  }

  if (exemption.rule_version !== alert.rule_version) {
    const reconfirmed =
      exemption.reconfirmed_rule_version !== null &&
      exemption.reconfirmed_rule_version === alert.rule_version;
    if (!reconfirmed) {
      return baseResult(alert, REASON.RULE_VERSION_CHANGED, {
        decision: DECISION.MANUAL,
        matched_exemptions: [matched],
        rule_version_check: {
          exemption_rule_version: exemption.rule_version,
          alert_rule_version: alert.rule_version,
          reconfirmed_rule_version: exemption.reconfirmed_rule_version,
          reconfirmed: false,
        },
      });
    }
  }

  if (exemption.evidence_revision !== alert.evidence_revision) {
    return baseResult(alert, REASON.EVIDENCE_REVISION_CHANGED, {
      decision: DECISION.REOPEN,
      matched_exemptions: [matched],
      evidence_check: {
        exemption_evidence_revision: exemption.evidence_revision,
        alert_evidence_revision: alert.evidence_revision,
      },
    });
  }

  const onBoundary = auditTimeMs === exemption.valid_until_ms;
  return baseResult(alert, REASON.EXEMPTION_VALID, {
    decision: DECISION.CARRIED,
    matched_exemptions: [matched],
    time_check: { at_valid_until_boundary: onBoundary },
  });
}

/**
 * 生成完整审计报告。排序键固定，保证同一输入输出字节级稳定。
 */
export function buildReport(alerts, exemptions, auditTimeMs, auditTimeInput) {
  const results = alerts.map((alert) => evaluateAlert(alert, exemptions, auditTimeMs));
  results.sort((a, b) => (a.alert_id < b.alert_id ? -1 : a.alert_id > b.alert_id ? 1 : 0));

  const summary = {
    total: results.length,
    carried: results.filter((r) => r.decision === DECISION.CARRIED).length,
    reopen: results.filter((r) => r.decision === DECISION.REOPEN).length,
    manual: results.filter((r) => r.decision === DECISION.MANUAL).length,
  };

  return {
    report_schema: 'exemption-audit/v1',
    generated_at: auditTimeInput,
    generated_at_ms: auditTimeMs,
    summary,
    results: {
      carried: results.filter((r) => r.decision === DECISION.CARRIED),
      reopen: results.filter((r) => r.decision === DECISION.REOPEN),
      manual: results.filter((r) => r.decision === DECISION.MANUAL),
    },
    results_flat: results,
  };
}
