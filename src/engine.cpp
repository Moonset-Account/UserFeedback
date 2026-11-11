#include "engine.hpp"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>

namespace memtriage {

namespace {

double round6(double v) {
    // 消除浮点尾差，保证同分比较与展示一致。
    return std::round(v * 1e6) / 1e6;
}

int mitigationRank(MitigationStatus st) {
    // 数值越小优先级越高：已核实有效优先展示，其次是明确无效，再次是争议/不确定/待验证。
    switch (st) {
        case MitigationStatus::VerifiedEffective: return 0;
        case MitigationStatus::Ineffective:       return 1;
        case MitigationStatus::Disputed:          return 2;
        case MitigationStatus::Inconclusive:      return 3;
        case MitigationStatus::Pending:           return 4;
    }
    return 5;
}

struct MitigationRef {
    std::string mitigationId;
    MitigationStatus status;
};

ReleaseAssessment assessRelease(const std::string& defectId,
                                const std::string& releaseId,
                                const DeploymentRecord* dep,
                                const std::vector<MitigationRecord>& allMitigations) {
    ReleaseAssessment ra;
    ra.releaseId = releaseId;
    if (dep == nullptr) {
        // 受影响发行版在部署表中缺失：既不能假设为 0，也不能假设已退役。
        ra.lifecycle = Lifecycle::Unknown;
        ra.deploymentKnown = false;
        ra.deployCount = 0;
    } else {
        ra.lifecycle = dep->lifecycle;
        ra.deploymentKnown = dep->hasDeployCount;
        ra.deployCount = dep->hasDeployCount ? dep->deployCount : 0;
    }

    MitigationRef bestRef;
    for (const auto& m : allMitigations) {
        if (m.defectId != defectId || m.releaseId != releaseId) continue;
        MitigationRef cur{m.mitigationId, m.status};
        if (!ra.hasMitigationRecord) {
            bestRef = cur;
            ra.hasMitigationRecord = true;
        } else {
            int curRank = mitigationRank(cur.status);
            int bestRank = mitigationRank(bestRef.status);
            if (curRank < bestRank ||
                (curRank == bestRank && cur.mitigationId < bestRef.mitigationId)) {
                bestRef = cur;
            }
        }
    }
    if (ra.hasMitigationRecord) {
        ra.bestStatus = bestRef.status;
        ra.bestMitigationId = bestRef.mitigationId;
        ra.coveredByEffectiveMitigation = (bestRef.status == MitigationStatus::VerifiedEffective);
    }
    return ra;
}

void addChecklist(std::vector<ChecklistItem>& list, const std::string& defectId,
                  const std::string& releaseId, const std::string& category,
                  const std::string& action) {
    list.push_back(ChecklistItem{defectId, releaseId, category, action});
}

}  // namespace

const ScoringConfig& defaultConfig() {
    static const ScoringConfig cfg{};
    return cfg;
}

const char* confidenceBand(double confidence, const ScoringConfig& cfg) {
    if (confidence >= cfg.confidenceHighThreshold) return "high";
    if (confidence >= cfg.confidenceMediumThreshold) return "medium";
    return "low";
}

std::map<std::string, const Assessment*> indexById(const std::vector<Assessment>& list) {
    std::map<std::string, const Assessment*> out;
    for (const auto& a : list) out.emplace(a.defect.defectId, &a);
    return out;
}

std::vector<Assessment> runTriage(const Dataset& ds, const ScoringConfig& cfg) {
    std::map<std::string, const DeploymentRecord*> depIndex;
    for (const auto& d : ds.deployments) depIndex[d.releaseId] = &d;

    std::vector<Assessment> results;
    results.reserve(ds.defects.size());

    for (const auto& defect : ds.defects) {
        Assessment a;
        a.defect = defect;
        a.impactWeight = static_cast<double>(static_cast<int>(defect.impactLevel));

        // 同一缺陷重复列出同一发行版时去重，避免重复计数。
        std::set<std::string> seenReleases;
        for (const auto& releaseId : defect.affectedReleases) {
            if (!seenReleases.insert(releaseId).second) continue;
            auto it = depIndex.find(releaseId);
            const DeploymentRecord* dep = (it == depIndex.end()) ? nullptr : it->second;
            a.releases.push_back(assessRelease(defect.defectId, releaseId, dep,
                                               ds.mitigations));
        }
        // 发行版按 ID 排序，保证输出与后续 diff 稳定。
        std::sort(a.releases.begin(), a.releases.end(),
                  [](const ReleaseAssessment& x, const ReleaseAssessment& y) {
                      return x.releaseId < y.releaseId;
                  });

        double penalty = 0.0;
        for (const auto& ra : a.releases) {
            const bool retired = (ra.lifecycle == Lifecycle::Retired);
            if (retired) {
                // 退役版本不计入当前暴露面，不产生扣分与核实项。
                ++a.retiredReleaseCount;
                continue;
            }
            ++a.exposedReleaseCount;
            if (ra.lifecycle == Lifecycle::Unknown) {
                ++a.unknownLifecycleReleaseCount;
                penalty += cfg.penaltyPerUnknownLifecycle;
                addChecklist(a.checklist, defect.defectId, ra.releaseId,
                             "lifecycle_unknown",
                             "核实发行版 " + ra.releaseId +
                                 " 的生命周期状态（无法证明已退役，当前按在保处理）");
            }
            if (!ra.deploymentKnown) {
                ++a.unknownDeploymentReleaseCount;
                penalty += cfg.penaltyPerUnknownDeployment;
                addChecklist(a.checklist, defect.defectId, ra.releaseId,
                             "unknown_deployment",
                             "补全发行版 " + ra.releaseId +
                                 " 的部署数量（当前未知，未计入受影响部署规模，禁止按 0 处理）");
            } else {
                a.exposedDeployments += ra.deployCount;
            }
            if (ra.coveredByEffectiveMitigation) {
                ++a.coveredReleaseCount;
                if (ra.deploymentKnown) a.coveredDeployments += ra.deployCount;
            } else {
                ++a.openMitigationReleaseCount;
                penalty += cfg.penaltyPerOpenMitigation;
                std::string reason;
                if (!ra.hasMitigationRecord) {
                    reason = "发行版 " + ra.releaseId + " 无任何缓解措施记录，需确认是否存在可用缓解并完成验证";
                } else {
                    reason = "发行版 " + ra.releaseId + " 的缓解 " + ra.bestMitigationId +
                             " 结论为「" + mitigationLabelZh(ra.bestStatus) +
                             "」，不能计入覆盖，需重新验证或更换缓解";
                }
                addChecklist(a.checklist, defect.defectId, ra.releaseId,
                             "mitigation_unverified", reason);
            }
        }

        a.unmitigatedDeployments = a.exposedDeployments - a.coveredDeployments;
        if (a.exposedDeployments > 0) {
            a.hasCoverage = true;
            a.mitigationCoverage = round6(static_cast<double>(a.coveredDeployments) /
                                          static_cast<double>(a.exposedDeployments));
        }
        a.riskScore = round6(a.impactWeight * static_cast<double>(a.unmitigatedDeployments));

        if (!defect.evidenceConfirmed) {
            penalty += cfg.penaltyUnconfirmedEvidence;
            addChecklist(a.checklist, defect.defectId, "", "impact_evidence",
                         "影响证据未经确认（来源：" +
                             (defect.evidenceSource.empty() ? "未登记" : defect.evidenceSource) +
                             "），需复核缺陷影响等级与告警有效性");
        }
        a.confidence = round6(std::max(0.0, std::min(1.0, 1.0 - penalty)));
        a.confidenceBand = confidenceBand(a.confidence, cfg);

        results.push_back(std::move(a));
    }

    // 确定性全序比较器：任何输入顺序下输出次序唯一，同分时次序可复现。
    std::sort(results.begin(), results.end(),
              [](const Assessment& x, const Assessment& y) {
                  if (x.riskScore != y.riskScore) return x.riskScore > y.riskScore;          // 1. 风险分降序
                  if (x.impactWeight != y.impactWeight) return x.impactWeight > y.impactWeight; // 2. 中断等级降序
                  if (x.unmitigatedDeployments != y.unmitigatedDeployments)
                      return x.unmitigatedDeployments > y.unmitigatedDeployments;            // 3. 未缓解部署降序
                  if (x.confidence != y.confidence) return x.confidence < y.confidence;      // 4. 置信度升序（证据弱者优先核实）
                  if (x.defect.alertCount != y.defect.alertCount)
                      return x.defect.alertCount > y.defect.alertCount;                       // 5. 告警次数降序
                  return x.defect.defectId < y.defect.defectId;                               // 6. 缺陷编号字典序
              });
    return results;
}

}  // namespace memtriage
