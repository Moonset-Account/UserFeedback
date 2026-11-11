// SPDX-License-Identifier: MIT
#include "engine.h"

#include <algorithm>
#include <deque>
#include <functional>
#include <sstream>
#include <tuple>

namespace patchplan {

const char* violationName(ViolationKind k) {
    switch (k) {
        case ViolationKind::InitialIncompatible: return "INITIAL_INCOMPATIBLE";
        case ViolationKind::DependencyCycle: return "DEPENDENCY_CYCLE";
        case ViolationKind::UnknownReference: return "UNKNOWN_REFERENCE";
        case ViolationKind::AlreadyApplied: return "ALREADY_APPLIED";
        case ViolationKind::VersionPrecondition: return "VERSION_PRECONDITION";
        case ViolationKind::DependencyMissing: return "DEPENDENCY_MISSING";
        case ViolationKind::MutexConflict: return "MUTEX_CONFLICT";
        case ViolationKind::WindowCapacity: return "WINDOW_CAPACITY";
        case ViolationKind::Compatibility: return "COMPATIBILITY";
        case ViolationKind::RollbackUnavailable: return "ROLLBACK_UNAVAILABLE";
        case ViolationKind::RollbackVersionDrift: return "ROLLBACK_VERSION_DRIFT";
        case ViolationKind::RollbackIncompatible: return "ROLLBACK_INCOMPATIBLE";
        case ViolationKind::RollbackBudgetExhausted:
            return "ROLLBACK_BUDGET_EXHAUSTED";
    }
    return "UNKNOWN";
}

Engine::Engine(const Plan& plan) : plan_(plan) {
    for (const auto& r : plan_.compatRules) {
        allowed_[{r.serviceA, r.versionA}].insert({r.serviceB, r.versionB});
        allowed_[{r.serviceB, r.versionB}].insert({r.serviceA, r.versionA});
    }
}

bool Engine::compatible(const VersionMap& versions, Violation& out) const {
    std::vector<std::string> names;
    names.reserve(versions.size());
    for (const auto& [name, ver] : versions) names.push_back(name);
    std::sort(names.begin(), names.end());
    for (size_t i = 0; i < names.size(); ++i) {
        for (size_t j = i + 1; j < names.size(); ++j) {
            const std::string& x = names[i];
            const std::string& y = names[j];
            const std::string& vx = versions.at(x);
            const std::string& vy = versions.at(y);
            auto it = allowed_.find({x, vx});
            if (it == allowed_.end()) continue;  // unconstrained (service,version)
            if (!it->second.count({y, vy})) {
                out.kind = ViolationKind::Compatibility;
                out.service = x;
                out.fromVersion = vx;
                out.toVersion = vx;
                out.otherService = y;
                out.otherVersion = vy;
                std::ostringstream d;
                d << "service pair (" << x << "@" << vx << ", " << y << "@" << vy
                  << ") is not listed in any compat rule";
                out.detail = d.str();
                return false;
            }
        }
    }
    return true;
}

bool Engine::checkInitialState(Violation& out) const {
    VersionMap initial;
    for (const auto& [name, svc] : plan_.services) initial[name] =
        svc.initialVersion;
    if (compatible(initial, out)) return true;
    out.kind = ViolationKind::InitialIncompatible;
    out.stepIndex = 0;
    return false;
}

bool Engine::detectCycle(std::vector<std::string>& cyclePath) const {
    enum Color { WHITE, GRAY, BLACK };
    std::map<std::string, int> color;
    for (const auto& [id, p] : plan_.patches) color[id] = WHITE;

    std::vector<std::string> stack;
    std::function<bool(const std::string&)> dfs = [&](const std::string& u) {
        color[u] = GRAY;
        stack.push_back(u);
        for (const auto& v : plan_.patches.at(u).dependsOn) {
            if (color[v] == GRAY) {
                auto it = std::find(stack.begin(), stack.end(), v);
                cyclePath.assign(it, stack.end());
                cyclePath.push_back(v);
                return true;
            }
            if (color[v] == WHITE && dfs(v)) return true;
        }
        stack.pop_back();
        color[u] = BLACK;
        return false;
    };

    for (const auto& [id, p] : plan_.patches) {
        if (color[id] == WHITE && dfs(id)) return true;
    }
    return false;
}

ForwardReport Engine::simulateForward() const {
    ForwardReport rep;
    VersionMap& versions = rep.finalVersions;
    for (const auto& [name, svc] : plan_.services) {
        versions[name] = svc.initialVersion;
        rep.servicePatched[name] = false;
    }
    for (const auto& [id, w] : plan_.windows) rep.windowUsage[id] = 0;

    for (const auto& step : plan_.steps) {
        const Patch& p = plan_.patches.at(step.patchId);
        Violation v;
        v.stepIndex = step.index;
        v.windowId = step.windowId;
        v.patchId = p.id;
        v.service = p.service;
        v.fromVersion = versions[p.service];
        v.toVersion = p.toVersion;

        auto fail = [&](ViolationKind k, const std::string& detail) {
            v.kind = k;
            v.detail = detail;
            rep.firstViolation = v;
            rep.ok = false;
            return rep;
        };

        if (rep.appliedPatches.count(p.id))
            return fail(ViolationKind::AlreadyApplied,
                        "patch '" + p.id + "' is already applied");

        if (versions[p.service] != p.fromVersion)
            return fail(ViolationKind::VersionPrecondition,
                        "service '" + p.service + "' is at '" +
                            versions[p.service] + "' but patch '" + p.id +
                            "' expects '" + p.fromVersion + "'");

        for (const auto& d : p.dependsOn) {
            if (!rep.appliedPatches.count(d))
                return fail(ViolationKind::DependencyMissing,
                            "dependency patch '" + d + "' of '" + p.id +
                                "' is not applied yet");
        }

        for (const auto& m : p.mutexWith) {
            if (rep.appliedPatches.count(m))
                return fail(ViolationKind::MutexConflict,
                            "patch '" + p.id + "' is mutually exclusive with "
                                            "already-applied patch '" + m + "'");
        }

        if (rep.windowUsage[step.windowId] + 1 >
            plan_.windows.at(step.windowId).capacity) {
            std::ostringstream d;
            d << "window '" << step.windowId << "' capacity "
              << plan_.windows.at(step.windowId).capacity
              << " exhausted; this would be change "
              << (rep.windowUsage[step.windowId] + 1);
            return fail(ViolationKind::WindowCapacity, d.str());
        }

        // Tentative apply, then verify cross-service compatibility.
        VersionMap tentative = versions;
        tentative[p.service] = p.toVersion;
        Violation cv;
        if (!compatible(tentative, cv)) {
            v.kind = ViolationKind::Compatibility;
            v.otherService = cv.otherService;
            v.otherVersion = cv.otherVersion;
            v.detail = cv.detail + " (introduced by applying '" + p.id + "')";
            rep.firstViolation = v;
            return rep;
        }

        versions = std::move(tentative);
        rep.appliedPatches.insert(p.id);
        rep.windowUsage[step.windowId]++;
        rep.servicePatched[p.service] = true;
        rep.appliedStepCount++;
        rep.traces.push_back(
            StepTrace{step.index, p.id, step.windowId, versions});
    }

    rep.ok = true;
    return rep;
}

VersionMap Engine::replayTo(int afterStepCount,
                            std::set<std::string>& applied) const {
    VersionMap versions;
    for (const auto& [name, svc] : plan_.services)
        versions[name] = svc.initialVersion;
    applied.clear();
    for (int i = 0; i < afterStepCount; ++i) {
        const Patch& p = plan_.patches.at(plan_.steps[i].patchId);
        versions[p.service] = p.toVersion;
        applied.insert(p.id);
    }
    return versions;
}

namespace {

std::string stateKey(const VersionMap& versions,
                     const std::set<std::string>& applied) {
    std::ostringstream k;
    for (const auto& [name, ver] : versions) k << name << "=" << ver << ";";
    k << "|";
    for (const auto& id : applied) k << id << ",";
    return k.str();
}

bool isInitialState(const Plan& plan, const VersionMap& versions,
                    const std::set<std::string>& applied) {
    if (!applied.empty()) return false;
    for (const auto& [name, svc] : plan.services)
        if (versions.at(name) != svc.initialVersion) return false;
    return true;
}

}  // namespace

RollbackReport Engine::searchRollback(const Failure& failure) const {
    RollbackReport rep;
    rep.failureId = failure.id;
    rep.budget = failure.rollbackBudget;
    rep.appliedAtFailure = [&] {
        std::set<std::string> a;
        rep.failureVersions = replayTo(failure.afterStepCount, a);
        return a;
    }();

    // Candidate actions that can possibly participate: those whose target
    // patch is applied at the failure point, sorted by action id.
    std::vector<std::string> candidates;
    for (const auto& [id, ra] : plan_.rollbacks) {
        if (rep.appliedAtFailure.count(ra.patchId)) candidates.push_back(id);
    }

    struct Node {
        VersionMap versions;
        std::set<std::string> applied;
        std::vector<std::string> path;
    };

    std::deque<Node> queue;
    std::set<std::string> visited;
    queue.push_back(Node{rep.failureVersions, rep.appliedAtFailure, {}});
    visited.insert(stateKey(rep.failureVersions, rep.appliedAtFailure));

    while (!queue.empty()) {
        Node cur = std::move(queue.front());
        queue.pop_front();

        if (isInitialState(plan_, cur.versions, cur.applied)) {
            rep.safe = true;
            rep.safeSequence = cur.path;
            return rep;
        }
        if (static_cast<int>(cur.path.size()) >= failure.rollbackBudget)
            continue;  // budget dead-end; evidence collected by the DFS below

        for (const auto& aid : candidates) {
            const RollbackAction& ra = plan_.rollbacks.at(aid);
            const Patch& p = plan_.patches.at(ra.patchId);
            if (!cur.applied.count(p.id)) continue;  // nothing to revert
            if (cur.versions.at(p.service) != p.toVersion) continue;  // drift

            Node nxt = cur;
            nxt.versions[p.service] = p.fromVersion;
            nxt.applied.erase(p.id);
            nxt.path.push_back(aid);
            Violation cv;
            if (!compatible(nxt.versions, cv)) continue;  // unsafe transition
            std::string key = stateKey(nxt.versions, nxt.applied);
            if (visited.insert(key).second)
                queue.push_back(std::move(nxt));
        }
    }

    // No safe path: run a bounded DFS to collect concrete conflict evidence.
    struct EvKey {
        ViolationKind kind;
        std::string action, svc, fromV, toV, oSvc, oVer;
        int depth;
        bool operator<(const EvKey& o) const {
            return std::tie(kind, action, svc, fromV, toV, oSvc, oVer, depth) <
                   std::tie(o.kind, o.action, o.svc, o.fromV, o.toV, o.oSvc,
                            o.oVer, o.depth);
        }
    };
    std::set<EvKey> seen;

    std::function<void(VersionMap, std::set<std::string>, int)> dfs =
        [&](VersionMap versions, std::set<std::string> applied, int depth) {
            if (isInitialState(plan_, versions, applied)) return;

            bool anyAction = false;
            for (const auto& aid : candidates) {
                const RollbackAction& ra = plan_.rollbacks.at(aid);
                const Patch& p = plan_.patches.at(ra.patchId);
                if (!applied.count(p.id)) continue;
                anyAction = true;
                EvKey base{ViolationKind::RollbackIncompatible,
                           aid, p.service, versions.at(p.service),
                           p.fromVersion, "", "", depth};
                if (versions.at(p.service) != p.toVersion) {
                    base.kind = ViolationKind::RollbackVersionDrift;
                    if (seen.insert(base).second) {
                        RollbackEvidence e;
                        e.kind = base.kind;
                        e.actionId = aid;
                        e.service = p.service;
                        e.fromVersion = versions.at(p.service);
                        e.toVersion = p.toVersion;
                        e.depth = depth;
                        e.detail = "action '" + aid + "' expects '" + p.service +
                                   "@" + p.toVersion + "' but service is at '" +
                                   versions.at(p.service) + "'";
                        rep.evidence.push_back(e);
                    }
                    continue;
                }
                VersionMap nv = versions;
                nv[p.service] = p.fromVersion;
                Violation cv;
                if (!compatible(nv, cv)) {
                    base.kind = ViolationKind::RollbackIncompatible;
                    base.oSvc = cv.otherService;
                    base.oVer = cv.otherVersion;
                    if (seen.insert(base).second) {
                        RollbackEvidence e;
                        e.kind = ViolationKind::RollbackIncompatible;
                        e.actionId = aid;
                        e.service = p.service;
                        e.fromVersion = versions.at(p.service);
                        e.toVersion = p.fromVersion;
                        e.otherService = cv.otherService;
                        e.otherVersion = cv.otherVersion;
                        e.depth = depth;
                        e.detail = "rollback '" + aid + "' would produce (" +
                                   p.service + "@" + p.fromVersion + ", " +
                                   cv.otherService + "@" + cv.otherVersion +
                                   ") which violates a compat rule";
                        rep.evidence.push_back(e);
                    }
                    continue;
                }
                if (depth + 1 > failure.rollbackBudget) {
                    base.kind = ViolationKind::RollbackBudgetExhausted;
                    base.oSvc.clear();
                    base.oVer.clear();
                    if (seen.insert(base).second) {
                        RollbackEvidence e;
                        e.kind = ViolationKind::RollbackBudgetExhausted;
                        e.actionId = aid;
                        e.service = p.service;
                        e.fromVersion = versions.at(p.service);
                        e.toVersion = p.fromVersion;
                        e.depth = depth;
                        e.detail = "taking action '" + aid + "' would exceed the "
                                             "rollback budget of " +
                                   std::to_string(failure.rollbackBudget);
                        rep.evidence.push_back(e);
                    }
                    continue;
                }
                std::set<std::string> napplied = applied;
                napplied.erase(p.id);
                dfs(nv, napplied, depth + 1);
            }
            if (!anyAction) {
                EvKey key{ViolationKind::RollbackUnavailable, "-", "", "",
                          "", "", "", depth};
                if (seen.insert(key).second) {
                    RollbackEvidence e;
                    e.kind = ViolationKind::RollbackUnavailable;
                    e.actionId = "-";
                    e.depth = depth;
                    std::ostringstream left;
                    for (const auto& id : applied) left << id << " ";
                    e.detail =
                        "no candidate rollback action remains for applied "
                        "patch(es): " +
                        left.str();
                    rep.evidence.push_back(e);
                }
            }
        };

    dfs(rep.failureVersions, rep.appliedAtFailure, 0);

    std::sort(rep.evidence.begin(), rep.evidence.end(),
              [](const RollbackEvidence& a, const RollbackEvidence& b) {
                  if (a.depth != b.depth) return a.depth < b.depth;
                  if (violationName(a.kind) != violationName(b.kind))
                      return violationName(a.kind) < violationName(b.kind);
                  if (a.actionId != b.actionId)
                      return a.actionId < b.actionId;
                  return a.detail < b.detail;
              });
    if (rep.evidence.size() > 50) rep.evidence.resize(50);
    rep.safe = false;
    return rep;
}

RollbackReport Engine::exhaustiveRollback(const Failure& failure) const {
    RollbackReport rep;
    rep.failureId = failure.id;
    rep.budget = failure.rollbackBudget;
    std::set<std::string> applied;
    rep.failureVersions = replayTo(failure.afterStepCount, applied);
    rep.appliedAtFailure = applied;

    std::vector<std::string> candidates;
    for (const auto& [id, ra] : plan_.rollbacks)
        if (applied.count(ra.patchId)) candidates.push_back(id);
    // Already sorted: plan_.rollbacks is an ordered map.
    const int n = static_cast<int>(candidates.size());

    VersionMap initial;
    for (const auto& [name, svc] : plan_.services)
        initial[name] = svc.initialVersion;

    std::vector<int> perm(n);
    for (int i = 0; i < n; ++i) perm[i] = i;
    do {
        ++rep.permutationsChecked;
        VersionMap versions = rep.failureVersions;
        std::set<std::string> remaining = applied;
        bool valid = true;
        int used = 0;
        for (int idx : perm) {
            const RollbackAction& ra =
                plan_.rollbacks.at(candidates[static_cast<size_t>(idx)]);
            const Patch& p = plan_.patches.at(ra.patchId);
            if (!remaining.count(p.id) ||
                versions.at(p.service) != p.toVersion) {
                valid = false;  // action not applicable in this ordering
                break;
            }
            if (used + 1 > failure.rollbackBudget) {
                valid = false;
                break;
            }
            versions[p.service] = p.fromVersion;
            remaining.erase(p.id);
            ++used;
            Violation cv;
            if (!compatible(versions, cv)) {
                valid = false;
                break;
            }
        }
        if (valid && remaining.empty() && versions == initial) {
            ++rep.permutationsSafe;
            if (rep.exhaustiveSafeSequence.empty()) {
                for (int idx : perm)
                    rep.exhaustiveSafeSequence.push_back(
                        candidates[static_cast<size_t>(idx)]);
            }
        }
    } while (std::next_permutation(perm.begin(), perm.end()));

    // Agreement with BFS is decided by the caller, which holds both reports.
    rep.safe = rep.permutationsSafe > 0;
    return rep;
}

}  // namespace patchplan
