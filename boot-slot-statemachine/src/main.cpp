#include "bootmgr.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

using namespace bootmgr;

namespace {

std::vector<std::string> tokenize(const std::string& line) {
    std::vector<std::string> out;
    std::istringstream in(line);
    std::string tok;
    while (in >> tok) out.push_back(tok);
    return out;
}

struct Scenario {
    std::string name;
    DeviceRecord manifest;
    std::vector<Event> events;
    std::vector<std::string> expect_lines;
};

// 清单/事件/期望三段式，'#' 起始为注释
Scenario parseScenario(const std::filesystem::path& path) {
    Scenario sc;
    sc.name = path.stem().string();

    std::ifstream in(path);
    if (!in) throw std::runtime_error("cannot open scenario: " + path.string());

    enum class Section { None, Manifest, Events, Expect } section = Section::None;
    std::string line;
    while (std::getline(in, line)) {
        auto pos = line.find('#');
        if (pos != std::string::npos) line.erase(pos);
        auto t = tokenize(line);
        if (t.empty()) continue;

        if (t[0].front() == '[') {
            std::string h = t[0];
            if (h == "[manifest]") section = Section::Manifest;
            else if (h == "[events]") section = Section::Events;
            else if (h == "[expect]") section = Section::Expect;
            else throw std::runtime_error("bad section header: " + h);
            continue;
        }

        if (section == Section::Manifest) {
            if (t[0] == "floor") sc.manifest.floor = std::stoi(t[1]);
            else if (t[0] == "max_retries") sc.manifest.max_retries = std::stoi(t[1]);
            else if (t[0] == "active") sc.manifest.active_slot = (t[1] == "-" ? "" : t[1]);
            else if (t[0] == "pending") sc.manifest.pending_slot = (t[1] == "-" ? "" : t[1]);
            else if (t[0] == "slot") {
                SlotRecord s;
                s.id = t[1];
                s.version = std::stoi(t[2]);
                s.sig = parseSig(t[3]);
                s.state = parseSlotState(t[4]);
                s.retries_left = t.size() > 5 ? std::stoi(t[5])
                                              : (s.state == SlotState::Unconfirmed
                                                     ? sc.manifest.max_retries
                                                     : 0);
                sc.manifest.slots[s.id] = s;
            } else throw std::runtime_error("bad manifest line: " + line);
        } else if (section == Section::Events) {
            Event ev;
            if (t[0] == "update") {
                ev.kind = Event::Kind::Update;
                ev.target = t[1];
                ev.version = std::stoi(t[2]);
                ev.min_version = std::stoi(t[3]);
                ev.sig = parseSig(t[4]);
            } else if (t[0] == "boot") {
                ev.kind = Event::Kind::Boot;
                ev.outcome = (t[1] == "ok") ? BootOutcome::Ok : BootOutcome::Fail;
            } else if (t[0] == "confirm") {
                ev.kind = Event::Kind::Confirm;
            } else if (t[0] == "power-loss" || t[0] == "powerloss") {
                ev.kind = Event::Kind::PowerLoss;
            } else throw std::runtime_error("bad event: " + line);
            sc.events.push_back(ev);
        } else if (section == Section::Expect) {
            sc.expect_lines.push_back(line);
        }
    }
    return sc;
}

bool checkExpect(const Scenario& sc, const DeviceRecord& rec, MachineState mstate,
                 const std::string& candidate, std::vector<std::string>& failures) {
    for (const auto& raw : sc.expect_lines) {
        auto t = tokenize(raw);
        auto fail = [&](const std::string& got) {
            failures.push_back("  expect '" + raw + "' but got " + got);
        };

        if (t[0] == "state") {
            std::string want = t[1];
            std::string got = toString(mstate);
            if (want != got) fail(got);
        } else if (t[0] == "floor") {
            if (rec.floor != std::stoi(t[1])) fail(std::to_string(rec.floor));
        } else if (t[0] == "active") {
            std::string want = (t[1] == "-" ? "" : t[1]);
            if (rec.active_slot != want) fail(rec.active_slot.empty() ? "-" : rec.active_slot);
        } else if (t[0] == "pending") {
            std::string want = (t[1] == "-" ? "" : t[1]);
            if (rec.pending_slot != want) fail(rec.pending_slot.empty() ? "-" : rec.pending_slot);
        } else if (t[0] == "candidate") {
            std::string want = t[1];
            if (want == "recovery") {
                if (mstate != MachineState::Recovery) fail(toString(mstate));
            } else if (want == "none") {
                if (!candidate.empty()) fail(candidate);
            } else if (candidate != want) {
                fail(candidate.empty() ? "none" : candidate);
            }
        } else if (t[0] == "slot") {
            auto it = rec.slots.find(t[1]);
            if (it == rec.slots.end()) { fail("slot missing"); continue; }
            const SlotRecord& s = it->second;
            if (s.version != std::stoi(t[2])) fail("v" + std::to_string(s.version));
            else if (toString(s.state) != t[3]) fail(toString(s.state));
            else if (t.size() > 4 && s.retries_left != std::stoi(t[4]))
                fail("retries=" + std::to_string(s.retries_left));
        } else {
            throw std::runtime_error("bad expect line: " + raw);
        }
    }
    return failures.empty();
}

void printSnapshot(const DeviceRecord& rec) {
    std::cout << "  final snapshot: floor=" << rec.floor
              << " active=" << (rec.active_slot.empty() ? "-" : rec.active_slot)
              << " pending=" << (rec.pending_slot.empty() ? "-" : rec.pending_slot) << "\n";
    for (const auto& [id, s] : rec.slots) {
        std::cout << "    slot " << id << ": v" << s.version << " sig=" << toString(s.sig)
                  << " state=" << toString(s.state) << " retries=" << s.retries_left << "\n";
    }
}

bool runScenario(const Scenario& sc) {
    std::cout << "============================================================\n";
    std::cout << "SCENARIO: " << sc.name << "\n";
    std::cout << "============================================================\n";

    const auto jpath = std::filesystem::temp_directory_path() /
                       ("bootmgr_" + sc.name + "_" +
                        std::to_string(static_cast<long long>(
                            std::chrono::steady_clock::now().time_since_epoch().count())) +
                        ".journal");
    auto journal = makeFileJournal(jpath.string());
    journal->clear();

    std::vector<std::string> trace;
    PersistentStore store(journal);
    store.loadFactoryRecord(sc.manifest);
    std::optional<StateMachine> sm;
    sm.emplace(store, trace);

    for (const Event& ev : sc.events) {
        if (ev.kind == Event::Kind::PowerLoss) {
            trace.push_back("event: POWER-LOSS (RAM lost; durable journal survives)");
            store.reload();
            sm.emplace(store, trace); // 重启：状态机仅依据持久化状态重建
            continue;
        }
        sm->handle(ev);
    }

    for (const auto& line : trace) std::cout << line << "\n";
    DeviceRecord rec = store.snapshot();
    printSnapshot(rec);

    std::vector<std::string> failures;
    bool ok = checkExpect(sc, rec, sm->state(), sm->candidate(), failures);
    if (ok) {
        std::cout << "RESULT: PASS\n\n";
    } else {
        std::cout << "RESULT: FAIL\n";
        for (const auto& f : failures) std::cout << f << "\n";
        std::cout << "\n";
    }
    std::filesystem::remove(jpath);
    return ok;
}

// 内置自检：用内存日志直接构造撕裂写入，验证未提交事务在 reload 时被整体丢弃
bool runTornWriteSelfTest() {
    std::cout << "============================================================\n";
    std::cout << "SELFTEST: torn journal / crash-amid-transaction durability\n";
    std::cout << "============================================================\n";
    std::vector<std::string> trace;
    auto journal = makeMemoryJournal();
    PersistentStore store(journal);

    DeviceRecord factory;
    factory.floor = 10;
    factory.max_retries = 3;
    factory.active_slot = "A";
    factory.slots["A"] = SlotRecord{"A", 10, SigStatus::Valid, SlotState::Confirmed, 0};
    factory.slots["B"] = SlotRecord{"B", 0, SigStatus::Unknown, SlotState::Empty, 0};
    store.loadFactoryRecord(factory);

    // 开始安装事务并写了一半（模拟断电发生在 commit 之前）
    store.beginTxn("update-install-commit B");
    store.putSlot(SlotRecord{"B", 12, SigStatus::Valid, SlotState::Unconfirmed, 3});
    store.putPending("B");
    store.putFloor(12);
    // 故意不 commit，并再追加一行物理上写了一半的数据
    journal->append("SET slot.B 12 valid unconf"); // 撕裂行，且整个事务无 COMMIT

    std::cout << "power loss before commit; journal tail contains TXN_BEGIN without TXN_COMMIT\n";
    store.reload();
    DeviceRecord r = store.snapshot();

    bool ok = true;
    auto expect = [&](bool cond, const std::string& label, const std::string& got) {
        std::cout << "  [" << (cond ? "ok" : "FAIL") << "] " << label << " => " << got << "\n";
        ok &= cond;
    };
    expect(r.floor == 10, "floor rolled back to committed value", std::to_string(r.floor));
    expect(r.pending_slot.empty(), "pending pointer absent", r.pending_slot.empty() ? "-" : r.pending_slot);
    expect(r.slots.at("B").state == SlotState::Empty, "target slot untouched",
           toString(r.slots.at("B").state));
    expect(r.active_slot == "A", "active slot preserved", r.active_slot);

    // 对照：完整提交的事务在断电后必须保留
    PersistentStore store2(journal);
    store2.beginTxn("update-install-commit B");
    store2.putSlot(SlotRecord{"B", 12, SigStatus::Valid, SlotState::Unconfirmed, 3});
    store2.putPending("B");
    store2.putFloor(12);
    store2.commit();
    store2.reload();
    DeviceRecord r2 = store2.snapshot();
    expect(r2.floor == 12 && r2.pending_slot == "B" &&
               r2.slots.at("B").state == SlotState::Unconfirmed,
           "committed transaction survives power loss",
           "floor=" + std::to_string(r2.floor) + " pending=" + r2.pending_slot);

    std::cout << "RESULT: " << (ok ? "PASS\n\n" : "FAIL\n\n");
    return ok;
}

} // namespace

int main(int argc, char** argv) {
    std::vector<std::filesystem::path> paths;
    bool selftest = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--selftest") selftest = true;
        else paths.emplace_back(a);
    }
    if (paths.empty()) paths.emplace_back("scenarios");

    std::vector<std::filesystem::path> files;
    for (const auto& p : paths) {
        if (std::filesystem::is_directory(p)) {
            for (const auto& e : std::filesystem::directory_iterator(p))
                if (e.path().extension() == ".scenario") files.push_back(e.path());
        } else {
            files.push_back(p);
        }
    }
    std::sort(files.begin(), files.end());

    int failed = 0, total = 0;
    if (selftest) { ++total; if (!runTornWriteSelfTest()) ++failed; }
    for (const auto& f : files) {
        ++total;
        try {
            if (!runScenario(parseScenario(f))) ++failed;
        } catch (const std::exception& e) {
            ++failed;
            std::cout << "SCENARIO " << f << " ERROR: " << e.what() << "\n";
        }
    }

    std::cout << "============================================================\n";
    std::cout << "SUMMARY: " << (total - failed) << "/" << total << " passed, "
              << failed << " failed\n";
    return failed == 0 ? 0 : 1;
}
