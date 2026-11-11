/**
 * 文本/JSON 报告渲染。
 */

const STATUS_TEXT = {
  FEASIBLE: '可行（预算内）',
  OVER_BUDGET: '超预算',
  INFEASIBLE: '不可行（覆盖要求无法满足）',
};

const REASON_TEXT = {
  mandatory: '强制保留',
  coverage: '覆盖修复要求',
  dependency: '传递依赖带入',
};

function fmtMinutes(v) {
  return `${v} 分钟`;
}

/**
 * 渲染单个构建的人类可读报告。
 */
export function renderBuildResult(result) {
  const lines = [];
  lines.push(`构建 ${result.build} —— ${STATUS_TEXT[result.status]}（策略：${result.strategy === 'exhaustive' ? '穷举（已证明最优）' : '确定性贪心（含冗余剪枝）'}）`);
  lines.push(`  预算：${fmtMinutes(result.budgetMinutes)}；预计总耗时：${fmtMinutes(result.totalMinutes ?? 0)}` +
    (result.status === 'FEASIBLE' ? `；预算余量：${fmtMinutes(result.remainingBudgetMinutes)}` : ''));
  lines.push(`  强制根用例：${result.mandatoryRoots.length ? result.mandatoryRoots.join('、') : '（无）'}` +
    `；强制基线耗时：${fmtMinutes(result.baselineCost)}`);

  if (result.selectedCases.length > 0) {
    lines.push('  入选用例：');
    const reasonById = new Map(result.reasons.map((r) => [r.caseId, r]));
    for (const c of result.selectedCases) {
      const r = reasonById.get(c.id);
      lines.push(`    - ${c.id}（${fmtMinutes(c.durationMinutes)}）[${REASON_TEXT[r?.code] ?? r?.code}] ${r?.detail ?? ''}`);
    }
  }

  if (result.conflicts.length > 0) {
    lines.push('  冲突诊断：');
    for (const conflict of result.conflicts) {
      lines.push(`    ! [${conflict.code}] ${conflict.message}`);
    }
  }
  return lines.join('\n');
}

/**
 * 渲染整份报告（含全局环警告与汇总）。
 */
export function renderReport(selection, cycleWarnings) {
  const lines = [];
  lines.push('========== 安全修复用例选择报告 ==========');
  if (cycleWarnings.length > 0) {
    lines.push('依赖环警告（不阻断选择，环上用例闭包可正常计算）：');
    for (const w of cycleWarnings) {
      lines.push(`  ! 构建 ${w.build} 上检测到依赖环：${w.cases.join(' -> ')} -> ${w.cases[0]}`);
    }
    lines.push('');
  }

  for (const result of selection.builds) {
    lines.push(renderBuildResult(result));
    lines.push('');
  }

  const s = selection.summary;
  lines.push('---------- 汇总 ----------');
  lines.push(`构建总数：${selection.builds.length}；可行：${s.feasible}；超预算：${s.overBudget}；不可行：${s.infeasible}`);
  lines.push(`各构建预算合计：${fmtMinutes(selection.globalBudgetMinutes)}；入选用例预计耗时合计：${fmtMinutes(s.minutes)}`);
  return lines.join('\n');
}
