// SPDX-License-Identifier: MIT
// Patch plan data model: parsed, read-only representation of a plan file.
#ifndef PATCHPLAN_MODEL_H
#define PATCHPLAN_MODEL_H

#include <map>
#include <set>
#include <string>
#include <vector>

namespace patchplan {

// One service and its synthesized initial version.
struct Service {
    std::string name;
    std::string initialVersion;
};

// A patch moves one service from one version to another and may carry
// explicit dependency / mutual-exclusion edges.
struct Patch {
    std::string id;
    std::string service;
    std::string fromVersion;
    std::string toVersion;
    std::set<std::string> dependsOn;   // patch ids that must be applied first
    std::set<std::string> mutexWith;   // patch ids that must never coexist
};

// A symmetric cross-service compatibility rule: when both services have
// concrete versions, the pair must be listed. Pairs not listed here are
// unconstrained (a service paired with itself is never constrained).
struct CompatRule {
    std::string serviceA;
    std::string versionA;
    std::string serviceB;
    std::string versionB;
};

// A maintenance window caps how many service changes (step applications)
// may happen inside it.
struct Window {
    std::string id;
    int capacity = 0;
};

// One entry of the candidate release order.
struct Step {
    int index = 0;
    std::string patchId;
    std::string windowId;
};

// A candidate rollback action reverting one patch (restoring its fromVersion).
struct RollbackAction {
    std::string id;
    std::string patchId;
};

// An injected mid-flight failure: the state after steps 1..afterStepCount
// is treated as the failure state from which a safe rollback is searched.
struct Failure {
    std::string id;
    int afterStepCount = 0;
    int rollbackBudget = 0;   // max rollback actions in the safe sequence
};

struct Plan {
    std::map<std::string, Service> services;
    std::map<std::string, Patch> patches;
    std::vector<CompatRule> compatRules;
    std::map<std::string, Window> windows;
    std::vector<Step> steps;
    std::map<std::string, RollbackAction> rollbacks;
    std::vector<Failure> failures;
};

}  // namespace patchplan

#endif  // PATCHPLAN_MODEL_H
