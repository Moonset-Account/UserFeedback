#include "ssp_checker/Analysis.h"

#include <algorithm>
#include <map>
#include <set>
#include <unordered_map>

namespace ssp {
namespace {

std::string markKey(const std::string& config, const std::string& compiledFunction,
                    const std::string& compilationUnit) {
    return config + "|" + compiledFunction + "|" + compilationUnit;
}

struct MarkLookup {
    const ProtectionMark* mark = nullptr;
};

void addUnique(std::vector<std::string>& values, const std::string& value) {
    if (std::find(values.begin(), values.end(), value) == values.end()) {
        values.push_back(value);
    }
}

FunctionVerdict analyzeOne(const RiskEntry& risk, const std::string& config,
                           const std::vector<AttributionEntry>& attributions,
                           const std::unordered_map<std::string, MarkLookup>& marks) {
    FunctionVerdict result;
    result.config = config;
    result.riskId = risk.riskId;
    result.sourceFunction = risk.sourceFunction;
    result.compilationUnit = risk.compilationUnit;
    result.level = risk.level;
    result.requirement = risk.requirement;

    // Policy-exempt leaf functions never require a protection evidence chain.
    if (risk.requirement == Requirement::ExemptLeaf) {
        result.verdict = Verdict::NotRequired;
        result.reason = Reason::ExemptLeaf;
        return result;
    }

    std::vector<const AttributionEntry*> rows;
    for (const auto& row : attributions) {
        if (row.config == config && row.sourceFunction == risk.sourceFunction) {
            rows.push_back(&row);
        }
    }
    std::sort(rows.begin(), rows.end(),
              [](const AttributionEntry* lhs, const AttributionEntry* rhs) {
                  return lhs->attributionId < rhs->attributionId;
              });

    if (rows.empty()) {
        // No post-optimization build record: the checker must stay silent as
        // UNKNOWN rather than inventing protection evidence.
        result.verdict = Verdict::Unknown;
        result.reason = Reason::MissingAttribution;
        addUnique(result.reviewCompilationUnits, risk.compilationUnit);
        return result;
    }

    bool anyMissingMark = false;
    bool anyProtected = false;
    bool anyUnprotected = false;
    bool allInlined = true;
    bool anySelf = false;

    for (const AttributionEntry* row : rows) {
        EvidenceLink link;
        link.attributionId = row->attributionId;
        link.relation = row->relation;
        link.compiledFunction = row->compiledFunction;
        link.compilationUnit = row->compilationUnit;
        if (row->relation == Relation::Self) {
            allInlined = false;
            anySelf = true;
        }
        const auto it = marks.find(
            markKey(row->config, row->compiledFunction, row->compilationUnit));
        if (it == marks.end() || it->second.mark == nullptr) {
            link.hasMark = false;
            anyMissingMark = true;
        } else {
            link.hasMark = true;
            link.mark = it->second.mark->mark;
            link.protectorKind = it->second.mark->protectorKind;
            link.markId = it->second.mark->markId;
            if (link.mark == Mark::Protected) {
                anyProtected = true;
            } else {
                anyUnprotected = true;
            }
        }
        result.evidence.push_back(std::move(link));
    }

    // Missing build evidence always dominates: output UNKNOWN.
    if (anyMissingMark) {
        result.verdict = Verdict::Unknown;
        result.reason = Reason::MissingProtectionMark;
        for (const auto& link : result.evidence) {
            if (!link.hasMark) addUnique(result.reviewCompilationUnits, link.compilationUnit);
        }
        return result;
    }

    // Multiple attributions with split protection cannot be treated as covered.
    const bool multiple = rows.size() > 1;
    if (multiple && anyProtected && anyUnprotected) {
        result.verdict = Verdict::Unknown;
        result.reason = Reason::PartialCallerProtection;
        for (const auto& link : result.evidence) {
            if (link.mark == Mark::Unprotected) {
                addUnique(result.reviewCompilationUnits, link.compilationUnit);
            }
        }
        return result;
    }

    if (anyProtected && !anyUnprotected) {
        result.verdict = Verdict::Confirmed;
        result.reason = anySelf ? Reason::SelfProtected
                                : Reason::InlinedCallerProtected;
        // Confirmed findings are not queued for re-review, but keep the
        // carrying compilation unit available in the evidence chain itself.
        return result;
    }

    // Every attributed compiled function is unprotected.
    result.verdict = Verdict::Violation;
    if (allInlined) {
        result.reason = Reason::UnprotectedCallers;
    } else if (anySelf) {
        result.reason = Reason::UnprotectedSelf;
    } else {
        result.reason = Reason::UnprotectedCallers;
    }
    for (const auto& link : result.evidence) {
        addUnique(result.reviewCompilationUnits, link.compilationUnit);
    }
    return result;
}

}  // namespace

CoverageReport buildCoverageReport(
    const std::vector<RiskEntry>& risks,
    const std::vector<AttributionEntry>& attributions,
    const std::vector<ProtectionMark>& marks,
    const std::vector<std::string>& configs) {
    CoverageReport report;
    report.configs = configs;

    std::unordered_map<std::string, MarkLookup> markIndex;
    markIndex.reserve(marks.size());
    for (const auto& mark : marks) {
        markIndex[markKey(mark.config, mark.compiledFunction, mark.compilationUnit)] =
            MarkLookup{&mark};
    }

    std::set<std::string> requestedConfigs(configs.begin(), configs.end());
    std::set<std::string> seenRecordConfigs;
    for (const auto& row : attributions) seenRecordConfigs.insert(row.config);
    for (const auto& row : marks) seenRecordConfigs.insert(row.config);
    for (const auto& name : seenRecordConfigs) {
        if (!requestedConfigs.count(name)) {
            report.warnings.push_back(
                "input records reference undeclared config '" + name +
                "'; those records are ignored");
        }
    }

    std::map<std::string, ConfigSummary> summaryByName;
    for (const auto& config : configs) {
        summaryByName[config] = ConfigSummary{config, 0, 0, 0, 0, 0};
    }

    for (const auto& config : configs) {
        for (const auto& risk : risks) {
            FunctionVerdict verdict = analyzeOne(risk, config, attributions, markIndex);
            auto& summary = summaryByName[config];
            ++summary.total;
            switch (verdict.verdict) {
                case Verdict::Confirmed:   ++summary.confirmed; break;
                case Verdict::Violation:   ++summary.violation; break;
                case Verdict::Unknown:     ++summary.unknown; break;
                case Verdict::NotRequired: ++summary.notRequired; break;
            }
            report.verdicts.push_back(std::move(verdict));
        }
    }
    for (const auto& config : configs) {
        report.summaries.push_back(summaryByName[config]);
    }

    // Adjacent-config regression/improvement differences.
    if (configs.size() >= 2) {
        const auto indexOf = [&](const std::string& config, const std::string& riskId) {
            for (size_t i = 0; i < report.verdicts.size(); ++i) {
                if (report.verdicts[i].config == config &&
                    report.verdicts[i].riskId == riskId) {
                    return i;
                }
            }
            return static_cast<size_t>(-1);
        };
        for (size_t pairIndex = 0; pairIndex + 1 < configs.size(); ++pairIndex) {
            const std::string& base = configs[pairIndex];
            const std::string& next = configs[pairIndex + 1];
            for (const auto& risk : risks) {
                const size_t left = indexOf(base, risk.riskId);
                const size_t right = indexOf(next, risk.riskId);
                if (left == static_cast<size_t>(-1) || right == static_cast<size_t>(-1)) {
                    continue;
                }
                const FunctionVerdict& a = report.verdicts[left];
                const FunctionVerdict& b = report.verdicts[right];
                if (a.verdict != b.verdict || a.reason != b.reason) {
                    report.diffs.push_back(
                        CoverageDiff{risk.sourceFunction, risk.riskId, base, next,
                                     a.verdict, a.reason, b.verdict, b.reason});
                }
            }
        }
        std::sort(report.diffs.begin(), report.diffs.end(),
                  [](const CoverageDiff& lhs, const CoverageDiff& rhs) {
                      if (lhs.riskId != rhs.riskId) return lhs.riskId < rhs.riskId;
                      if (lhs.baselineConfig != rhs.baselineConfig) {
                          return lhs.baselineConfig < rhs.baselineConfig;
                      }
                      return lhs.comparedConfig < rhs.comparedConfig;
                  });
    }

    // Distinct compilation units requiring human re-review.
    std::map<std::pair<std::string, std::string>, CompilationUnitReview> reviewIndex;
    for (const auto& verdict : report.verdicts) {
        if (verdict.verdict != Verdict::Unknown && verdict.verdict != Verdict::Violation) {
            continue;
        }
        for (const auto& unit : verdict.reviewCompilationUnits) {
            auto key = std::make_pair(verdict.config, unit);
            auto it = reviewIndex.find(key);
            if (it == reviewIndex.end()) {
                CompilationUnitReview review;
                review.compilationUnit = unit;
                review.config = verdict.config;
                it = reviewIndex.emplace(key, std::move(review)).first;
            }
            if (verdict.verdict == Verdict::Unknown) {
                ++it->second.unknownCount;
            } else {
                ++it->second.violationCount;
            }
        }
    }
    for (const auto& entry : reviewIndex) {
        report.unitsToReview.push_back(entry.second);
    }
    std::sort(report.unitsToReview.begin(), report.unitsToReview.end(),
              [](const CompilationUnitReview& lhs, const CompilationUnitReview& rhs) {
                  if (lhs.config != rhs.config) return lhs.config < rhs.config;
                  if (lhs.compilationUnit != rhs.compilationUnit) {
                      return lhs.compilationUnit < rhs.compilationUnit;
                  }
                  return false;
              });

    return report;
}

}  // namespace ssp
