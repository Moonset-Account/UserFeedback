#include "ssp_checker/Parser.h"

#include <algorithm>
#include <set>
#include <sstream>

namespace ssp {
namespace {

std::string trim(const std::string& value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return "";
    const auto last = value.find_last_not_of(" \t\r\n");
    return value.substr(first, last - first + 1);
}

std::vector<std::string> splitFields(const std::string& line) {
    std::vector<std::string> fields;
    std::string current;
    std::stringstream stream(line);
    while (std::getline(stream, current, '|')) {
        fields.push_back(trim(current));
    }
    return fields;
}

bool parseRiskLevel(const std::string& token, RiskLevel& out) {
    if (token == "low") {
        out = RiskLevel::Low;
    } else if (token == "medium") {
        out = RiskLevel::Medium;
    } else if (token == "high") {
        out = RiskLevel::High;
    } else {
        return false;
    }
    return true;
}

bool parseRequirement(const std::string& token, Requirement& out) {
    if (token == "required") {
        out = Requirement::Required;
    } else if (token == "exempt_leaf") {
        out = Requirement::ExemptLeaf;
    } else {
        return false;
    }
    return true;
}

bool parseRelation(const std::string& token, Relation& out) {
    if (token == "self") {
        out = Relation::Self;
    } else if (token == "inlined") {
        out = Relation::Inlined;
    } else {
        return false;
    }
    return true;
}

bool parseMark(const std::string& token, Mark& out) {
    if (token == "protected") {
        out = Mark::Protected;
    } else if (token == "unprotected") {
        out = Mark::Unprotected;
    } else {
        return false;
    }
    return true;
}

}  // namespace

ParseOutcome parseRiskList(std::istream& input, std::vector<RiskEntry>& out) {
    ParseOutcome outcome;
    std::string line;
    size_t lineNumber = 0;
    std::set<std::string> seenIds;
    while (std::getline(input, line)) {
        ++lineNumber;
        const std::string stripped = trim(line);
        if (stripped.empty() || stripped[0] == '#') continue;

        const auto fields = splitFields(line);
        if (fields.size() != 6) {
            outcome.errors.push_back(
                {lineNumber, "risk list row must have 6 '|'-separated fields, got " +
                                 std::to_string(fields.size())});
            continue;
        }
        RiskEntry entry;
        entry.riskId = fields[0];
        entry.sourceFunction = fields[1];
        entry.compilationUnit = fields[2];
        entry.note = fields[5];
        if (entry.riskId.empty() || entry.sourceFunction.empty() ||
            entry.compilationUnit.empty()) {
            outcome.errors.push_back(
                {lineNumber, "risk_id, source_function and compilation_unit must be non-empty"});
            continue;
        }
        if (!parseRiskLevel(fields[3], entry.level)) {
            outcome.errors.push_back(
                {lineNumber, "unknown risk_level '" + fields[3] + "' (expected low|medium|high)"});
            continue;
        }
        if (!parseRequirement(fields[4], entry.requirement)) {
            outcome.errors.push_back({lineNumber,
                                      "unknown requirement '" + fields[4] +
                                          "' (expected required|exempt_leaf)"});
            continue;
        }
        if (!seenIds.insert(entry.riskId).second) {
            outcome.errors.push_back(
                {lineNumber, "duplicate risk_id '" + entry.riskId + "'"});
            continue;
        }
        out.push_back(std::move(entry));
    }
    return outcome;
}

ParseOutcome parseAttribution(std::istream& input, std::vector<AttributionEntry>& out) {
    ParseOutcome outcome;
    std::string line;
    size_t lineNumber = 0;
    std::set<std::string> seenIds;
    std::set<std::string> seenKeys;
    while (std::getline(input, line)) {
        ++lineNumber;
        const std::string stripped = trim(line);
        if (stripped.empty() || stripped[0] == '#') continue;

        const auto fields = splitFields(line);
        if (fields.size() != 6) {
            outcome.errors.push_back(
                {lineNumber, "attribution row must have 6 '|'-separated fields, got " +
                                 std::to_string(fields.size())});
            continue;
        }
        AttributionEntry entry;
        entry.config = fields[0];
        entry.sourceFunction = fields[1];
        entry.compiledFunction = fields[2];
        entry.compilationUnit = fields[3];
        entry.attributionId = fields[5];
        if (entry.config.empty() || entry.sourceFunction.empty() ||
            entry.compiledFunction.empty() || entry.compilationUnit.empty() ||
            entry.attributionId.empty()) {
            outcome.errors.push_back(
                {lineNumber, "all attribution fields except none must be non-empty"});
            continue;
        }
        if (!parseRelation(fields[4], entry.relation)) {
            outcome.errors.push_back({lineNumber,
                                      "unknown relation '" + fields[4] +
                                          "' (expected self|inlined)"});
            continue;
        }
        if (!seenIds.insert(entry.attributionId).second) {
            outcome.errors.push_back(
                {lineNumber, "duplicate attribution_id '" + entry.attributionId + "'"});
            continue;
        }
        const std::string key = entry.config + "|" + entry.sourceFunction + "|" +
                                entry.compiledFunction + "|" + entry.compilationUnit;
        if (!seenKeys.insert(key).second) {
            outcome.errors.push_back(
                {lineNumber, "duplicate attribution row for key '" + key + "'"});
            continue;
        }
        out.push_back(std::move(entry));
    }
    return outcome;
}

ParseOutcome parseProtectionMarks(std::istream& input, std::vector<ProtectionMark>& out) {
    ParseOutcome outcome;
    std::string line;
    size_t lineNumber = 0;
    std::set<std::string> seenIds;
    std::set<std::string> seenKeys;
    while (std::getline(input, line)) {
        ++lineNumber;
        const std::string stripped = trim(line);
        if (stripped.empty() || stripped[0] == '#') continue;

        const auto fields = splitFields(line);
        if (fields.size() != 6) {
            outcome.errors.push_back(
                {lineNumber, "protection mark row must have 6 '|'-separated fields, got " +
                                 std::to_string(fields.size())});
            continue;
        }
        ProtectionMark entry;
        entry.config = fields[0];
        entry.compiledFunction = fields[1];
        entry.compilationUnit = fields[2];
        entry.protectorKind = fields[4];
        entry.markId = fields[5];
        if (entry.config.empty() || entry.compiledFunction.empty() ||
            entry.compilationUnit.empty() || entry.markId.empty()) {
            outcome.errors.push_back(
                {lineNumber, "config, compiled_function, compilation_unit and mark_id are required"});
            continue;
        }
        if (!parseMark(fields[3], entry.mark)) {
            outcome.errors.push_back({lineNumber,
                                      "unknown mark '" + fields[3] +
                                          "' (expected protected|unprotected)"});
            continue;
        }
        if (!seenIds.insert(entry.markId).second) {
            outcome.errors.push_back(
                {lineNumber, "duplicate mark_id '" + entry.markId + "'"});
            continue;
        }
        const std::string key =
            entry.config + "|" + entry.compiledFunction + "|" + entry.compilationUnit;
        if (!seenKeys.insert(key).second) {
            outcome.errors.push_back(
                {lineNumber, "conflicting duplicate mark for key '" + key + "'"});
            continue;
        }
        out.push_back(std::move(entry));
    }
    return outcome;
}

}  // namespace ssp
