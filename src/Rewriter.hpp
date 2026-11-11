#ifndef FMTREWRITE_REWRITER_HPP
#define FMTREWRITE_REWRITER_HPP

#include <cstddef>
#include <string>
#include <vector>

namespace json { class Value; }

namespace rewriter {

// One supported formatting output function, declared up front.
//   functionName : spelling used at the call site
//   expectedType : canonical function type as printed by clang's JSON dump
//   formatArg    : zero-based index of the format argument
struct TargetSpec {
    std::string functionName;
    std::string expectedType;
    unsigned formatArg;
};

// A location inside the analyzed main file.
struct SrcLoc {
    unsigned line = 0;
    unsigned col = 0;
    size_t offset = 0;
    bool valid = false;
};

enum class ReviewReason {
    UnsupportedCallee,     // call does not bind to a supported declaration
    MacroExpansion,        // call/format slot comes from macro expansion
    NonStringType,         // format argument type is not a char pointer/array
    RuntimeSpecifiers,     // extra varargs imply runtime format specifiers
    ParseError,            // AST could not be produced or is malformed
};

struct ReviewItem {
    std::string file;
    SrcLoc loc;
    std::string function;
    ReviewReason reason;
    std::string detail;
};

struct Edit {
    size_t offset = 0;              // insertion point (before the char there)
    std::string inserted;           // text to insert
    SrcLoc loc;                     // location of the format argument start
    std::string function;
    std::string originalExpr;       // verbatim source text of the argument
};

struct Result {
    std::vector<Edit> edits;
    std::vector<ReviewItem> review;
    int totalCallsSeen = 0;         // calls whose spelling names a target
    int literalSafe = 0;            // target calls already using a literal
};

const char* reasonText(ReviewReason r);

// Analyze the JSON AST text for `mainFile`. The source text of the main file
// is required for verbatim extraction of argument ranges.
Result analyze(const std::string& mainFile,
               const std::string& sourceText,
               const json::Value* astRoot,
               const std::vector<TargetSpec>& targets);

} // namespace rewriter

#endif // FMTREWRITE_REWRITER_HPP
