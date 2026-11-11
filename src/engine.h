// SPDX-License-Identifier: MIT
// Validation engine: forward simulation, dependency-cycle detection,
// safe-rollback search (BFS plus an exhaustive permutation cross-check).
#ifndef PATCHPLAN_ENGINE_H
#define PATCHPLAN_ENGINE_H

#include <map>
#include <set>
#include <string>
#include <vector>

#include "model.h"

namespace patchplan {

enum class ViolationKind {
    InitialIncompatible,
    DependencyCycle,
    UnknownReference,
    AlreadyApplied,
    VersionPrecondition,
    DependencyMissing,
    MutexConflict,
    WindowCapacity,
    Compatibility,
    RollbackUnavailable,
    RollbackVersionDrift,
    RollbackIncompatible,
    RollbackBudgetExhausted,
};

const char* violationName(ViolationKind k);

struct Violation {
    ViolationKind kind = ViolationKind::Compatibility;
    int stepIndex = 0;            // 0 for pre-flight / cycle violations
    std::string windowId;
    std::string patchId;
    std::string service;
    std::string fromVersion;     // version before the attempted change
    std::string toVersion;       // attempted target version
    std::string otherService;
    std::string otherVersion;
    std::string detail;          // human-readable, fully deterministic
};

// service -> current version
using VersionMap = std::map<std::string, std::string>;

// One successfully applied step with the full version map afterwards.
struct StepTrace {
    int index = 0;
    std::string patchId;
    std::string windowId;
    VersionMap versionsAfter;
};

struct ForwardReport {
    bool ok = false;
    Violation firstViolation;                       // earliest constraint breach
    VersionMap finalVersions;                       // state where it stopped
    std::set<std::string> appliedPatches;
    std::map<std::string, int> windowUsage;         // window id -> changes used
    std::map<std::string, bool> servicePatched;     // service -> fix covered
    int appliedStepCount = 0;                       // steps actually applied
    std::vector<StepTrace> traces;                  // per-step state evolution
};

struct RollbackEvidence {
    ViolationKind kind = ViolationKind::RollbackIncompatible;
    std::string actionId;          // action attempted ("-" when none available)
    std::string service;
    std::string fromVersion;
    std::string toVersion;
    std::string otherService;
    std::string otherVersion;
    int depth = 0;                 // rollback actions already taken
    std::string detail;
};

struct RollbackReport {
    bool safe = false;
    std::string failureId;
    std::vector<std::string> safeSequence;   // rollback action ids, empty if none
    int budget = 0;
    VersionMap failureVersions;
    std::set<std::string> appliedAtFailure;
    std::vector<RollbackEvidence> evidence;  // sorted, deterministic conflicts
    // Exhaustive permutation cross-check results.
    long long permutationsChecked = 0;
    long long permutationsSafe = 0;
    bool exhaustiveAgrees = true;
    std::vector<std::string> exhaustiveSafeSequence;
};

class Engine {
public:
    explicit Engine(const Plan& plan);

    // Pre-flight: initial versions must satisfy every compatibility rule.
    bool checkInitialState(Violation& out) const;

    // Dependency graph cycle detection; fills a deterministic cycle path.
    bool detectCycle(std::vector<std::string>& cyclePath) const;

    // Full forward simulation of the candidate release order.
    ForwardReport simulateForward() const;

    // Replay steps 1..afterStepCount to reconstruct a failure state.
    VersionMap replayTo(int afterStepCount,
                        std::set<std::string>& applied) const;

    // BFS over candidate rollback actions for a safe path back to the
    // initial versions, honouring compatibility at every intermediate state.
    RollbackReport searchRollback(const Failure& failure) const;

    // Exhaustive enumeration of every permutation of the applicable
    // rollback actions; for tiny inputs only (rejects inputs > 10 actions).
    RollbackReport exhaustiveRollback(const Failure& failure) const;

private:
    const Plan& plan_;
    // Allowed pairs, canonicalized (lexicographically smaller service first).
    std::map<std::pair<std::string, std::string>,
             std::set<std::pair<std::string, std::string>>>
        allowed_;

    // Returns a deterministic compatibility violation, or an empty
    // optional-like Violation with kind==Compatibility only when failed.
    bool compatible(const VersionMap& versions, Violation& out) const;
};

}  // namespace patchplan

#endif  // PATCHPLAN_ENGINE_H
