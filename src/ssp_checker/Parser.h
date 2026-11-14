// Offline record parsers for the SEC-VULN-013 coverage checker.
// Input grammar (all three file types share the same line conventions):
//   * lines starting with '#' are comments; blank lines are ignored
//   * fields are separated by '|' and surrounding whitespace is trimmed
//   * every non-comment line must carry the exact number of fields below
//
// risk list        (6 fields):
//   risk_id | source_function | compilation_unit | risk_level | requirement | note
//   risk_level   in {low, medium, high}
//   requirement  in {required, exempt_leaf}
//
// attribution map  (6 fields):
//   config | source_function | compiled_function | compilation_unit | relation | attribution_id
//   relation     in {self, inlined}
//
// protection marks (6 fields):
//   config | compiled_function | compilation_unit | mark | protector_kind | mark_id
//   mark         in {protected, unprotected}
#pragma once

#include <istream>
#include <string>
#include <vector>

#include "ssp_checker/Types.h"

namespace ssp {

struct ParseError {
    size_t line = 0;
    std::string message;
};

struct ParseOutcome {
    std::vector<ParseError> errors;
    bool ok() const { return errors.empty(); }
};

ParseOutcome parseRiskList(std::istream& input, std::vector<RiskEntry>& out);
ParseOutcome parseAttribution(std::istream& input, std::vector<AttributionEntry>& out);
ParseOutcome parseProtectionMarks(std::istream& input, std::vector<ProtectionMark>& out);

}  // namespace ssp
