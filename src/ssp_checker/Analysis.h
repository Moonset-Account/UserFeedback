// Coverage decision engine.
//
// For every (risk-list entry, build configuration) pair the engine resolves
// the post-optimization attribution of the source function, joins each
// compiled function with its abstract protection mark, and derives a verdict:
//
//   NOT_REQUIRED : policy marks the source function as an exempt leaf
//   UNKNOWN      : no attribution row, or an attributed compiled function has
//                  no exported protection mark (missing/ambiguous build record)
//   CONFIRMED    : every attributed compiled function is protected; the reason
//                  distinguishes a retained self record from inlined callers
//   VIOLATION    : all attributed compiled functions are unprotected, or only
//                  a subset of several callers is protected (ambiguous
//                  multi-attribution cannot be treated as covered)
#pragma once

#include <string>
#include <vector>

#include "ssp_checker/Types.h"

namespace ssp {

// One hop of the minimal evidence chain: source function -> compiled function
// -> abstract protection mark, carrying the exported record ids.
struct EvidenceLink {
    std::string attributionId;
    Relation relation = Relation::Self;
    std::string compiledFunction;
    std::string compilationUnit;
    bool hasMark = false;
    Mark mark = Mark::Unprotected;
    std::string protectorKind;
    std::string markId;
};

struct FunctionVerdict {
    std::string config;
    std::string riskId;
    std::string sourceFunction;
    std::string compilationUnit;
    RiskLevel level = RiskLevel::Low;
    Requirement requirement = Requirement::Required;
    Verdict verdict = Verdict::Unknown;
    Reason reason = Reason::None;
    std::vector<EvidenceLink> evidence;
    // Compilation units a maintainer must re-review for this verdict.
    std::vector<std::string> reviewCompilationUnits;
};

struct ConfigSummary {
    std::string config;
    int total = 0;
    int confirmed = 0;
    int violation = 0;
    int unknown = 0;
    int notRequired = 0;
};

struct CoverageDiff {
    std::string sourceFunction;
    std::string riskId;
    std::string baselineConfig;
    std::string comparedConfig;
    Verdict baselineVerdict = Verdict::Unknown;
    Reason baselineReason = Reason::None;
    Verdict comparedVerdict = Verdict::Unknown;
    Reason comparedReason = Reason::None;
};

struct CompilationUnitReview {
    std::string compilationUnit;
    std::string config;
    int unknownCount = 0;
    int violationCount = 0;
};

struct CoverageReport {
    std::vector<std::string> configs;
    // Ordered by config declaration order, then risk-list order.
    std::vector<FunctionVerdict> verdicts;
    std::vector<ConfigSummary> summaries;
    // One entry per adjacent config pair whose verdict changed.
    std::vector<CoverageDiff> diffs;
    // Distinct (config, compilation unit) pairs carrying UNKNOWN/VIOLATION.
    std::vector<CompilationUnitReview> unitsToReview;
    // Non-fatal data quality warnings (e.g. config name mismatches).
    std::vector<std::string> warnings;
};

CoverageReport buildCoverageReport(
    const std::vector<RiskEntry>& risks,
    const std::vector<AttributionEntry>& attributions,
    const std::vector<ProtectionMark>& marks,
    const std::vector<std::string>& configs);

}  // namespace ssp
