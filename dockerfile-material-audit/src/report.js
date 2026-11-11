/**
 * 人类可读的中文文本报告（结构化 JSON 仍由 CLI 的 --json 输出）。
 */

const STATUS_TEXT = {
  PINNED_APPROVED: '已固定且已审批',
  PINNED_UNAPPROVED: '摘要未获审批',
  MUTABLE_TAG_APPROVED: '可变标签·摘要未绑定',
  MUTABLE_TAG_OTHER_SOURCE: '可变标签·审批属于另一来源',
  MUTABLE_TAG_UNAPPROVED: '可变标签·无审批',
  PENDING: '待核实',
};

const DECISION_TEXT = {
  PASS: '通过（发布目标依赖均已绑定匹配的内容摘要）',
  FAIL: '不通过（存在未绑定/不匹配的外部材料）',
  INCONCLUSIVE: '待核实（存在无法解析的构建参数）',
};

export function formatTextReport(report) {
  const lines = [];
  lines.push('================ Dockerfile 外部材料审计报告 ================');
  lines.push(`发布目标阶段 : ${report.target}`);
  lines.push(`审计结论     : ${report.decision} — ${DECISION_TEXT[report.decision]}`);
  const s = report.summary;
  lines.push(
    `材料统计     : 外部材料 ${s.totalExternalMaterials} 份；参与门禁 ${s.gatingMaterials}；` +
      `违规 ${s.violations}；待核实 ${s.pending}；仅测试 ${s.testOnlyMaterials}；未使用阶段 ${s.unusedStageMaterials}`,
  );
  lines.push(
    `阶段可达性   : 可达 [${s.stages.reachable.join(', ')}]；` +
      `测试阶段 [${s.stages.testStages.join(', ') || '无'}]；` +
      `未使用 [${s.stages.unused.join(', ') || '无'}]`,
  );
  lines.push('');

  const render = (f) => {
    lines.push(`- 第 ${f.line} 行  ${f.instruction}`);
    lines.push(`  所属阶段   : ${f.ownerStage}${f.testStage ? '（测试阶段，仅供测试）' : ''}${f.reachable ? '' : '（未使用阶段）'}`);
    if (f.dependencyPath) {
      lines.push(`  依赖路径   : ${f.dependencyPath.join(' -> ')}`);
    }
    if (f.material) {
      const m = f.material;
      lines.push(`  材料身份   : ${m.source}${m.tag ? `:${m.tag}` : ''}${m.digest ? `@${m.digest}` : ''}`);
      lines.push(`  内容固定   : ${m.contentPinned ? '是（@digest）' : '否（可变标签）'}`);
    }
    if (f.applicableApproval) {
      const a = f.applicableApproval;
      lines.push(`  适用审批   : ${a.id || '(无编号)'} ${a.registry}/${a.repository}${a.tag ? `:${a.tag}` : ''}@${a.digest}`);
    } else {
      lines.push('  适用审批   : 无');
    }
    lines.push(`  判定       : ${f.status} — ${STATUS_TEXT[f.status]}`);
    lines.push(`  说明       : ${f.message}`);
    lines.push(`  参与门禁   : ${f.gating ? '是' : '否'}`);
    lines.push('');
  };

  const gating = report.findings.filter((f) => f.gating);
  const nonGating = report.findings.filter((f) => !f.gating);

  if (gating.length > 0) {
    lines.push('【参与门禁的外部材料（发布目标真正依赖）】');
    gating.forEach(render);
  }
  if (nonGating.length > 0) {
    lines.push('【不参与门禁的外部材料（测试阶段 / 未使用阶段）】');
    nonGating.forEach(render);
  }

  return lines.join('\n');
}
