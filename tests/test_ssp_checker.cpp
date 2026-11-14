// Native SEC-VULN-013 checker tests (C++17, standard library only).
// Each test carries a stable case_id and asserts concrete inputs, actual
// outputs and expected values. No network, no third-party libraries.
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "ssp_checker/Analysis.h"
#include "ssp_checker/Parser.h"
#include "ssp_checker/Report.h"

using namespace ssp;

namespace {

int g_failures = 0;
int g_checks = 0;
std::vector<std::string> g_runningCase;

void beginCase(const std::string& caseId, const std::string& title) {
    g_runningCase.push_back(caseId);
    std::cout << "[ RUN      ] " << caseId << " " << title << "\n";
}

void endCase() {
    std::cout << "[       OK ] " << g_runningCase.back() << "\n";
    g_runningCase.pop_back();
}

void fail(const std::string& message, const char* file, int line) {
    ++g_failures;
    std::cout << "  ASSERT FAIL (" << g_runningCase.back() << ") " << file << ":" << line
              << ": " << message << "\n";
}

#define CHECK_TRUE(cond)                                                       \
    do {                                                                       \
        ++g_checks;                                                            \
        if (!(cond)) fail(std::string("expected true: ") + #cond, __FILE__,   \
                          __LINE__);                                           \
    } while (0)

#define CHECK_EQ(actual, expected)                                             \
    do {                                                                       \
        ++g_checks;                                                            \
        const auto actualValue = (actual);                                     \
        const auto expectedValue = (expected);                                 \
        if (!(actualValue == expectedValue)) {                                 \
            std::ostringstream oss;                                            \
            oss << #actual << " == " << #expected << " (actual='"              \
                << actualValue << "' expected='" << expectedValue << "')";     \
            fail(oss.str(), __FILE__, __LINE__);                               \
        }                                                                      \
    } while (0)

RiskEntry risk(const std::string& id, const std::string& fn, const std::string& cu,
               Requirement requirement = Requirement::Required) {
    RiskEntry entry;
    entry.riskId = id;
    entry.sourceFunction = fn;
    entry.compilationUnit = cu;
    entry.level = RiskLevel::High;
    entry.requirement = requirement;
    return entry;
}

AttributionEntry attr(const std::string& config, const std::string& source,
                      const std::string& compiled, const std::string& cu,
                      Relation relation, const std::string& id) {
    AttributionEntry entry;
    entry.config = config;
    entry.sourceFunction = source;
    entry.compiledFunction = compiled;
    entry.compilationUnit = cu;
    entry.relation = relation;
    entry.attributionId = id;
    return entry;
}

ProtectionMark mark(const std::string& config, const std::string& compiled,
                    const std::string& cu, Mark value, const std::string& id) {
    ProtectionMark entry;
    entry.config = config;
    entry.compiledFunction = compiled;
    entry.compilationUnit = cu;
    entry.mark = value;
    entry.protectorKind = value == Mark::Protected ? "ssp-strong" : "-";
    entry.markId = id;
    return entry;
}

const FunctionVerdict& findVerdict(const CoverageReport& report, const std::string& config,
                                   const std::string& riskId) {
    for (const auto& verdict : report.verdicts) {
        if (verdict.config == config && verdict.riskId == riskId) return verdict;
    }
    static const FunctionVerdict empty;
    return empty;
}

// ---- parser tests -------------------------------------------------------

void testParseRiskList() {
    beginCase("TC-PARSE-001", "risk list rows parse into typed records");
    std::istringstream input(
        "# comment\n"
        "R1 | alpha | a.c | high | required | note one\n"
        "\n"
        "R2|beta|b.c|low|exempt_leaf|leaf\n");
    std::vector<RiskEntry> entries;
    const auto outcome = parseRiskList(input, entries);
    CHECK_TRUE(outcome.ok());
    CHECK_EQ(entries.size(), static_cast<size_t>(2));
    CHECK_EQ(entries[0].riskId, std::string("R1"));
    CHECK_EQ(entries[0].sourceFunction, std::string("alpha"));
    CHECK_EQ(entries[0].compilationUnit, std::string("a.c"));
    CHECK_TRUE(entries[0].level == RiskLevel::High);
    CHECK_TRUE(entries[0].requirement == Requirement::Required);
    CHECK_EQ(entries[0].note, std::string("note one"));
    CHECK_TRUE(entries[1].requirement == Requirement::ExemptLeaf);
    endCase();
}

void testParseErrors() {
    beginCase("TC-PARSE-002", "malformed rows report the offending line number");
    std::istringstream input(
        "R1|alpha|a.c|high|required|ok\n"
        "R2|beta|b.c|high|required\n"           // only 5 fields
        "R3|gamma|c.c|extreme|required|x\n");   // bad enum
    std::vector<RiskEntry> entries;
    const auto outcome = parseRiskList(input, entries);
    CHECK_TRUE(!outcome.ok());
    CHECK_EQ(outcome.errors.size(), static_cast<size_t>(2));
    CHECK_EQ(outcome.errors[0].line, static_cast<size_t>(2));
    CHECK_EQ(outcome.errors[1].line, static_cast<size_t>(3));
    CHECK_EQ(entries.size(), static_cast<size_t>(1));
    endCase();
}

void testParseDuplicates() {
    beginCase("TC-PARSE-003", "duplicate ids and conflicting marks are rejected");
    std::istringstream riskInput("R1|alpha|a.c|high|required|x\n"
                                 "R1|beta|b.c|high|required|y\n");
    std::vector<RiskEntry> risks;
    const auto riskOutcome = parseRiskList(riskInput, risks);
    CHECK_TRUE(!riskOutcome.ok());
    CHECK_EQ(riskOutcome.errors.size(), static_cast<size_t>(1));

    std::istringstream markInput(
        "O2|f|a.c|protected|ssp|M1\n"
        "O2|f|a.c|unprotected|-|M2\n");
    std::vector<ProtectionMark> marks;
    const auto markOutcome = parseProtectionMarks(markInput, marks);
    CHECK_TRUE(!markOutcome.ok());
    CHECK_EQ(markOutcome.errors.size(), static_cast<size_t>(1));
    endCase();
}

// ---- engine decision tests ---------------------------------------------

void testSelfProtected() {
    beginCase("TC-SELF-001", "retained self function with protected mark is confirmed");
    std::vector<RiskEntry> risks{risk("R1", "alpha", "a.c")};
    std::vector<AttributionEntry> rows{
        attr("O2", "alpha", "alpha", "a.c", Relation::Self, "A1")};
    std::vector<ProtectionMark> marks{
        mark("O2", "alpha", "a.c", Mark::Protected, "M1")};
    const auto report = buildCoverageReport(risks, rows, marks, {"O2"});
    const auto& v = findVerdict(report, "O2", "R1");
    CHECK_TRUE(v.verdict == Verdict::Confirmed);
    CHECK_TRUE(v.reason == Reason::SelfProtected);
    CHECK_EQ(v.evidence.size(), static_cast<size_t>(1));
    CHECK_EQ(v.evidence[0].attributionId, std::string("A1"));
    CHECK_EQ(v.evidence[0].markId, std::string("M1"));
    CHECK_TRUE(v.evidence[0].hasMark);
    CHECK_TRUE(v.reviewCompilationUnits.empty());
    endCase();
}

void testInlinedCallerProtected() {
    beginCase("TC-INLINE-001", "inlined source carried by one protected caller is confirmed");
    std::vector<RiskEntry> risks{risk("R1", "helper", "a.c")};
    std::vector<AttributionEntry> rows{
        attr("O2", "helper", "caller", "b.c", Relation::Inlined, "A1")};
    std::vector<ProtectionMark> marks{
        mark("O2", "caller", "b.c", Mark::Protected, "M9")};
    const auto report = buildCoverageReport(risks, rows, marks, {"O2"});
    const auto& v = findVerdict(report, "O2", "R1");
    CHECK_TRUE(v.verdict == Verdict::Confirmed);
    CHECK_TRUE(v.reason == Reason::InlinedCallerProtected);
    CHECK_EQ(v.evidence[0].compiledFunction, std::string("caller"));
    CHECK_EQ(v.evidence[0].markId, std::string("M9"));
    endCase();
}

void testPartialCallerProtection() {
    beginCase("TC-PARTIAL-001", "multiple callers with only partial protection stay unknown");
    std::vector<RiskEntry> risks{risk("R1", "helper", "a.c")};
    std::vector<AttributionEntry> rows{
        attr("O2", "helper", "caller_a", "a.c", Relation::Inlined, "A1"),
        attr("O2", "helper", "caller_b", "b.c", Relation::Inlined, "A2")};
    std::vector<ProtectionMark> marks{
        mark("O2", "caller_a", "a.c", Mark::Protected, "M1"),
        mark("O2", "caller_b", "b.c", Mark::Unprotected, "M2")};
    const auto report = buildCoverageReport(risks, rows, marks, {"O2"});
    const auto& v = findVerdict(report, "O2", "R1");
    CHECK_TRUE(v.verdict == Verdict::Unknown);
    CHECK_TRUE(v.reason == Reason::PartialCallerProtection);
    CHECK_EQ(v.evidence.size(), static_cast<size_t>(2));
    // Only the unprotected carrying unit is queued for re-review.
    CHECK_EQ(v.reviewCompilationUnits.size(), static_cast<size_t>(1));
    CHECK_EQ(v.reviewCompilationUnits[0], std::string("b.c"));
    endCase();
}

void testMissingAttribution() {
    beginCase("TC-MISSING-ATTR-001", "absent build attribution is unknown, not assumed safe");
    std::vector<RiskEntry> risks{risk("R1", "ghost", "ghost.c")};
    const auto report = buildCoverageReport(risks, {}, {}, {"O2"});
    const auto& v = findVerdict(report, "O2", "R1");
    CHECK_TRUE(v.verdict == Verdict::Unknown);
    CHECK_TRUE(v.reason == Reason::MissingAttribution);
    CHECK_TRUE(v.evidence.empty());
    CHECK_EQ(v.reviewCompilationUnits.size(), static_cast<size_t>(1));
    CHECK_EQ(v.reviewCompilationUnits[0], std::string("ghost.c"));
    endCase();
}

void testMissingProtectionMark() {
    beginCase("TC-MISSING-MARK-001", "attributed function without exported mark is unknown");
    std::vector<RiskEntry> risks{risk("R1", "alpha", "a.c")};
    std::vector<AttributionEntry> rows{
        attr("O2", "alpha", "alpha", "a.c", Relation::Self, "A1")};
    const auto report = buildCoverageReport(risks, rows, {}, {"O2"});
    const auto& v = findVerdict(report, "O2", "R1");
    CHECK_TRUE(v.verdict == Verdict::Unknown);
    CHECK_TRUE(v.reason == Reason::MissingProtectionMark);
    CHECK_TRUE(!v.evidence[0].hasMark);
    CHECK_EQ(v.reviewCompilationUnits[0], std::string("a.c"));
    endCase();
}

void testUnprotectedSelf() {
    beginCase("TC-VIOL-SELF-001", "unprotected self record is a violation");
    std::vector<RiskEntry> risks{risk("R1", "alpha", "a.c")};
    std::vector<AttributionEntry> rows{
        attr("O2", "alpha", "alpha", "a.c", Relation::Self, "A1")};
    std::vector<ProtectionMark> marks{
        mark("O2", "alpha", "a.c", Mark::Unprotected, "M1")};
    const auto report = buildCoverageReport(risks, rows, marks, {"O2"});
    const auto& v = findVerdict(report, "O2", "R1");
    CHECK_TRUE(v.verdict == Verdict::Violation);
    CHECK_TRUE(v.reason == Reason::UnprotectedSelf);
    endCase();
}

void testUnprotectedCallers() {
    beginCase("TC-VIOL-CALLER-001", "all inlining callers unprotected is a violation");
    std::vector<RiskEntry> risks{risk("R1", "helper", "a.c")};
    std::vector<AttributionEntry> rows{
        attr("O2", "helper", "caller_a", "a.c", Relation::Inlined, "A1"),
        attr("O2", "helper", "caller_b", "b.c", Relation::Inlined, "A2")};
    std::vector<ProtectionMark> marks{
        mark("O2", "caller_a", "a.c", Mark::Unprotected, "M1"),
        mark("O2", "caller_b", "b.c", Mark::Unprotected, "M2")};
    const auto report = buildCoverageReport(risks, rows, marks, {"O2"});
    const auto& v = findVerdict(report, "O2", "R1");
    CHECK_TRUE(v.verdict == Verdict::Violation);
    CHECK_TRUE(v.reason == Reason::UnprotectedCallers);
    CHECK_EQ(v.reviewCompilationUnits.size(), static_cast<size_t>(2));
    endCase();
}

void testAllProtectedMultipleCallers() {
    beginCase("TC-INLINE-002", "several protected callers together confirm the source item");
    std::vector<RiskEntry> risks{risk("R1", "helper", "a.c")};
    std::vector<AttributionEntry> rows{
        attr("O2", "helper", "caller_a", "a.c", Relation::Inlined, "A1"),
        attr("O2", "helper", "caller_b", "b.c", Relation::Inlined, "A2")};
    std::vector<ProtectionMark> marks{
        mark("O2", "caller_a", "a.c", Mark::Protected, "M1"),
        mark("O2", "caller_b", "b.c", Mark::Protected, "M2")};
    const auto report = buildCoverageReport(risks, rows, marks, {"O2"});
    const auto& v = findVerdict(report, "O2", "R1");
    CHECK_TRUE(v.verdict == Verdict::Confirmed);
    CHECK_TRUE(v.reason == Reason::InlinedCallerProtected);
    endCase();
}

void testExemptLeaf() {
    beginCase("TC-EXEMPT-001", "policy-exempt leaf is not required even without any records");
    std::vector<RiskEntry> risks{
        risk("R1", "byte_swap", "u.c", Requirement::ExemptLeaf)};
    const auto report = buildCoverageReport(risks, {}, {}, {"O2"});
    const auto& v = findVerdict(report, "O2", "R1");
    CHECK_TRUE(v.verdict == Verdict::NotRequired);
    CHECK_TRUE(v.reason == Reason::ExemptLeaf);
    CHECK_TRUE(v.evidence.empty());
    CHECK_TRUE(v.reviewCompilationUnits.empty());
    endCase();
}

void testCrossConfigRegression() {
    beginCase("TC-DIFF-001", "optimization-driven regressions surface as config differences");
    std::vector<RiskEntry> risks{
        risk("R1", "alpha", "a.c"),
        risk("R2", "helper", "h.c")};
    std::vector<AttributionEntry> rows{
        attr("O0", "alpha", "alpha", "a.c", Relation::Self, "A1"),
        attr("O0", "helper", "helper", "h.c", Relation::Self, "A2"),
        attr("O2", "alpha", "alpha", "a.c", Relation::Self, "A3"),
        attr("O2", "helper", "caller", "c.c", Relation::Inlined, "A4")};
    std::vector<ProtectionMark> marks{
        mark("O0", "alpha", "a.c", Mark::Protected, "M1"),
        mark("O0", "helper", "h.c", Mark::Protected, "M2"),
        mark("O2", "alpha", "a.c", Mark::Unprotected, "M3"),
        mark("O2", "caller", "c.c", Mark::Protected, "M4")};
    const auto report = buildCoverageReport(risks, rows, marks, {"O0", "O2"});

    CHECK_EQ(report.summaries.size(), static_cast<size_t>(2));
    CHECK_EQ(report.summaries[0].confirmed, 2);
    CHECK_EQ(report.summaries[1].violation, 1);
    CHECK_EQ(report.summaries[1].confirmed, 1);

    const auto& alphaO2 = findVerdict(report, "O2", "R1");
    CHECK_TRUE(alphaO2.verdict == Verdict::Violation);
    const auto& helperO2 = findVerdict(report, "O2", "R2");
    CHECK_TRUE(helperO2.reason == Reason::InlinedCallerProtected);

    // R1 changes verdict; R2 keeps CONFIRMED but changes reason. Both are diffs.
    CHECK_EQ(report.diffs.size(), static_cast<size_t>(2));
    bool sawVerdictRegression = false;
    bool sawReasonOnlyChange = false;
    for (const auto& diff : report.diffs) {
        if (diff.riskId == "R1") {
            sawVerdictRegression =
                diff.baselineVerdict == Verdict::Confirmed &&
                diff.comparedVerdict == Verdict::Violation;
        }
        if (diff.riskId == "R2") {
            sawReasonOnlyChange =
                diff.baselineVerdict == Verdict::Confirmed &&
                diff.comparedVerdict == Verdict::Confirmed &&
                diff.baselineReason == Reason::SelfProtected &&
                diff.comparedReason == Reason::InlinedCallerProtected;
        }
    }
    CHECK_TRUE(sawVerdictRegression);
    CHECK_TRUE(sawReasonOnlyChange);

    CHECK_EQ(report.unitsToReview.size(), static_cast<size_t>(1));
    CHECK_EQ(report.unitsToReview[0].compilationUnit, std::string("a.c"));
    CHECK_EQ(report.unitsToReview[0].config, std::string("O2"));
    CHECK_EQ(report.unitsToReview[0].violationCount, 1);
    endCase();
}

void testReportRendering() {
    beginCase("TC-REPORT-001", "rendered report exposes stable machine-readable prefixes");
    std::vector<RiskEntry> risks{risk("R1", "alpha", "a.c")};
    std::vector<AttributionEntry> rows{
        attr("O2", "alpha", "alpha", "a.c", Relation::Self, "A1")};
    std::vector<ProtectionMark> marks{
        mark("O2", "alpha", "a.c", Mark::Unprotected, "M1")};
    const auto report = buildCoverageReport(risks, rows, marks, {"O2"});
    std::ostringstream output;
    renderReport(report, output);
    const std::string text = output.str();
    CHECK_TRUE(text.find("V | config=O2 risk_id=R1") != std::string::npos);
    CHECK_TRUE(text.find("verdict=VIOLATION reason=unprotected_self") !=
               std::string::npos);
    CHECK_TRUE(text.find("S | config=O2") != std::string::npos);
    CHECK_TRUE(text.find("U | config=O2 cu=a.c") != std::string::npos);
    CHECK_TRUE(text.find("A1 (self) alpha -> alpha @ a.c") != std::string::npos);
    CHECK_TRUE(text.find("via M1") != std::string::npos);
    endCase();
}

void testUndeclaredConfigWarning() {
    beginCase("TC-WARN-001", "records under undeclared configs are ignored and warned about");
    std::vector<RiskEntry> risks{risk("R1", "alpha", "a.c")};
    std::vector<AttributionEntry> rows{
        attr("O3", "alpha", "alpha", "a.c", Relation::Self, "A1")};
    std::vector<ProtectionMark> marks{
        mark("O3", "alpha", "a.c", Mark::Protected, "M1")};
    const auto report = buildCoverageReport(risks, rows, marks, {"O2"});
    const auto& v = findVerdict(report, "O2", "R1");
    CHECK_TRUE(v.verdict == Verdict::Unknown);
    CHECK_TRUE(v.reason == Reason::MissingAttribution);
    CHECK_EQ(report.warnings.size(), static_cast<size_t>(1));
    endCase();
}

}  // namespace

int main() {
    testParseRiskList();
    testParseErrors();
    testParseDuplicates();
    testSelfProtected();
    testInlinedCallerProtected();
    testPartialCallerProtection();
    testMissingAttribution();
    testMissingProtectionMark();
    testUnprotectedSelf();
    testUnprotectedCallers();
    testAllProtectedMultipleCallers();
    testExemptLeaf();
    testCrossConfigRegression();
    testReportRendering();
    testUndeclaredConfigWarning();

    std::cout << "\n=== test summary: 15 cases, " << g_checks << " assertions, "
              << g_failures << " failure(s) ===\n";
    return g_failures == 0 ? 0 : 1;
}
