// SPDX-License-Identifier: MIT
#include "parser.h"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <sstream>

namespace patchplan {
namespace {

std::string trim(const std::string& s) {
    size_t b = 0;
    while (b < s.size() && std::isspace(static_cast<unsigned char>(s[b]))) ++b;
    size_t e = s.size();
    while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) --e;
    return s.substr(b, e - b);
}

// Splits a directive body on commas and trims every token.
std::vector<std::string> splitList(const std::string& body) {
    std::vector<std::string> out;
    std::stringstream ss(body);
    std::string item;
    while (std::getline(ss, item, ',')) {
        std::string t = trim(item);
        if (!t.empty()) out.push_back(t);
    }
    return out;
}

bool isIdent(const std::string& s) {
    if (s.empty()) return false;
    for (char c : s) {
        if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '_' ||
              c == '-' || c == '.')) {
            return false;
        }
    }
    return true;
}

int parsePositiveInt(const std::string& s, int lineNo) {
    if (s.empty()) throw ParseError("line " + std::to_string(lineNo) +
                                    ": expected integer, got empty field");
    int v = 0;
    for (char c : s) {
        if (!std::isdigit(static_cast<unsigned char>(c))) {
            throw ParseError("line " + std::to_string(lineNo) +
                             ": expected non-negative integer, got '" + s + "'");
        }
        v = v * 10 + (c - '0');
    }
    return v;
}

}  // namespace

Plan parsePlanFile(const std::string& path) {
    std::ifstream in(path);
    if (!in) throw ParseError("cannot open plan file: " + path);

    Plan plan;
    std::string raw;
    int lineNo = 0;
    while (std::getline(in, raw)) {
        ++lineNo;
        std::string line = trim(raw);
        if (line.empty() || line[0] == '#') continue;

        std::istringstream ls(line);
        std::string kw;
        ls >> kw;
        const std::string rest = [&] {
            std::string r;
            std::getline(ls, r);
            return trim(r);
        }();
        auto needRest = [&](size_t n) {
            if (rest.empty())
                throw ParseError("line " + std::to_string(lineNo) + ": " + kw +
                                 " requires " + std::to_string(n) + " fields");
        };

        if (kw == "service") {
            // service <name> <initialVersion>
            needRest(2);
            std::istringstream fs(rest);
            std::string name, ver;
            fs >> name >> ver;
            if (!isIdent(name) || !isIdent(ver))
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": invalid service token");
            if (plan.services.count(name))
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": duplicate service '" + name + "'");
            plan.services[name] = Service{name, ver};
        } else if (kw == "patch") {
            // patch <id> <service> <fromVersion> -> <toVersion> [deps=a,b] [mutex=x,y]
            std::istringstream fs(rest);
            std::string id, svc, from, arrow, to;
            fs >> id >> svc >> from >> arrow >> to;
            if (!isIdent(id) || !isIdent(svc) || !isIdent(from) || !isIdent(to) ||
                arrow != "->") {
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": bad patch directive, expected "
                                 "'patch <id> <service> <from> -> <to> ...'");
            }
            if (plan.patches.count(id))
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": duplicate patch id '" + id + "'");
            Patch p;
            p.id = id;
            p.service = svc;
            p.fromVersion = from;
            p.toVersion = to;
            std::string opt;
            while (fs >> opt) {
                auto eq = opt.find('=');
                if (eq == std::string::npos)
                    throw ParseError("line " + std::to_string(lineNo) +
                                     ": malformed patch option '" + opt + "'");
                std::string key = opt.substr(0, eq);
                std::string val = opt.substr(eq + 1);
                if (key == "deps") {
                    for (const auto& d : splitList(val)) p.dependsOn.insert(d);
                } else if (key == "mutex") {
                    for (const auto& m : splitList(val)) p.mutexWith.insert(m);
                } else {
                    throw ParseError("line " + std::to_string(lineNo) +
                                     ": unknown patch option '" + key + "'");
                }
            }
            plan.patches[id] = p;
        } else if (kw == "compat") {
            // compat <serviceA> <versionA> <serviceB> <versionB>
            std::istringstream fs(rest);
            CompatRule r;
            fs >> r.serviceA >> r.versionA >> r.serviceB >> r.versionB;
            if (r.serviceA.empty() || r.serviceB.empty() || r.versionA.empty() ||
                r.versionB.empty()) {
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": compat needs 4 fields");
            }
            plan.compatRules.push_back(r);
        } else if (kw == "window") {
            // window <id> capacity=<n>
            std::istringstream fs(rest);
            std::string id, cap;
            fs >> id >> cap;
            if (!isIdent(id) || cap.rfind("capacity=", 0) != 0)
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": expected 'window <id> capacity=<n>'");
            if (plan.windows.count(id))
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": duplicate window id '" + id + "'");
            Window w;
            w.id = id;
            w.capacity = parsePositiveInt(cap.substr(9), lineNo);
            plan.windows[id] = w;
        } else if (kw == "step") {
            // step <index> <patchId> window=<id>
            std::istringstream fs(rest);
            std::string idx, pid, wopt;
            fs >> idx >> pid >> wopt;
            if (wopt.rfind("window=", 0) != 0)
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": expected 'step <n> <patch> window=<id>'");
            Step st;
            st.index = parsePositiveInt(idx, lineNo);
            st.patchId = pid;
            st.windowId = wopt.substr(7);
            plan.steps.push_back(st);
        } else if (kw == "rollback") {
            // rollback <id> patch=<patchId>
            std::istringstream fs(rest);
            std::string id, popt;
            fs >> id >> popt;
            if (!isIdent(id) || popt.rfind("patch=", 0) != 0)
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": expected 'rollback <id> patch=<patchId>'");
            if (plan.rollbacks.count(id))
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": duplicate rollback id '" + id + "'");
            RollbackAction ra;
            ra.id = id;
            ra.patchId = popt.substr(6);
            plan.rollbacks[id] = ra;
        } else if (kw == "failure") {
            // failure <id> after=<n> budget=<n>
            std::istringstream fs(rest);
            std::string id, aopt, bopt;
            fs >> id >> aopt >> bopt;
            if (!isIdent(id) || aopt.rfind("after=", 0) != 0 ||
                bopt.rfind("budget=", 0) != 0) {
                throw ParseError("line " + std::to_string(lineNo) +
                                 ": expected 'failure <id> after=<n> budget=<n>'");
            }
            Failure f;
            f.id = id;
            f.afterStepCount = parsePositiveInt(aopt.substr(6), lineNo);
            f.rollbackBudget = parsePositiveInt(bopt.substr(7), lineNo);
            plan.failures.push_back(f);
        } else {
            throw ParseError("line " + std::to_string(lineNo) +
                             ": unknown directive '" + kw + "'");
        }
    }

    // ---- referential validation ----------------------------------------
    if (plan.services.empty()) throw ParseError("plan declares no services");
    if (plan.patches.empty()) throw ParseError("plan declares no patches");

    for (const auto& [id, p] : plan.patches) {
        if (!plan.services.count(p.service))
            throw ParseError("patch '" + id + "' references unknown service '" +
                             p.service + "'");
        for (const auto& d : p.dependsOn) {
            if (!plan.patches.count(d))
                throw ParseError("patch '" + id +
                                 "' depends on unknown patch '" + d + "'");
        }
        for (const auto& m : p.mutexWith) {
            if (!plan.patches.count(m))
                throw ParseError("patch '" + id +
                                 "' declares mutex with unknown patch '" + m +
                                 "'");
        }
    }
    for (const auto& r : plan.compatRules) {
        if (!plan.services.count(r.serviceA) || !plan.services.count(r.serviceB))
            throw ParseError("compat rule references unknown service");
    }
    if (plan.steps.empty()) throw ParseError("plan contains no steps");
    for (const auto& st : plan.steps) {
        if (!plan.patches.count(st.patchId))
            throw ParseError("step " + std::to_string(st.index) +
                             " references unknown patch '" + st.patchId + "'");
        if (!plan.windows.count(st.windowId))
            throw ParseError("step " + std::to_string(st.index) +
                             " references unknown window '" + st.windowId + "'");
    }
    // Indices must be 1..N unique and appear in ascending order in the file.
    for (size_t i = 0; i < plan.steps.size(); ++i) {
        if (plan.steps[i].index != static_cast<int>(i) + 1)
            throw ParseError("step indices must be 1..N in order; bad index " +
                             std::to_string(plan.steps[i].index));
    }
    for (const auto& [id, ra] : plan.rollbacks) {
        if (!plan.patches.count(ra.patchId))
            throw ParseError("rollback '" + id +
                             "' references unknown patch '" + ra.patchId + "'");
    }
    for (const auto& f : plan.failures) {
        if (f.afterStepCount < 1 ||
            f.afterStepCount > static_cast<int>(plan.steps.size()))
            throw ParseError("failure '" + f.id +
                             "' after-count out of step range");
    }
    return plan;
}

}  // namespace patchplan
