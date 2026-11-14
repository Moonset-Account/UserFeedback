// SEC-VULN-013 stack-protection coverage checker: core data model.
// All records are abstract, offline representations of build-tool exports.
#pragma once

#include <string>
#include <vector>

namespace ssp {

enum class RiskLevel { Low, Medium, High };

// Whether the protection policy demands stack protection for a source function.
enum class Requirement {
    Required,    // policy demands a protection evidence chain
    ExemptLeaf   // leaf function that the policy does not require to be protected
};

// How a source function is carried by a post-optimization compiled function.
enum class Relation {
    Self,     // retained as its own compiled function
    Inlined   // absorbed into an inlining caller
};

// Abstract protection mark exported by the build tool for a compiled function.
enum class Mark {
    Protected,
    Unprotected
};

// Top-level verdict for one source function in one build configuration.
enum class Verdict {
    Confirmed,   // required protection is backed by complete evidence
    Violation,   // complete evidence shows the required protection is absent
    Unknown,     // missing build record or ambiguous multiple attribution
    NotRequired  // policy does not require protection for this function
};

// Stable machine-readable reason codes explaining a verdict.
enum class Reason {
    None,
    SelfProtected,           // the function's own compiled record is protected
    InlinedCallerProtected,  // every inlining caller's compiled record is protected
    PartialCallerProtection, // multiple callers, only a subset are protected
    MissingAttribution,      // no post-optimization attribution record exists
    MissingProtectionMark,   // attribution exists but the compiled function has no mark
    UnprotectedSelf,         // the function's own compiled record is unprotected
    UnprotectedCallers,      // every inlining caller's compiled record is unprotected
    ExemptLeaf               // policy-exempt leaf function
};

const char* token(RiskLevel level);
const char* token(Requirement requirement);
const char* token(Relation relation);
const char* token(Mark mark);
const char* token(Verdict verdict);
const char* token(Reason reason);

// Human-readable Chinese explanation of a reason code.
const char* reasonLabelZh(Reason reason);

// A row of the local function risk list.
struct RiskEntry {
    std::string riskId;            // stable record id, e.g. R001
    std::string sourceFunction;    // source-level function identifier
    std::string compilationUnit;   // source compilation unit, e.g. net/parse.c
    RiskLevel level = RiskLevel::Low;
    Requirement requirement = Requirement::Required;
    std::string note;
};

// A row of the post-optimization inline attribution mapping.
struct AttributionEntry {
    std::string config;            // build configuration name, e.g. O2
    std::string sourceFunction;
    std::string compiledFunction;  // post-optimization function identifier
    std::string compilationUnit;   // object file / compilation unit carrying the result
    Relation relation = Relation::Self;
    std::string attributionId;     // stable record id, e.g. A001
};

// A row of the build-tool exported abstract protection marks.
struct ProtectionMark {
    std::string config;
    std::string compiledFunction;
    std::string compilationUnit;
    Mark mark = Mark::Unprotected;
    std::string protectorKind;     // free-text abstract kind, e.g. ssp-strong
    std::string markId;            // stable record id, e.g. M001
};

}  // namespace ssp
