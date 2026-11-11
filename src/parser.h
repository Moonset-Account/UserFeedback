// SPDX-License-Identifier: MIT
// Plan file parser. The format is a small line-based DSL (see README.md);
// the parser only reads the given file and never executes anything.
#ifndef PATCHPLAN_PARSER_H
#define PATCHPLAN_PARSER_H

#include <stdexcept>
#include <string>

#include "model.h"

namespace patchplan {

class ParseError : public std::runtime_error {
public:
    explicit ParseError(const std::string& msg) : std::runtime_error(msg) {}
};

// Parses the plan file at path. Throws ParseError on any syntax or
// referential problem; all references (services, patches, windows, ...)
// are resolved here so the engine sees a closed, validated model.
Plan parsePlanFile(const std::string& path);

}  // namespace patchplan

#endif  // PATCHPLAN_PARSER_H
