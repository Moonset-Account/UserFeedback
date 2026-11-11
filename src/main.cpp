// SPDX-License-Identifier: MIT
// Command-line entry point. Reads and validates plan files only; it never
// performs any release or rollback action.
#include <algorithm>
#include <iostream>
#include <string>

#include "engine.h"
#include "parser.h"

using namespace patchplan;

namespace {

void printVersions(const VersionMap& v) {
    for (const auto& [name, ver] : v) std::cout << "      " << name << " = "
                                                << ver << "\n";
}

void printViolation(const Violation& v) {
    std::cout << "  VIOLATION: " << violationName(v.kind) << "\n";
    if (v.stepIndex > 0)
        std::cout << "    step:        " << v.stepIndex << "\n";
    if (!v.windowId.empty())
        std::cout << "    window:      " << v.windowId << "\n";
    if (!v.patchId.empty())
        std::cout << "    patch:       " << v.patchId << "\n";
    if (!v.service.empty())
        std::cout << "    service:     " << v.service << "\n";
    if (!v.fromVersion.empty())
        std::cout << "    fromVersion: " << v.fromVersion << "\n";
    if (!v.toVersion.empty())
        std::cout << "    toVersion:   " << v.toVersion << "\n";
    if (!v.otherService.empty())
        std::cout << "    conflictsWith: " << v.otherService << "@"
                  << v.otherVersion << "\n";
    std::cout << "    detail:      " << v.detail << "\n";
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "usage: " << argv[0]
                  << " [--exhaustive] <plan-file> [<plan-file> ...]\n";
        return 2;
    }
    bool exhaustive = false;
    std::vector<std::string> files;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--exhaustive")
            exhaustive = true;
        else
            files.push_back(a);
    }
    if (files.empty()) {
        std::cerr << "error: no plan file given\n";
        return 2;
    }

    int globalExit = 0;

    for (size_t fi = 0; fi < files.size(); ++fi) {
        const std::string& path = files[fi];
        std::cout << "======================================================"
                     "========================\n";
        std::cout << "PLAN FILE: " << path << "\n";
        std::cout << "======================================================"
                     "========================\n";

        Plan plan;
        try {
            plan = parsePlanFile(path);
        } catch (const ParseError& e) {
            std::cout << "RESULT: REJECTED (parse error)\n  " << e.what()
                      << "\n\n";
            globalExit = 1;
            continue;
        }

        Engine engine(plan);
        bool planUnsafe = false;

        // ---- 1. pre-flight checks --------------------------------------
        std::cout << "\n[1] PRE-FLIGHT\n";
        Violation iv;
        if (!engine.checkInitialState(iv)) {
            printViolation(iv);
            std::cout << "RESULT: REJECTED\n\n";
            globalExit = 1;
            continue;
        }
        std::cout << "  initial state: compatible\n";

        std::vector<std::string> cycle;
        if (engine.detectCycle(cycle)) {
            std::cout << "  VIOLATION: DEPENDENCY_CYCLE\n    cycle:       ";
            for (size_t i = 0; i < cycle.size(); ++i)
                std::cout << cycle[i] << (i + 1 == cycle.size() ? "\n" : " -> ");
            std::cout << "RESULT: REJECTED\n\n";
            globalExit = 1;
            continue;
        }
        std::cout << "  dependency graph: acyclic\n";

        // ---- 2. forward simulation -------------------------------------
        std::cout << "\n[2] FORWARD SIMULATION (candidate release order)\n";
        ForwardReport fr = engine.simulateForward();
        for (size_t ti = 0; ti < fr.traces.size(); ++ti) {
            const auto& t = fr.traces[ti];
            std::cout << "  step " << t.index << " apply " << t.patchId
                      << " [window " << t.windowId << "] -> versions:\n";
            printVersions(t.versionsAfter);
            std::cout << "      coverage: " << (ti + 1) << "/"
                      << plan.services.size() << " services fixed so far\n";
        }

        if (!fr.ok) {
            std::cout << "\n  EARLIEST CONSTRAINT BREACH\n";
            printViolation(fr.firstViolation);
            planUnsafe = true;
        } else {
            std::cout << "\n  all " << plan.steps.size()
                      << " steps applied without constraint breach\n";
        }

        std::cout << "  window usage:\n";
        for (const auto& [id, w] : plan.windows)
            std::cout << "    " << id << ": " << fr.windowUsage.at(id) << "/"
                      << w.capacity << " changes\n";
        std::cout << "  final fix coverage:\n";
        for (const auto& [name, patched] : fr.servicePatched)
            std::cout << "    " << name << ": "
                      << (patched ? "PATCHED" : "UNPATCHED") << " ("
                      << fr.finalVersions.at(name) << ")\n";

        // ---- 3. failure states and safe rollback -----------------------
        std::cout << "\n[3] MID-FLIGHT FAILURES AND ROLLBACK SEARCH\n";
        for (const auto& failure : plan.failures) {
            std::cout << "\n  FAILURE " << failure.id << " after "
                      << failure.afterStepCount << " step(s), rollback budget "
                      << failure.rollbackBudget << "\n";
            RollbackReport rr = engine.searchRollback(failure);
            std::cout << "    failure-state versions:\n";
            printVersions(rr.failureVersions);

            if (rr.safe) {
                std::cout << "    SAFE ROLLBACK SEQUENCE ("
                          << rr.safeSequence.size() << " action(s)):\n";
                for (const auto& a : rr.safeSequence)
                    std::cout << "      " << a << " (reverts patch "
                              << plan.rollbacks.at(a).patchId << ")\n";
            } else {
                planUnsafe = true;
                std::cout << "    NO SAFE ROLLBACK EXISTS within budget\n";
                std::cout << "    CONFLICT EVIDENCE (" << rr.evidence.size()
                          << " item(s)):\n";
                for (const auto& e : rr.evidence) {
                    std::cout << "      - [" << violationName(e.kind)
                              << "] depth=" << e.depth << " action="
                              << e.actionId;
                    if (!e.service.empty())
                        std::cout << " " << e.service << ": " << e.fromVersion
                                  << " -> " << e.toVersion;
                    if (!e.otherService.empty())
                        std::cout << " vs " << e.otherService << "@"
                                  << e.otherVersion;
                    std::cout << "\n        " << e.detail << "\n";
                }
            }

            // Exhaustive permutation cross-check for small inputs.
            size_t applicable = 0;
            for (const auto& [id, ra] : plan.rollbacks)
                if (rr.appliedAtFailure.count(ra.patchId)) ++applicable;
            if (exhaustive && applicable <= 10) {
                RollbackReport ex = engine.exhaustiveRollback(failure);
                bool agrees = (ex.permutationsSafe > 0) == rr.safe;
                if (agrees && rr.safe)
                    agrees = (ex.exhaustiveSafeSequence == rr.safeSequence);
                std::cout << "    EXHAUSTIVE CROSS-CHECK: "
                          << ex.permutationsChecked << " permutation(s), "
                          << ex.permutationsSafe << " safe; agreement="
                          << (agrees ? "YES" : "NO") << "\n";
                if (!ex.exhaustiveSafeSequence.empty()) {
                    std::cout << "      lexicographically first safe path:";
                    for (const auto& a : ex.exhaustiveSafeSequence)
                        std::cout << " " << a;
                    std::cout << "\n";
                }
                if (!agrees) planUnsafe = true;
            } else if (exhaustive) {
                std::cout << "    EXHAUSTIVE CROSS-CHECK: skipped ("
                          << applicable << " applicable actions > 10)\n";
            }
        }

        std::cout << "\nRESULT: " << (planUnsafe ? "REJECTED" : "ACCEPTED")
                  << "\n\n";
        if (planUnsafe) globalExit = 1;
    }
    return globalExit;
}
