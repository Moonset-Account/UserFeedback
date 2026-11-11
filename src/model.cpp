#include "model.hpp"

#include <stdexcept>

namespace memtriage {

ImpactLevel parseImpactLevel(const std::string& s) {
    if (s == "minor_degradation") return ImpactLevel::MinorDegradation;
    if (s == "service_degradation") return ImpactLevel::ServiceDegradation;
    if (s == "business_interruption") return ImpactLevel::BusinessInterruption;
    if (s == "critical_process_abort") return ImpactLevel::CriticalProcessAbort;
    throw std::runtime_error("unknown impact_level: " + s);
}

const char* toString(ImpactLevel level) {
    switch (level) {
        case ImpactLevel::MinorDegradation:       return "minor_degradation";
        case ImpactLevel::ServiceDegradation:     return "service_degradation";
        case ImpactLevel::BusinessInterruption:   return "business_interruption";
        case ImpactLevel::CriticalProcessAbort:   return "critical_process_abort";
    }
    return "unknown";
}

const char* impactLabelZh(ImpactLevel level) {
    switch (level) {
        case ImpactLevel::MinorDegradation:       return "轻微降级";
        case ImpactLevel::ServiceDegradation:     return "服务降级";
        case ImpactLevel::BusinessInterruption:   return "业务中断";
        case ImpactLevel::CriticalProcessAbort:   return "关键进程中止";
    }
    return "未知";
}

Lifecycle parseLifecycle(const std::string& s) {
    if (s == "active") return Lifecycle::Active;
    if (s == "retired") return Lifecycle::Retired;
    if (s == "unknown") return Lifecycle::Unknown;
    throw std::runtime_error("unknown lifecycle: " + s);
}

const char* toString(Lifecycle lc) {
    switch (lc) {
        case Lifecycle::Active:  return "active";
        case Lifecycle::Retired: return "retired";
        case Lifecycle::Unknown: return "unknown";
    }
    return "unknown";
}

const char* lifecycleLabelZh(Lifecycle lc) {
    switch (lc) {
        case Lifecycle::Active:  return "活跃";
        case Lifecycle::Retired: return "退役";
        case Lifecycle::Unknown: return "生命周期未知";
    }
    return "未知";
}

MitigationStatus parseMitigationStatus(const std::string& s) {
    if (s == "verified_effective") return MitigationStatus::VerifiedEffective;
    if (s == "ineffective") return MitigationStatus::Ineffective;
    if (s == "inconclusive") return MitigationStatus::Inconclusive;
    if (s == "pending") return MitigationStatus::Pending;
    if (s == "disputed") return MitigationStatus::Disputed;
    throw std::runtime_error("unknown verification_status: " + s);
}

const char* toString(MitigationStatus st) {
    switch (st) {
        case MitigationStatus::VerifiedEffective: return "verified_effective";
        case MitigationStatus::Ineffective:       return "ineffective";
        case MitigationStatus::Inconclusive:      return "inconclusive";
        case MitigationStatus::Pending:           return "pending";
        case MitigationStatus::Disputed:          return "disputed";
    }
    return "unknown";
}

const char* mitigationLabelZh(MitigationStatus st) {
    switch (st) {
        case MitigationStatus::VerifiedEffective: return "已核实有效";
        case MitigationStatus::Ineffective:       return "已核实无效";
        case MitigationStatus::Inconclusive:      return "结论不确定";
        case MitigationStatus::Pending:           return "待验证";
        case MitigationStatus::Disputed:          return "结论存在争议";
    }
    return "未知";
}

}  // namespace memtriage
