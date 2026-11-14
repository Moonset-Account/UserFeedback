#include "ssp_checker/Types.h"

namespace ssp {

const char* token(RiskLevel level) {
    switch (level) {
        case RiskLevel::Low:    return "low";
        case RiskLevel::Medium: return "medium";
        case RiskLevel::High:   return "high";
    }
    return "unknown";
}

const char* token(Requirement requirement) {
    switch (requirement) {
        case Requirement::Required:   return "required";
        case Requirement::ExemptLeaf: return "exempt_leaf";
    }
    return "unknown";
}

const char* token(Relation relation) {
    switch (relation) {
        case Relation::Self:    return "self";
        case Relation::Inlined: return "inlined";
    }
    return "unknown";
}

const char* token(Mark mark) {
    switch (mark) {
        case Mark::Protected:   return "protected";
        case Mark::Unprotected: return "unprotected";
    }
    return "unknown";
}

const char* token(Verdict verdict) {
    switch (verdict) {
        case Verdict::Confirmed:   return "CONFIRMED";
        case Verdict::Violation:   return "VIOLATION";
        case Verdict::Unknown:     return "UNKNOWN";
        case Verdict::NotRequired: return "NOT_REQUIRED";
    }
    return "UNKNOWN";
}

const char* token(Reason reason) {
    switch (reason) {
        case Reason::None:                    return "none";
        case Reason::SelfProtected:           return "self_protected";
        case Reason::InlinedCallerProtected:  return "inlined_caller_protected";
        case Reason::PartialCallerProtection: return "partial_caller_protection";
        case Reason::MissingAttribution:      return "missing_attribution";
        case Reason::MissingProtectionMark:   return "missing_protection_mark";
        case Reason::UnprotectedSelf:         return "unprotected_self";
        case Reason::UnprotectedCallers:      return "unprotected_callers";
        case Reason::ExemptLeaf:              return "exempt_leaf";
    }
    return "unknown";
}

const char* reasonLabelZh(Reason reason) {
    switch (reason) {
        case Reason::None:
            return "无";
        case Reason::SelfProtected:
            return "函数自身的编译后记录具备栈保护";
        case Reason::InlinedCallerProtected:
            return "内联后承接该函数的调用者记录具备栈保护";
        case Reason::PartialCallerProtection:
            return "存在多个内联归属，仅部分调用者具备保护记录，归属不确定";
        case Reason::MissingAttribution:
            return "构建产物中缺失该源码函数的优化归属记录";
        case Reason::MissingProtectionMark:
            return "归属的编译后函数缺失构建工具导出的保护标记";
        case Reason::UnprotectedSelf:
            return "函数自身的编译后记录明确无栈保护";
        case Reason::UnprotectedCallers:
            return "承接该函数的全部内联调用者均无栈保护";
        case Reason::ExemptLeaf:
            return "策略不要求保护的叶函数";
    }
    return "未知原因";
}

}  // namespace ssp
