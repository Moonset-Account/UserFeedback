#include "report.hpp"

#include <iomanip>
#include <sstream>

#include "json.hpp"

namespace memtriage {

namespace {

std::string fmtInt(std::int64_t v) {
    std::ostringstream ss;
    ss << v;
    return ss.str();
}

std::string fmtFixed(double v, int precision) {
    std::ostringstream ss;
    ss << std::fixed << std::setprecision(precision) << v;
    return ss.str();
}

std::string fmtCoverage(const Assessment& a) {
    if (!a.hasCoverage) return "N/A(E=0)";
    return fmtFixed(a.mitigationCoverage * 100.0, 1) + "%";
}

std::string q(const std::string& s) {
    return "\"" + jsonEscape(s) + "\"";
}

std::string bandLabelZh(const std::string& band) {
    if (band == "high") return "高";
    if (band == "medium") return "中";
    return "低";
}

void appendJsonRelease(std::ostringstream& ss, const ReleaseAssessment& ra,
                       const std::string& indent) {
    ss << indent << "{\n";
    ss << indent << "  \"release_id\": " << q(ra.releaseId) << ",\n";
    ss << indent << "  \"lifecycle\": " << q(toString(ra.lifecycle)) << ",\n";
    ss << indent << "  \"lifecycle_zh\": " << q(lifecycleLabelZh(ra.lifecycle)) << ",\n";
    ss << indent << "  \"deployment_known\": " << (ra.deploymentKnown ? "true" : "false") << ",\n";
    ss << indent << "  \"deploy_count\": "
       << (ra.deploymentKnown ? fmtInt(ra.deployCount) : "null") << ",\n";
    ss << indent << "  \"has_mitigation_record\": "
       << (ra.hasMitigationRecord ? "true" : "false") << ",\n";
    ss << indent << "  \"mitigation_id\": "
       << (ra.hasMitigationRecord ? q(ra.bestMitigationId) : "null") << ",\n";
    ss << indent << "  \"verification_status\": "
       << (ra.hasMitigationRecord ? q(toString(ra.bestStatus)) : "null") << ",\n";
    ss << indent << "  \"counts_toward_coverage\": "
       << (ra.coveredByEffectiveMitigation ? "true" : "false") << "\n";
    ss << indent << "}";
}

void appendJsonChecklist(std::ostringstream& ss, const ChecklistItem& item,
                         const std::string& indent) {
    ss << indent << "{\n";
    ss << indent << "  \"category\": " << q(item.category) << ",\n";
    ss << indent << "  \"release_id\": " << (item.releaseId.empty() ? "null" : q(item.releaseId)) << ",\n";
    ss << indent << "  \"action\": " << q(item.action) << "\n";
    ss << indent << "}";
}

}  // namespace

std::string renderTextReport(const std::vector<Assessment>& results,
                             const ReportOptions& opts,
                             const ComparisonReport* comparison) {
    std::ostringstream out;
    const ScoringConfig& cfg = defaultConfig();
    out << "================================================================================\n";
    out << "内存缺陷修复分诊报告\n";
    out << "数据集: " << (opts.inputLabel.empty() ? std::string("(未命名)") : opts.inputLabel) << "\n";
    out << "================================================================================\n\n";

    out << "一、评分与排序规则（确定性全序，任何输入顺序下输出唯一）\n";
    out << "--------------------------------------------------------------------------------\n";
    out << "1. 受影响范围 E：缺陷影响的「活跃」与「生命周期未知」发行版中，部署数已知的部署总量；\n";
    out << "   退役发行版不计入当前暴露面（单独列出）；部署数为 null 时不得按 0 处理，单列未知项。\n";
    out << "2. 缓解覆盖 M：仅 verification_status=verified_effective 的发行版，其部署数计入 M；\n";
    out << "   ineffective / inconclusive / pending / disputed 以及无记录的发行版一律不计入。\n";
    out << "3. 未缓解部署 U = E - M；缓解覆盖率 = M / E（E=0 时为 N/A）。\n";
    out << "4. 业务中断权重 W：关键进程中止=4，业务中断=3，服务降级=2，轻微降级=1。\n";
    out << "5. 风险分 R = W × U（整数语义，告警次数不参与风险分，避免退役版本高频告警误导）。\n";
    out << "6. 证据置信度：起始 1.0，逐项扣分后截断到 [0,1]：\n";
    out << "   - 每个部署范围未知的在保发行版 -" << fmtFixed(cfg.penaltyPerUnknownDeployment, 2) << "\n";
    out << "   - 每个生命周期未知的在保发行版 -" << fmtFixed(cfg.penaltyPerUnknownLifecycle, 2) << "\n";
    out << "   - 每个缺少已核实有效缓解的在保发行版 -" << fmtFixed(cfg.penaltyPerOpenMitigation, 2) << "\n";
    out << "   - 影响证据未经确认 -" << fmtFixed(cfg.penaltyUnconfirmedEvidence, 2) << "\n";
    out << "   分档：>= " << fmtFixed(cfg.confidenceHighThreshold, 2) << " 高；>= "
        << fmtFixed(cfg.confidenceMediumThreshold, 2) << " 中；其余为低。\n";
    out << "7. 排序键（依次比较，全部相同则顺序固定）：\n";
    out << "   (1) 风险分 R 降序 -> (2) 中断权重 W 降序 -> (3) 未缓解部署 U 降序 ->\n";
    out << "   (4) 置信度升序（证据弱者优先核实）-> (5) 告警次数降序 -> (6) 缺陷编号字典序升序。\n\n";

    out << "二、修复顺序总表\n";
    out << "--------------------------------------------------------------------------------\n";
    out << "排名 缺陷编号     风险分R  权重W  未缓解U  暴露E  覆盖M  覆盖率    置信度(分档)  告警数\n";
    int rank = 0;
    for (const auto& a : results) {
        ++rank;
        std::ostringstream line;
        line << std::setw(4) << rank << ' '
             << std::left << std::setw(12) << a.defect.defectId
             << std::right << std::setw(7) << fmtFixed(a.riskScore, 1)
             << std::setw(7) << fmtFixed(a.impactWeight, 0)
             << std::setw(8) << a.unmitigatedDeployments
             << std::setw(7) << a.exposedDeployments
             << std::setw(7) << a.coveredDeployments
             << std::setw(10) << fmtCoverage(a)
             << std::setw(8) << fmtFixed(a.confidence, 2) << '('
             << bandLabelZh(a.confidenceBand) << ')'
             << std::setw(7) << a.defect.alertCount << '\n';
        out << line.str();
    }
    out << '\n';

    out << "三、逐缺陷分诊明细\n";
    out << "--------------------------------------------------------------------------------\n";
    rank = 0;
    for (const auto& a : results) {
        ++rank;
        out << '[' << rank << "] " << a.defect.defectId << ' '
            << (a.defect.title.empty() ? std::string() : a.defect.title) << '\n';
        out << "    中断等级: " << impactLabelZh(a.defect.impactLevel)
            << "（W=" << fmtFixed(a.impactWeight, 0)
            << "）  告警次数: " << a.defect.alertCount
            << "  影响证据: " << (a.defect.evidenceConfirmed ? "已确认" : "未确认")
            << (a.defect.evidenceSource.empty() ? "" : "（来源: " + a.defect.evidenceSource + "）")
            << '\n';
        out << "    受影响范围: 暴露部署 E=" << a.exposedDeployments
            << "（在保发行版 " << a.exposedReleaseCount << " 个）；退役发行版 "
            << a.retiredReleaseCount << " 个（不计入）；部署范围未知 "
            << a.unknownDeploymentReleaseCount << " 个；生命周期未知 "
            << a.unknownLifecycleReleaseCount << " 个\n";
        out << "    缓解覆盖: M=" << a.coveredDeployments
            << "（已核实有效缓解覆盖发行版 " << a.coveredReleaseCount << "/"
            << a.exposedReleaseCount << "），覆盖率 " << fmtCoverage(a)
            << "，未缓解部署 U=" << a.unmitigatedDeployments
            << "，风险分 R=" << fmtFixed(a.riskScore, 1) << '\n';
        out << "    证据置信度: " << fmtFixed(a.confidence, 2)
            << "（" << bandLabelZh(a.confidenceBand) << "）\n";
        for (const auto& ra : a.releases) {
            out << "      - 发行版 " << ra.releaseId
                << " | 生命周期: " << lifecycleLabelZh(ra.lifecycle)
                << " | 部署: " << (ra.deploymentKnown ? fmtInt(ra.deployCount) : std::string("未知"))
                << " | 缓解: ";
            if (!ra.hasMitigationRecord) {
                out << "无记录";
            } else {
                out << ra.bestMitigationId << " 结论「"
                    << mitigationLabelZh(ra.bestStatus) << "」"
                    << (ra.coveredByEffectiveMitigation ? " [计入覆盖]" : " [不计入覆盖]");
            }
            if (ra.lifecycle == Lifecycle::Retired) out << " | 退役，不计入暴露面";
            out << '\n';
        }
        if (a.checklist.empty()) {
            out << "    核实清单: 无（证据充分）\n";
        } else {
            out << "    核实清单（" << a.checklist.size() << " 项）:\n";
            for (const auto& item : a.checklist) {
                out << "      * [" << item.category << "] " << item.action << '\n';
            }
        }
        out << '\n';
    }

    out << "四、证据不足条目核实清单汇总（置信度分档为中/低）\n";
    out << "--------------------------------------------------------------------------------\n";
    bool any = false;
    for (const auto& a : results) {
        if (a.confidenceBand == "high") continue;
        any = true;
        out << "- " << a.defect.defectId << "（置信度 " << fmtFixed(a.confidence, 2)
            << "，" << bandLabelZh(a.confidenceBand) << "）共 " << a.checklist.size()
            << " 项待核实:\n";
        for (const auto& item : a.checklist) {
            out << "    * [" << item.category << "] " << item.action << '\n';
        }
    }
    if (!any) out << "无证据不足条目。\n";
    out << '\n';

    if (comparison != nullptr && opts.includeComparison) {
        out << "五、与基线数据集的对比（修复顺序变化归因）\n";
        out << "--------------------------------------------------------------------------------\n";
        out << "基线数据集: " << (opts.baselineLabel.empty() ? std::string("(未命名)") : opts.baselineLabel)
            << "  ->  当前数据集: "
            << (opts.inputLabel.empty() ? std::string("(未命名)") : opts.inputLabel) << "\n\n";
        if (comparison->rankFlips.empty()) {
            out << "无排名反转。\n\n";
        } else {
            out << "排名反转（相对次序发生交换的缺陷对）:\n";
            for (const auto& flip : comparison->rankFlips) {
                out << "  * " << flip.defectIdA << " 与 " << flip.defectIdB
                    << " 的相对次序发生反转\n";
            }
            out << '\n';
        }
        out << "逐缺陷变化说明:\n";
        for (const auto& e : comparison->entries) {
            out << "  " << e.defectId << ": ";
            if (e.isNew) {
                out << "新增条目，当前排名 #" << e.currentRank << '\n';
            } else if (e.isRemoved) {
                out << "已移除，基线排名 #" << e.baselineRank << '\n';
            } else {
                out << "排名 #" << e.baselineRank << " -> #" << e.currentRank
                    << "，风险分 " << fmtFixed(e.baselineRisk, 1) << " -> "
                    << fmtFixed(e.currentRisk, 1)
                    << "，未缓解部署 " << e.baselineUnmitigated << " -> "
                    << e.currentUnmitigated
                    << "，置信度 " << fmtFixed(e.baselineConfidence, 2) << " -> "
                    << fmtFixed(e.currentConfidence, 2) << '\n';
            }
            for (const auto& reason : e.reasons) {
                out << "      - " << reason << '\n';
            }
        }
        out << '\n';
    }

    out << "================================================================================\n";
    out << "报告结束\n";
    return out.str();
}

std::string renderJsonReport(const std::vector<Assessment>& results,
                             const ReportOptions& opts,
                             const ComparisonReport* comparison) {
    std::ostringstream ss;
    ss << "{\n";
    ss << "  \"report\": \"memory-defect-triage\",\n";
    ss << "  \"schema_version\": 1,\n";
    ss << "  \"input_label\": " << q(opts.inputLabel) << ",\n";
    ss << "  \"scoring\": {\n";
    ss << "    \"risk_formula\": \"R = impact_weight * unmitigated_deployments\",\n";
    ss << "    \"coverage_rule\": \"only verified_effective mitigations count toward covered_deployments\",\n";
    ss << "    \"retired_rule\": \"retired releases excluded from exposure\",\n";
    ss << "    \"sort_keys\": [\"risk_score desc\", \"impact_weight desc\",\n";
    ss << "                   \"unmitigated_deployments desc\", \"confidence asc\",\n";
    ss << "                   \"alert_count desc\", \"defect_id asc\"]\n";
    ss << "  },\n";
    ss << "  \"assessments\": [\n";
    for (std::size_t i = 0; i < results.size(); ++i) {
        const auto& a = results[i];
        ss << "    {\n";
        ss << "      \"rank\": " << (i + 1) << ",\n";
        ss << "      \"defect_id\": " << q(a.defect.defectId) << ",\n";
        ss << "      \"title\": " << q(a.defect.title) << ",\n";
        ss << "      \"impact_level\": " << q(toString(a.defect.impactLevel)) << ",\n";
        ss << "      \"impact_weight\": " << fmtFixed(a.impactWeight, 0) << ",\n";
        ss << "      \"alert_count\": " << a.defect.alertCount << ",\n";
        ss << "      \"evidence_confirmed\": " << (a.defect.evidenceConfirmed ? "true" : "false") << ",\n";
        ss << "      \"exposed_deployments\": " << a.exposedDeployments << ",\n";
        ss << "      \"covered_deployments\": " << a.coveredDeployments << ",\n";
        ss << "      \"unmitigated_deployments\": " << a.unmitigatedDeployments << ",\n";
        ss << "      \"exposed_release_count\": " << a.exposedReleaseCount << ",\n";
        ss << "      \"covered_release_count\": " << a.coveredReleaseCount << ",\n";
        ss << "      \"retired_release_count\": " << a.retiredReleaseCount << ",\n";
        ss << "      \"unknown_deployment_release_count\": " << a.unknownDeploymentReleaseCount << ",\n";
        ss << "      \"unknown_lifecycle_release_count\": " << a.unknownLifecycleReleaseCount << ",\n";
        ss << "      \"mitigation_coverage\": "
           << (a.hasCoverage ? fmtFixed(a.mitigationCoverage, 4) : "null") << ",\n";
        ss << "      \"risk_score\": " << fmtFixed(a.riskScore, 1) << ",\n";
        ss << "      \"confidence\": " << fmtFixed(a.confidence, 2) << ",\n";
        ss << "      \"confidence_band\": " << q(a.confidenceBand) << ",\n";
        ss << "      \"releases\": [\n";
        for (std::size_t j = 0; j < a.releases.size(); ++j) {
            appendJsonRelease(ss, a.releases[j], "        ");
            if (j + 1 < a.releases.size()) ss << ',';
            ss << '\n';
        }
        ss << "      ],\n";
        ss << "      \"checklist\": [\n";
        for (std::size_t j = 0; j < a.checklist.size(); ++j) {
            appendJsonChecklist(ss, a.checklist[j], "        ");
            if (j + 1 < a.checklist.size()) ss << ',';
            ss << '\n';
        }
        ss << "      ]\n";
        ss << "    }" << (i + 1 < results.size() ? "," : "") << '\n';
    }
    ss << "  ]";

    if (comparison != nullptr && opts.includeComparison) {
        ss << ",\n  \"comparison\": {\n";
        ss << "    \"baseline_label\": " << q(opts.baselineLabel) << ",\n";
        ss << "    \"rank_flips\": [\n";
        for (std::size_t i = 0; i < comparison->rankFlips.size(); ++i) {
            const auto& f = comparison->rankFlips[i];
            ss << "      {\"defect_a\": " << q(f.defectIdA)
               << ", \"defect_b\": " << q(f.defectIdB)
               << ", \"baseline_order\": " << f.baselineOrder
               << ", \"current_order\": " << f.currentOrder << "}"
               << (i + 1 < comparison->rankFlips.size() ? "," : "") << '\n';
        }
        ss << "    ],\n";
        ss << "    \"entries\": [\n";
        for (std::size_t i = 0; i < comparison->entries.size(); ++i) {
            const auto& e = comparison->entries[i];
            ss << "      {\n";
            ss << "        \"defect_id\": " << q(e.defectId) << ",\n";
            ss << "        \"baseline_rank\": " << (e.baselineRank ? std::to_string(e.baselineRank) : "null") << ",\n";
            ss << "        \"current_rank\": " << (e.currentRank ? std::to_string(e.currentRank) : "null") << ",\n";
            ss << "        \"is_new\": " << (e.isNew ? "true" : "false") << ",\n";
            ss << "        \"is_removed\": " << (e.isRemoved ? "true" : "false") << ",\n";
            ss << "        \"baseline_risk_score\": " << fmtFixed(e.baselineRisk, 1) << ",\n";
            ss << "        \"current_risk_score\": " << fmtFixed(e.currentRisk, 1) << ",\n";
            ss << "        \"baseline_unmitigated\": " << e.baselineUnmitigated << ",\n";
            ss << "        \"current_unmitigated\": " << e.currentUnmitigated << ",\n";
            ss << "        \"reasons\": [";
            for (std::size_t j = 0; j < e.reasons.size(); ++j) {
                ss << q(e.reasons[j]);
                if (j + 1 < e.reasons.size()) ss << ", ";
            }
            ss << "]\n";
            ss << "      }" << (i + 1 < comparison->entries.size() ? "," : "") << '\n';
        }
        ss << "    ]\n";
        ss << "  }";
    }
    ss << "\n}\n";
    return ss.str();
}

}  // namespace memtriage
