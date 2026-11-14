#include "ssp_checker/Report.h"

namespace ssp {
namespace {

void renderEvidence(const FunctionVerdict& verdict, std::ostream& output) {
    if (verdict.evidence.empty()) {
        output << "    evidence: (no attribution record exported by the build)\n";
        return;
    }
    for (const auto& link : verdict.evidence) {
        output << "    evidence: " << link.attributionId << " (" << token(link.relation)
               << ") " << verdict.sourceFunction << " -> " << link.compiledFunction
               << " @ " << link.compilationUnit << " | ";
        if (!link.hasMark) {
            output << "mark=MISSING\n";
        } else {
            output << "mark=" << token(link.mark) << " via " << link.markId;
            if (!link.protectorKind.empty()) {
                output << " (" << link.protectorKind << ")";
            }
            output << "\n";
        }
    }
}

}  // namespace

void renderReport(const CoverageReport& report, std::ostream& output) {
    output << "=== SEC-VULN-013 stack-protection coverage report ===\n";
    output << "configs: ";
    for (size_t i = 0; i < report.configs.size(); ++i) {
        if (i) output << ", ";
        output << report.configs[i];
    }
    output << "\n\n";

    output << "[1] per-config coverage summary\n";
    for (const auto& summary : report.summaries) {
        output << "S | config=" << summary.config << " total=" << summary.total
               << " confirmed=" << summary.confirmed << " violation=" << summary.violation
               << " unknown=" << summary.unknown
               << " not_required=" << summary.notRequired << "\n";
    }
    output << "\n";

    output << "[2] function findings (source item -> post-optimization owner)\n";
    for (const auto& verdict : report.verdicts) {
        output << "V | config=" << verdict.config << " risk_id=" << verdict.riskId
               << " source=" << verdict.sourceFunction << " cu=" << verdict.compilationUnit
               << " level=" << token(verdict.level)
               << " requirement=" << token(verdict.requirement)
               << " verdict=" << token(verdict.verdict)
               << " reason=" << token(verdict.reason) << "\n";
        output << "    reason_zh: " << reasonLabelZh(verdict.reason) << "\n";
        renderEvidence(verdict, output);
    }
    output << "\n";

    output << "[3] cross-config coverage differences\n";
    if (report.diffs.empty()) {
        output << "    (no verdict change between adjacent configurations)\n";
    } else {
        for (const auto& diff : report.diffs) {
            output << "D | risk_id=" << diff.riskId << " source=" << diff.sourceFunction
                   << " " << diff.baselineConfig << ":" << token(diff.baselineVerdict)
                   << "/" << token(diff.baselineReason) << " -> " << diff.comparedConfig
                   << ":" << token(diff.comparedVerdict) << "/"
                   << token(diff.comparedReason) << "\n";
        }
    }
    output << "\n";

    output << "[4] compilation units to re-review (UNKNOWN / VIOLATION)\n";
    if (report.unitsToReview.empty()) {
        output << "    (none)\n";
    } else {
        for (const auto& review : report.unitsToReview) {
            output << "U | config=" << review.config << " cu=" << review.compilationUnit
                   << " unknown=" << review.unknownCount
                   << " violation=" << review.violationCount << "\n";
        }
    }
    output << "\n";

    if (!report.warnings.empty()) {
        output << "[5] data quality warnings\n";
        for (const auto& warning : report.warnings) {
            output << "W | " << warning << "\n";
        }
    }
}

}  // namespace ssp
