#include "Rewriter.hpp"
#include "Json.hpp"

#include <algorithm>
#include <functional>
#include <set>
#include <sstream>
#include <unordered_map>

namespace rewriter {

const char* reasonText(ReviewReason r) {
    switch (r) {
    case ReviewReason::UnsupportedCallee:
        return "call does not bind to a supported library declaration";
    case ReviewReason::MacroExpansion:
        return "call or format argument is produced by macro expansion";
    case ReviewReason::NonStringType:
        return "format argument has a non-string (non char-pointer/array) type";
    case ReviewReason::RuntimeSpecifiers:
        return "arguments after the format slot imply runtime format "
               "specifiers that a literal \"%s\" cannot consume";
    case ReviewReason::ParseError:
        return "AST could not be parsed";
    }
    return "unknown";
}

namespace {

struct LineIndex {
    // Start offset of each line (line numbers are 1-based; index 0 unused).
    std::vector<size_t> lineStart{0};

    explicit LineIndex(const std::string& src) {
        for (size_t i = 0; i < src.size(); ++i)
            if (src[i] == '\n') lineStart.push_back(i + 1);
    }
    SrcLoc at(size_t offset) const {
        SrcLoc loc;
        loc.offset = offset;
        auto it = std::upper_bound(lineStart.begin(), lineStart.end(), offset);
        size_t idx = static_cast<size_t>(it - lineStart.begin()); // 1-based line
        loc.line = static_cast<unsigned>(idx);
        loc.col = static_cast<unsigned>(offset - lineStart[idx - 1] + 1);
        loc.valid = true;
        return loc;
    }
};

// ---- clang JSON location helpers ----------------------------------------

// A SourceLocation object may be either {offset,...} or
// {spellingLoc:{...}, expansionLoc:{...}}.
const json::Value* expansionLoc(const json::Value* loc) {
    if (!loc) return nullptr;
    if (const json::Value* e = loc->find("expansionLoc")) return e;
    return loc;
}

std::string locFile(const json::Value* loc) {
    if (!loc) return {};
    if (const json::Value* f = loc->find("file")) return std::string(f->asString());
    // No "file" means the spelling file is the translation-unit main file
    // (clang only emits "file" when it differs from the previous TU file).
    return {};
}

std::string baseName(std::string path) {
    size_t slash = path.find_last_of('/');
    return slash == std::string::npos ? path : path.substr(slash + 1);
}

// Is this location (spelling side) physically inside the main file?
// `mainBase` is the basename of the translation-unit main file. Clang omits
// the "file" field for main-file locations until the first header switch, and
// may emit either a relative name or a full path elsewhere, so compare
// basenames rather than testing for the presence of the field.
bool spellingInMainFile(const json::Value* loc, const std::string& mainBase) {
    if (!loc) return false;
    if (loc->find("spellingLoc"))
        return spellingInMainFile(loc->find("spellingLoc"), mainBase);
    if (loc->find("includedFrom")) return false;
    std::string f = locFile(loc);
    if (f.empty()) return true;          // omitted field == main file
    return baseName(f) == mainBase;
}

bool isMacroExpandedRange(const json::Value* range) {
    if (!range) return false;
    const json::Value* begin = range->find("begin");
    const json::Value* end = range->find("end");
    for (const json::Value* side : {begin, end}) {
        if (!side) continue;
        if (side->find("spellingLoc") || side->find("expansionLoc"))
            return true;
        if (side->find("isMacroArgExpansion")) {
            const json::Value* flag = side->find("isMacroArgExpansion");
            if (flag->type == json::Type::Bool && flag->boolean) return true;
        }
    }
    return false;
}

// Expansion-location offset within the main file, or npos.
size_t expansionOffset(const json::Value* rangeSide) {
    const json::Value* e = expansionLoc(rangeSide);
    const json::Value* off = e ? e->find("offset") : nullptr;
    if (!off || off->type != json::Type::Number) return std::string::npos;
    return static_cast<size_t>(std::stoull(std::string(off->asString())));
}

struct Range {
    size_t begin = 0;
    size_t end = 0;                 // one-past-the-end
    bool macro = false;
};

bool getRange(const json::Value* node, Range& out) {
    const json::Value* range = node->find("range");
    if (!range) return false;
    const json::Value* b = range->find("begin");
    const json::Value* e = range->find("end");
    size_t bOff = expansionOffset(b);
    if (bOff == std::string::npos) return false;
    // End is token-based: offset points at the last token, tokLen gives its
    // length, so the half-open end is offset + tokLen.
    const json::Value* eExp = expansionLoc(e);
    const json::Value* eOffNode = eExp ? eExp->find("offset") : nullptr;
    const json::Value* eLenNode = eExp ? eExp->find("tokLen") : nullptr;
    if (!eOffNode || !eLenNode) return false;
    size_t eOff = static_cast<size_t>(
        std::stoull(std::string(eOffNode->asString())));
    unsigned tokLen = static_cast<unsigned>(
        std::stoul(std::string(eLenNode->asString())));
    out.begin = bOff;
    out.end = eOff + tokLen;
    out.macro = isMacroExpandedRange(range);
    return true;
}

// Walk every node in the JSON tree.
void walk(const json::Value* node,
          const std::function<void(const json::Value*)>& fn) {
    if (!node) return;
    fn(node);
    if (const json::Value* inner = node->find("inner")) {
        for (const json::Value* child : inner->array) walk(child, fn);
    }
}

// Descend through implicit casts/wrapper expressions to the semantic node.
const json::Value* unwrapImplicit(const json::Value* node) {
    while (node) {
        if (node->type != json::Type::Object) break;
        std::string_view kind = node->find("kind") ? node->find("kind")->asString()
                                                   : std::string_view{};
        if (kind == "ImplicitCastExpr" || kind == "ParenExpr" ||
            kind == "CStyleCastExpr") {
            const json::Value* inner = node->find("inner");
            if (!inner || inner->array.empty()) break;
            node = inner->array.front();
            continue;
        }
        break;
    }
    return node;
}

// Find a StringLiteral anywhere under a node (adjacent literal concatenation
// collapses to one StringLiteral anyway, but be defensive about wrappers).
const json::Value* findStringLiteral(const json::Value* node) {
    if (!node) return nullptr;
    const json::Value* kindNode = node->find("kind");
    if (kindNode && kindNode->asString() == "StringLiteral") return node;
    if (const json::Value* inner = node->find("inner"))
        for (const json::Value* c : inner->array)
            if (const json::Value* hit = findStringLiteral(c)) return hit;
    return nullptr;
}

// Is the canonical/printed type a char pointer or char array?
bool isCharStringType(const std::string& qualType) {
    // Normalize whitespace a little.
    std::string t = qualType;
    for (char& c : t)
        if (c == '\t') c = ' ';
    auto contains = [&](std::string_view needle) {
        return t.find(needle) != std::string::npos;
    };
    if (contains("char[")) return true;                  // char[N]
    if (contains("char *") || contains("char*")) return true;
    if (contains("char [")) return true;
    return false;
}

std::string nodeQualType(const json::Value* node) {
    const json::Value* t = node->find("type");
    if (!t) return {};
    const json::Value* q = t->find("qualType");
    return q ? std::string(q->asString()) : std::string{};
}

} // namespace

Result analyze(const std::string& mainFile,
               const std::string& sourceText,
               const json::Value* root,
               const std::vector<TargetSpec>& targets) {
    Result result;
    LineIndex lines(sourceText);
    const std::string mainBase = baseName(mainFile);

    std::unordered_map<std::string, const TargetSpec*> byName;
    for (const TargetSpec& t : targets) byName.emplace(t.functionName, &t);

    // Determine which target names have ANY declaration spelled in the main
    // file. Clang may keep a main-file redeclaration under a different AST id
    // than the merged system-header declaration, so id grouping alone cannot
    // reveal a shadowing declaration. A name shadowed in the translation unit
    // makes every same-named call ineligible for mechanical rewriting.
    std::set<std::string> namesShadowedInMainFile;

    walk(root, [&](const json::Value* node) {
        const json::Value* kindNode = node->find("kind");
        if (!kindNode || kindNode->asString() != "FunctionDecl") return;
        const json::Value* nameNode = node->find("name");
        if (!nameNode) return;
        const std::string name = std::string(nameNode->asString());
        if (!byName.count(name)) return;
        const json::Value* loc = node->find("loc");
        if (spellingInMainFile(loc, mainBase))
            namesShadowedInMainFile.insert(name);
    });

    walk(root, [&](const json::Value* node) {
        const json::Value* kindNode = node->find("kind");
        if (!kindNode || kindNode->asString() != "CallExpr") return;

        const json::Value* inner = node->find("inner");
        if (!inner || inner->array.empty()) return;
        const json::Value* calleeWrapper = inner->array.front();
        const json::Value* callee = unwrapImplicit(calleeWrapper);
        const json::Value* calleeKind = callee->find("kind");
        if (!calleeKind || calleeKind->asString() != "DeclRefExpr")
            return;  // indirect/builtin call: nothing mechanical to bind
        const json::Value* ref = callee->find("referencedDecl");
        if (!ref) return;
        const json::Value* refName = ref->find("name");
        if (!refName) return;
        auto specIt = byName.find(std::string(refName->asString()));
        if (specIt == byName.end()) return;   // not one of our target names
        const TargetSpec& spec = *specIt->second;
        ++result.totalCallsSeen;

        // Call spelling must live in the main file.
        Range callRange;
        if (!getRange(node, callRange)) return;
        SrcLoc callLoc = lines.at(callRange.begin);

        // Locate the format argument.
        const size_t argCount = inner->array.size() - 1;
        if (argCount <= spec.formatArg) {
            ReviewItem item{mainFile, callLoc, spec.functionName,
                            ReviewReason::UnsupportedCallee,
                            "call has no argument at the format position"};
            result.review.push_back(std::move(item));
            return;
        }
        const json::Value* fmtArg = inner->array[1 + spec.formatArg];
        Range fmtRange;
        if (!getRange(fmtArg, fmtRange)) return;

        // 1. Macro-expanded call or format slot: manual review. This is a
        //    property of the source spelling and is reported regardless of
        //    which declaration the call resolves to.
        if (callRange.macro || fmtRange.macro) {
            ReviewItem item;
            item.file = mainFile;
            item.loc = lines.at(fmtRange.begin);
            item.function = spec.functionName;
            item.reason = ReviewReason::MacroExpansion;
            item.detail =
                callRange.macro
                    ? "the whole call is produced by macro expansion"
                    : "the format argument spelling comes from a macro";
            result.review.push_back(std::move(item));
            return;
        }

        // Bounds check against the actual source text.
        if (fmtRange.end > sourceText.size()) return;

        const json::Value* semanticArg = unwrapImplicit(fmtArg);

        // 2. Literal format strings are already safe: leave untouched,
        //    however many trailing varargs they carry (those feed the
        //    literal's own conversion specifiers), and regardless of any
        //    same-named declaration in the file.
        const json::Value* literal = findStringLiteral(semanticArg);
        if (literal) {
            ++result.literalSafe;
            return;
        }

        // 3. A same-named declaration in the main file means the call cannot
        //    be proven to bind to the supported library declaration.
        if (namesShadowedInMainFile.count(spec.functionName)) {
            ReviewItem item;
            item.file = mainFile;
            item.loc = callLoc;
            item.function = spec.functionName;
            item.reason = ReviewReason::UnsupportedCallee;
            item.detail =
                "a declaration named '" + spec.functionName +
                "' exists in the main file; the call cannot be proven to "
                "bind to the supported library declaration";
            result.review.push_back(std::move(item));
            return;
        }

        // 4. Type gate: only mechanical-fix char pointer/array expressions.
        //    Inspect the semantic inner expression, not the implicit cast
        //    node, because an int argument decays to "const char *" only
        //    through the call's implicit conversion.
        const std::string qt = nodeQualType(semanticArg);
        if (!isCharStringType(qt)) {
            ReviewItem item;
            item.file = mainFile;
            item.loc = lines.at(fmtRange.begin);
            item.function = spec.functionName;
            item.reason = ReviewReason::NonStringType;
            item.detail = "format argument type is '" + qt + "'";
            result.review.push_back(std::move(item));
            return;
        }

        // 5. Arguments after the format slot would be consumed by conversion
        //    specifiers living inside the runtime string. Rebinding the
        //    runtime string to a literal "%s" leaves those arguments
        //    unconsumed (-Wformat-extra-args) and changes behavior, so a
        //    human must decide.
        if (argCount > spec.formatArg + 1) {
            ReviewItem item;
            item.file = mainFile;
            item.loc = lines.at(fmtRange.begin);
            item.function = spec.functionName;
            item.reason = ReviewReason::RuntimeSpecifiers;
            std::ostringstream d;
            d << (argCount - spec.formatArg - 1)
              << " argument(s) follow the format position";
            item.detail = d.str();
            result.review.push_back(std::move(item));
            return;
        }

        // 6. Mechanically fixable.
        Edit edit;
        // The safe form is func(..., "%s", original, ...): the literal is
        // inserted BEFORE the original expression, which is re-bound as the
        // first vararg. Inserting at the expression's start keeps its exact
        // spelling, evaluation count and order, and leaves any trailing
        // comment attached to the argument where a reader expects it.
        edit.offset = fmtRange.begin;
        edit.loc = lines.at(fmtRange.begin);
        edit.function = spec.functionName;
        edit.originalExpr =
            sourceText.substr(fmtRange.begin, fmtRange.end - fmtRange.begin);
        edit.inserted = "\"%s\", ";
        result.edits.push_back(std::move(edit));
    });

    // Stable, de-duplicated output (same offset cannot carry two edits).
    std::sort(result.edits.begin(), result.edits.end(),
              [](const Edit& a, const Edit& b) { return a.offset < b.offset; });
    std::sort(result.review.begin(), result.review.end(),
              [](const ReviewItem& a, const ReviewItem& b) {
                  if (a.loc.line != b.loc.line) return a.loc.line < b.loc.line;
                  if (a.loc.col != b.loc.col) return a.loc.col < b.loc.col;
                  return a.reason < b.reason;
              });
    // Deduplicate identical review entries (e.g. duplicate decl chains).
    std::set<std::string> seen;
    std::vector<ReviewItem> uniqueReview;
    for (const ReviewItem& r : result.review) {
        std::string key = std::to_string(r.loc.line) + ":" +
                          std::to_string(r.loc.col) + ":" +
                          std::to_string(static_cast<int>(r.reason)) + ":" +
                          r.function;
        if (seen.insert(key).second) uniqueReview.push_back(r);
    }
    result.review = std::move(uniqueReview);
    return result;
}

} // namespace rewriter
