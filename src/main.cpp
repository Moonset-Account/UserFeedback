// ssp-cover-check: SEC-VULN-013 function-level stack-protection coverage checker.
//
// Usage:
//   ssp-cover-check \
//     --risk-list <path> \
//     --bundle <config>=<attribution_path>,<marks_path> [--bundle ...] \
//     [--report <path>] [--strict]
//
// Exit codes:
//   0 analysis completed (in --strict mode: no VIOLATION and no UNKNOWN)
//   1 input / parse / usage error
//   3 --strict mode: at least one VIOLATION
//   4 --strict mode: no violation but at least one UNKNOWN
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include "ssp_checker/Analysis.h"
#include "ssp_checker/Parser.h"
#include "ssp_checker/Report.h"

using namespace ssp;

namespace {

struct BundleArg {
    std::string config;
    std::string attributionPath;
    std::string marksPath;
};

void printUsage(std::ostream& output) {
    output << "usage: ssp-cover-check --risk-list <path> "
              "--bundle <config>=<attribution>,<marks> [--bundle ...] "
              "[--report <path>] [--strict]\n";
}

bool readFile(const std::string& path, std::ifstream& stream) {
    stream.open(path);
    return stream.is_open();
}

void printParseErrors(const std::string& path, const ParseOutcome& outcome) {
    for (const auto& error : outcome.errors) {
        std::cerr << "parse error in " << path << " at line " << error.line << ": "
                  << error.message << "\n";
    }
}

bool parseBundle(const std::string& value, BundleArg& out) {
    const auto equal = value.find('=');
    if (equal == std::string::npos || equal == 0) return false;
    out.config = value.substr(0, equal);
    const std::string rest = value.substr(equal + 1);
    const auto comma = rest.find(',');
    if (comma == std::string::npos || comma == 0 || comma + 1 >= rest.size()) {
        return false;
    }
    out.attributionPath = rest.substr(0, comma);
    out.marksPath = rest.substr(comma + 1);
    return !out.config.empty() && !out.attributionPath.empty() && !out.marksPath.empty();
}

}  // namespace

int main(int argc, char** argv) {
    std::string riskListPath;
    std::string reportPath;
    bool strict = false;
    std::vector<BundleArg> bundles;

    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--help" || arg == "-h") {
            printUsage(std::cout);
            return 0;
        }
        if (arg == "--strict") {
            strict = true;
            continue;
        }
        if (arg == "--risk-list") {
            if (++i >= argc) {
                std::cerr << "missing value for --risk-list\n";
                return 1;
            }
            riskListPath = argv[i];
        } else if (arg == "--report") {
            if (++i >= argc) {
                std::cerr << "missing value for --report\n";
                return 1;
            }
            reportPath = argv[i];
        } else if (arg == "--bundle") {
            if (++i >= argc) {
                std::cerr << "missing value for --bundle\n";
                return 1;
            }
            BundleArg bundle;
            if (!parseBundle(argv[i], bundle)) {
                std::cerr << "invalid --bundle '" << argv[i]
                          << "', expected <config>=<attribution>,<marks>\n";
                return 1;
            }
            bundles.push_back(std::move(bundle));
        } else {
            std::cerr << "unknown argument: " << arg << "\n";
            printUsage(std::cerr);
            return 1;
        }
    }

    if (riskListPath.empty() || bundles.empty()) {
        printUsage(std::cerr);
        return 1;
    }

    std::ifstream riskStream;
    if (!readFile(riskListPath, riskStream)) {
        std::cerr << "cannot open risk list: " << riskListPath << "\n";
        return 1;
    }
    std::vector<RiskEntry> risks;
    const ParseOutcome riskOutcome = parseRiskList(riskStream, risks);
    if (!riskOutcome.ok()) {
        printParseErrors(riskListPath, riskOutcome);
        return 1;
    }
    if (risks.empty()) {
        std::cerr << "risk list " << riskListPath << " contains no entries\n";
        return 1;
    }

    std::vector<std::string> configs;
    std::vector<AttributionEntry> allAttributions;
    std::vector<ProtectionMark> allMarks;

    for (const auto& bundle : bundles) {
        if (std::find(configs.begin(), configs.end(), bundle.config) != configs.end()) {
            std::cerr << "duplicate config name: " << bundle.config << "\n";
            return 1;
        }
        configs.push_back(bundle.config);

        std::ifstream attributionStream;
        if (!readFile(bundle.attributionPath, attributionStream)) {
            std::cerr << "cannot open attribution map: " << bundle.attributionPath << "\n";
            return 1;
        }
        const ParseOutcome attributionOutcome =
            parseAttribution(attributionStream, allAttributions);
        if (!attributionOutcome.ok()) {
            printParseErrors(bundle.attributionPath, attributionOutcome);
            return 1;
        }

        std::ifstream marksStream;
        if (!readFile(bundle.marksPath, marksStream)) {
            std::cerr << "cannot open protection marks: " << bundle.marksPath << "\n";
            return 1;
        }
        const ParseOutcome marksOutcome = parseProtectionMarks(marksStream, allMarks);
        if (!marksOutcome.ok()) {
            printParseErrors(bundle.marksPath, marksOutcome);
            return 1;
        }
    }

    const CoverageReport report =
        buildCoverageReport(risks, allAttributions, allMarks, configs);

    std::ofstream reportFile;
    std::ostream* output = &std::cout;
    if (!reportPath.empty()) {
        reportFile.open(reportPath);
        if (!reportFile.is_open()) {
            std::cerr << "cannot open report output: " << reportPath << "\n";
            return 1;
        }
        output = &reportFile;
    }
    renderReport(report, *output);
    output->flush();

    if (!strict) return 0;

    bool hasViolation = false;
    bool hasUnknown = false;
    for (const auto& verdict : report.verdicts) {
        if (verdict.verdict == Verdict::Violation) hasViolation = true;
        if (verdict.verdict == Verdict::Unknown) hasUnknown = true;
    }
    if (hasViolation) return 3;
    if (hasUnknown) return 4;
    return 0;
}
