#ifndef MEMTRIAGE_MODEL_HPP
#define MEMTRIAGE_MODEL_HPP

#include <cstdint>
#include <string>
#include <vector>

namespace memtriage {

// 业务中断等级（impact_level 枚举），weight 为排序权重，越大越严重。
enum class ImpactLevel {
    MinorDegradation = 1,   // 轻微降级
    ServiceDegradation = 2, // 服务降级
    BusinessInterruption = 3, // 业务中断
    CriticalProcessAbort = 4  // 关键进程中止
};

// 发行版生命周期（lifecycle 枚举）。退役版本不计入当前暴露面。
enum class Lifecycle {
    Active,     // 活跃
    Retired,    // 退役
    Unknown     // 未知：无法证明已退役，按谨慎原则纳入暴露面并降低置信度
};

// 缓解措施验证结论（verification_status 枚举）。
// 只有 VerifiedEffective 计入缓解覆盖。
enum class MitigationStatus {
    VerifiedEffective,  // 已核实有效
    Ineffective,        // 已核实无效
    Inconclusive,       // 结论不确定
    Pending,            // 待验证
    Disputed            // 结论存在争议
};

struct DefectRecord {
    std::string defectId;
    std::string title;
    ImpactLevel impactLevel = ImpactLevel::MinorDegradation;
    std::int64_t alertCount = 0;
    bool evidenceConfirmed = false;  // evidence_confirmed：影响证据是否经确认
    std::string evidenceSource;
    std::vector<std::string> affectedReleases;
};

struct DeploymentRecord {
    std::string releaseId;
    Lifecycle lifecycle = Lifecycle::Unknown;
    // deploy_count 为 null 时 hasDeployCount=false，代表部署范围未知，禁止按 0 处理。
    bool hasDeployCount = false;
    std::int64_t deployCount = 0;
};

struct MitigationRecord {
    std::string mitigationId;
    std::string defectId;
    std::string releaseId;
    MitigationStatus status = MitigationStatus::Pending;
    std::string evidenceNote;
};

// 单个受影响发行版层面的评估明细。
struct ReleaseAssessment {
    std::string releaseId;
    Lifecycle lifecycle = Lifecycle::Unknown;
    bool deploymentKnown = false;
    std::int64_t deployCount = 0;
    // 该发行版上该缺陷的最佳缓解结论；无任何缓解记录时为 false。
    bool hasMitigationRecord = false;
    MitigationStatus bestStatus = MitigationStatus::Pending;
    std::string bestMitigationId;
    bool coveredByEffectiveMitigation = false; // 仅当结论为已核实有效
};

struct ChecklistItem {
    std::string defectId;
    std::string releaseId; // 可为空（缺陷级条目）
    std::string category;  // unknown_deployment / lifecycle_unknown /
                           // mitigation_unverified / impact_evidence
    std::string action;
};

struct Assessment {
    DefectRecord defect;

    std::vector<ReleaseAssessment> releases;

    // 受影响范围（仅统计活跃 + 生命周期未知且部署数已知的发行版）
    std::int64_t exposedDeployments = 0;   // E：暴露部署总数
    std::int64_t coveredDeployments = 0;   // M：已核实有效缓解覆盖的部署数
    std::int64_t unmitigatedDeployments = 0; // U = E - M
    int exposedReleaseCount = 0;           // 纳入暴露面的发行版数量
    int coveredReleaseCount = 0;           // 已核实有效缓解覆盖的发行版数量
    int retiredReleaseCount = 0;           // 退役发行版数量（不计入暴露面）
    int unknownDeploymentReleaseCount = 0; // 部署范围未知的发行版数量
    int unknownLifecycleReleaseCount = 0;  // 生命周期未知的发行版数量
    int openMitigationReleaseCount = 0;    // 暴露面中缺乏“已核实有效”缓解的发行版数量

    double impactWeight = 0.0;   // 中断等级权重
    double riskScore = 0.0;      // R = impactWeight * unmitigatedDeployments（整数语义）
    double mitigationCoverage = 0.0; // M / E；E=0 时为空（hasCoverage=false）
    bool hasCoverage = false;
    double confidence = 0.0;     // 0..1
    std::string confidenceBand;  // high / medium / low
    std::vector<ChecklistItem> checklist;
};

// 枚举 <-> 字符串（解析与报告共用，未知值直接报错，避免静默吞数据）。
ImpactLevel parseImpactLevel(const std::string& s);
const char* toString(ImpactLevel level);
const char* impactLabelZh(ImpactLevel level);

Lifecycle parseLifecycle(const std::string& s);
const char* toString(Lifecycle lc);
const char* lifecycleLabelZh(Lifecycle lc);

MitigationStatus parseMitigationStatus(const std::string& s);
const char* toString(MitigationStatus st);
const char* mitigationLabelZh(MitigationStatus st);

}  // namespace memtriage

#endif
