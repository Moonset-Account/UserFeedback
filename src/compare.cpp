#include "compare.hpp"

#include <map>
#include <set>
#include <sstream>

namespace memtriage {

namespace {

struct ReleaseSnapshot {
    bool deploymentKnown;
    std::int64_t deployCount;
    Lifecycle lifecycle;
    bool covered;
    bool hasMitigation;
    MitigationStatus bestStatus;
    std::string bestMitigationId;
};

std::map<std::string, ReleaseSnapshot> snapshotReleases(const Assessment& a) {
    std::map<std::string, ReleaseSnapshot> out;
    for (const auto& r : a.releases) {
        out[r.releaseId] = ReleaseSnapshot{
            r.deploymentKnown, r.deployCount, r.lifecycle,
            r.coveredByEffectiveMitigation, r.hasMitigationRecord,
            r.bestStatus, r.bestMitigationId};
    }
    return out;
}

void appendMitigationReasons(const Assessment& base, const Assessment& cur,
                             std::vector<std::string>& reasons) {
    auto bmap = snapshotReleases(base);
    auto cmap = snapshotReleases(cur);
    std::set<std::string> ids;
    for (const auto& kv : bmap) ids.insert(kv.first);
    for (const auto& kv : cmap) ids.insert(kv.first);
    for (const auto& rid : ids) {
        auto bit = bmap.find(rid);
        auto cit = cmap.find(rid);
        if (bit == bmap.end()) {
            const auto& c = cit->second;
            reasons.push_back("新增受影响发行版 " + rid +
                              std::string("（生命周期:") + lifecycleLabelZh(c.lifecycle) +
                              (c.deploymentKnown ? "，部署 " + std::to_string(c.deployCount)
                                                 : "，部署范围未知") +
                              (c.covered ? "，已有已核实有效缓解）" : "，暂无已核实有效缓解）"));
            continue;
        }
        if (cit == cmap.end()) {
            reasons.push_back("发行版 " + rid + " 已移出受影响范围");
            continue;
        }
        const auto& b = bit->second;
        const auto& c = cit->second;
        if (b.lifecycle != c.lifecycle) {
            reasons.push_back("发行版 " + rid + " 生命周期由「" + lifecycleLabelZh(b.lifecycle) +
                              "」更正为「" + lifecycleLabelZh(c.lifecycle) + "」");
        }
        if (b.deploymentKnown != c.deploymentKnown) {
            if (c.deploymentKnown) {
                reasons.push_back("发行版 " + rid + " 的部署范围由未知补全为 " +
                                  std::to_string(c.deployCount) + "，纳入受影响部署规模");
            } else {
                reasons.push_back("发行版 " + rid + " 的部署数量改为未知，移出受影响部署规模并触发核实清单");
            }
        } else if (c.deploymentKnown && b.deployCount != c.deployCount) {
            std::int64_t delta = c.deployCount - b.deployCount;
            reasons.push_back("发行版 " + rid + " 部署数量 " + std::to_string(b.deployCount) +
                              " -> " + std::to_string(c.deployCount) +
                              "（" + (delta >= 0 ? "+" : "") + std::to_string(delta) + "）");
        }
        if (b.covered != c.covered || b.bestStatus != c.bestStatus ||
            b.hasMitigation != c.hasMitigation ||
            b.bestMitigationId != c.bestMitigationId) {
            if (c.covered && !b.covered) {
                reasons.push_back("发行版 " + rid + " 的缓解证据更正为「已核实有效」（缓解 " +
                                  c.bestMitigationId + "），该发行版部署计入缓解覆盖");
            } else if (!c.covered && b.covered) {
                reasons.push_back("发行版 " + rid + " 的缓解结论被撤销/改判（当前：" +
                                  std::string(mitigationLabelZh(c.bestStatus)) +
                                  "），不再计入缓解覆盖");
            } else if (c.hasMitigation) {
                reasons.push_back("发行版 " + rid + " 缓解结论由「" +
                                  std::string(mitigationLabelZh(b.bestStatus)) + "」变更为「" +
                                  mitigationLabelZh(c.bestStatus) + "」（缓解 " +
                                  c.bestMitigationId + "），均不满足覆盖条件");
            }
        }
    }
}

}  // namespace

ComparisonReport compareAssessments(const std::vector<Assessment>& baseline,
                                    const std::vector<Assessment>& current) {
    ComparisonReport report;
    auto baseIndex = indexById(baseline);
    auto curIndex = indexById(current);

    std::map<std::string, int> baseRank;
    for (std::size_t i = 0; i < baseline.size(); ++i)
        baseRank[baseline[i].defect.defectId] = static_cast<int>(i + 1);
    std::map<std::string, int> curRank;
    for (std::size_t i = 0; i < current.size(); ++i)
        curRank[current[i].defect.defectId] = static_cast<int>(i + 1);

    for (const auto& cur : current) {
        ComparisonEntry e;
        e.defectId = cur.defect.defectId;
        e.currentRank = curRank[cur.defect.defectId];
        e.currentUnmitigated = cur.unmitigatedDeployments;
        e.currentRisk = cur.riskScore;
        e.currentConfidence = cur.confidence;

        auto bit = baseIndex.find(cur.defect.defectId);
        if (bit == baseIndex.end()) {
            e.isNew = true;
            e.reasons.push_back("新增缺陷记录，直接进入分诊序列");
            report.entries.push_back(std::move(e));
            continue;
        }
        const Assessment& base = *bit->second;
        e.baselineRank = baseRank[cur.defect.defectId];
        e.baselineUnmitigated = base.unmitigatedDeployments;
        e.baselineRisk = base.riskScore;
        e.baselineConfidence = base.confidence;

        if (base.exposedDeployments != cur.exposedDeployments) {
            std::int64_t delta = cur.exposedDeployments - base.exposedDeployments;
            std::ostringstream ss;
            ss << "受影响部署规模 E: " << base.exposedDeployments << " -> "
               << cur.exposedDeployments << "（" << (delta >= 0 ? "+" : "") << delta
               << "，来源：新增/缩减部署或部署范围由未知补全）";
            e.reasons.push_back(ss.str());
        }
        if (cur.unknownDeploymentReleaseCount != base.unknownDeploymentReleaseCount) {
            std::ostringstream ss;
            ss << "部署范围未知的在保发行版: " << base.unknownDeploymentReleaseCount
               << " -> " << cur.unknownDeploymentReleaseCount;
            e.reasons.push_back(ss.str());
        }
        appendMitigationReasons(base, cur, e.reasons);
        if (base.coveredDeployments != cur.coveredDeployments) {
            std::ostringstream ss;
            ss << "已核实有效缓解覆盖部署 M: " << base.coveredDeployments << " -> "
               << cur.coveredDeployments
               << "（仅 verified_effective 结论计入覆盖）";
            e.reasons.push_back(ss.str());
        }
        if (base.unmitigatedDeployments != cur.unmitigatedDeployments) {
            std::ostringstream ss;
            ss << "未缓解部署 U=E-M: " << base.unmitigatedDeployments << " -> "
               << cur.unmitigatedDeployments;
            e.reasons.push_back(ss.str());
        }
        if (base.riskScore != cur.riskScore) {
            std::ostringstream ss;
            ss << "风险分 R=中断权重*U: " << base.riskScore << " -> " << cur.riskScore
               << "，排序键 1 变化";
            e.reasons.push_back(ss.str());
        }
        if (base.confidence != cur.confidence) {
            std::ostringstream ss;
            ss << "证据置信度: " << base.confidence << "（" << base.confidenceBand << "） -> "
               << cur.confidence << "（" << cur.confidenceBand << "）";
            e.reasons.push_back(ss.str());
        }
        if (e.reasons.empty()) {
            e.reasons.push_back("指标无变化，排名仅可能因其他缺陷的分数变动而被动调整");
        }
        report.entries.push_back(std::move(e));
    }

    for (const auto& base : baseline) {
        if (!curIndex.count(base.defect.defectId)) {
            ComparisonEntry e;
            e.defectId = base.defect.defectId;
            e.isRemoved = true;
            e.baselineRank = baseRank[base.defect.defectId];
            e.baselineUnmitigated = base.unmitigatedDeployments;
            e.baselineRisk = base.riskScore;
            e.baselineConfidence = base.confidence;
            e.reasons.push_back("缺陷记录已从当前数据集移除");
            report.entries.push_back(std::move(e));
        }
    }

    // 排名反转：基线中 A 在 B 前，当前反转。仅比较两期都存在的缺陷。
    for (std::size_t i = 0; i < baseline.size(); ++i) {
        for (std::size_t j = i + 1; j < baseline.size(); ++j) {
            const std::string& a = baseline[i].defect.defectId;
            const std::string& b = baseline[j].defect.defectId;
            auto ra = curRank.find(a);
            auto rb = curRank.find(b);
            if (ra == curRank.end() || rb == curRank.end()) continue;
            if (ra->second > rb->second) {
                report.rankFlips.push_back(RankFlip{a, b, -1, 1});
            }
        }
    }
    return report;
}

}  // namespace memtriage
