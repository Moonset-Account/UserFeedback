#include "bootmgr.hpp"

#include <fstream>
#include <sstream>
#include <stdexcept>

namespace bootmgr {

const char* toString(SigStatus s) {
    switch (s) {
    case SigStatus::Valid: return "valid";
    case SigStatus::Invalid: return "INVALID";
    case SigStatus::Unknown: return "unknown";
    }
    return "?";
}

const char* toString(SlotState s) {
    switch (s) {
    case SlotState::Empty: return "empty";
    case SlotState::Unconfirmed: return "unconfirmed";
    case SlotState::Confirmed: return "confirmed";
    case SlotState::Unbootable: return "unbootable";
    }
    return "?";
}

const char* toString(MachineState s) {
    switch (s) {
    case MachineState::Select: return "SELECT";
    case MachineState::AwaitingConfirm: return "AWAITING_CONFIRM";
    case MachineState::Running: return "RUNNING";
    case MachineState::Recovery: return "RECOVERY";
    }
    return "?";
}

SigStatus parseSig(const std::string& token) {
    if (token == "valid") return SigStatus::Valid;
    if (token == "invalid") return SigStatus::Invalid;
    if (token == "unknown") return SigStatus::Unknown;
    throw std::runtime_error("bad signature token: " + token);
}

SlotState parseSlotState(const std::string& token) {
    if (token == "empty") return SlotState::Empty;
    if (token == "unconfirmed") return SlotState::Unconfirmed;
    if (token == "confirmed") return SlotState::Confirmed;
    if (token == "unbootable") return SlotState::Unbootable;
    throw std::runtime_error("bad slot-state token: " + token);
}

// ---------------------------------------------------------------------------
// 日志实现
// ---------------------------------------------------------------------------
namespace {

class FileJournal : public IJournal {
public:
    explicit FileJournal(std::string path) : path_(std::move(path)) {}

    void append(const std::string& line) override {
        std::ofstream out(path_, std::ios::app);
        if (!out) throw std::runtime_error("cannot open journal: " + path_);
        out << line << '\n';
        out.flush();
    }

    std::vector<std::string> readAll() const override {
        std::vector<std::string> lines;
        std::ifstream in(path_);
        std::string line;
        while (std::getline(in, line)) {
            if (!line.empty()) lines.push_back(line);
        }
        return lines;
    }

    void clear() override {
        std::ofstream out(path_, std::ios::trunc);
    }

private:
    std::string path_;
};

class MemoryJournal : public IJournal {
public:
    void append(const std::string& line) override { lines_.push_back(line); }
    std::vector<std::string> readAll() const override { return lines_; }
    void clear() override { lines_.clear(); }

private:
    std::vector<std::string> lines_;
};

} // namespace

std::shared_ptr<IJournal> makeFileJournal(const std::string& path) {
    return std::make_shared<FileJournal>(path);
}

std::shared_ptr<IJournal> makeMemoryJournal() {
    return std::make_shared<MemoryJournal>();
}

// ---------------------------------------------------------------------------
// PersistentStore
//
// 日志行格式：
//   TXN_BEGIN <seq> <label>
//   SET <key> <value...>          （value 可含空格的行用 '=' 形式，见 putSlot）
//   TXN_COMMIT <seq>
//
// 键约定：floor / max_retries / active / pending / slot.<id>.<field>
// 槽位行：SET slot.<id> <version> <sig> <state> <retries>
// ---------------------------------------------------------------------------
PersistentStore::PersistentStore(std::shared_ptr<IJournal> journal)
    : journal_(std::move(journal)) {
    replay();
}

void PersistentStore::loadFactoryRecord(const DeviceRecord& rec) {
    if (seq_ != 0 || journal_->readAll().empty() == false) {
        throw std::runtime_error("factory record can only be loaded once");
    }
    rec_ = DeviceRecord{};
    beginTxn("factory-record");
    putFloor(rec.floor);
    if (rec.max_retries != 0) rec_.max_retries = rec.max_retries;
    buffered_.emplace_back("max_retries", std::to_string(rec.max_retries));
    putActive(rec.active_slot);
    putPending(rec.pending_slot);
    for (const auto& [id, slot] : rec.slots) putSlot(slot);
    commit();
}

void PersistentStore::beginTxn(const std::string& label) {
    if (txn_open_) throw std::runtime_error("nested transaction");
    txn_open_ = true;
    txn_label_ = label;
    buffered_.clear();
    journal_->append("TXN_BEGIN " + std::to_string(seq_ + 1) + " " + label);
}

void PersistentStore::putFloor(int v) {
    if (!txn_open_) throw std::runtime_error("write outside transaction");
    buffered_.emplace_back("floor", std::to_string(v));
}

void PersistentStore::putPending(const std::string& id) {
    if (!txn_open_) throw std::runtime_error("write outside transaction");
    buffered_.emplace_back("pending", id);
}

void PersistentStore::putActive(const std::string& id) {
    if (!txn_open_) throw std::runtime_error("write outside transaction");
    buffered_.emplace_back("active", id);
}

void PersistentStore::putSlot(const SlotRecord& s) {
    if (!txn_open_) throw std::runtime_error("write outside transaction");
    std::ostringstream v;
    v << s.version << ' ' << toString(s.sig) << ' ' << toString(s.state)
      << ' ' << s.retries_left;
    buffered_.emplace_back("slot." + s.id, v.str());
}

void PersistentStore::commit() {
    if (!txn_open_) throw std::runtime_error("commit without begin");
    for (const auto& [key, value] : buffered_) {
        journal_->append("SET " + key + " " + value);
    }
    journal_->append("TXN_COMMIT " + std::to_string(seq_ + 1));

    // 提交成功后才应用到内存工作副本（写穿语义）
    for (const auto& [key, value] : buffered_) applySet(key, value);
    ++seq_;
    audit_.push_back("txn#" + std::to_string(seq_) + " [" + txn_label_ +
                     "] committed: floor=" + std::to_string(rec_.floor) +
                     " active=" + (rec_.active_slot.empty() ? "-" : rec_.active_slot) +
                     " pending=" + (rec_.pending_slot.empty() ? "-" : rec_.pending_slot));
    txn_open_ = false;
    txn_label_.clear();
    buffered_.clear();
}

void PersistentStore::applySet(const std::string& key, const std::string& value) {
    if (key == "floor") {
        rec_.floor = std::stoi(value);
    } else if (key == "max_retries") {
        rec_.max_retries = std::stoi(value);
    } else if (key == "active") {
        rec_.active_slot = value;
    } else if (key == "pending") {
        rec_.pending_slot = value;
    } else if (key.rfind("slot.", 0) == 0) {
        std::string id = key.substr(5);
        std::istringstream in(value);
        SlotRecord s;
        s.id = id;
        std::string sig, state;
        in >> s.version >> sig >> state >> s.retries_left;
        s.sig = parseSig(sig);
        s.state = parseSlotState(state);
        rec_.slots[id] = s;
    } else {
        throw std::runtime_error("unknown journal key: " + key);
    }
}

void PersistentStore::replay() {
    rec_ = DeviceRecord{};
    seq_ = 0;
    audit_.clear();

    const auto lines = journal_->readAll();
    size_t i = 0;
    std::vector<std::string> compacted; // 恢复后保留的有效日志（未提交尾部被截断）
    while (i < lines.size()) {
        if (lines[i].rfind("TXN_BEGIN ", 0) != 0) {
            ++i; // 游离行（不可能由正常流程产生），跳过
            continue;
        }
        std::istringstream head(lines[i]);
        std::string tag;
        int seq = 0;
        head >> tag >> seq;
        std::string label;
        std::getline(head, label);
        if (!label.empty() && label.front() == ' ') label.erase(0, 1);

        std::vector<std::string> txn_lines{lines[i]};
        std::vector<std::pair<std::string, std::string>> sets;
        size_t j = i + 1;
        bool committed = false;
        while (j < lines.size() && lines[j].rfind("TXN_BEGIN ", 0) != 0) {
            if (lines[j].rfind("TXN_COMMIT ", 0) == 0) {
                committed = true;
                txn_lines.push_back(lines[j]);
                break;
            }
            if (lines[j].rfind("SET ", 0) == 0) {
                txn_lines.push_back(lines[j]);
                std::string body = lines[j].substr(4);
                auto sp = body.find(' ');
                sets.emplace_back(body.substr(0, sp), body.substr(sp + 1));
            }
            ++j;
        }

        if (!committed) {
            // 断电发生在 commit 之前：整个事务（含可能写了一半的 SET）丢弃，
            // 并截断物理日志尾部——真实启动器恢复时对未提交区域做的就是这件事
            audit_.push_back("replay: txn#" + std::to_string(seq) + " [" + label +
                             "] NOT committed before power loss -> discarded and journal truncated");
            break;
        }
        for (const auto& [key, value] : sets) applySet(key, value);
        seq_ = seq;
        audit_.push_back("replay: txn#" + std::to_string(seq) + " [" + label +
                         "] restored (floor=" + std::to_string(rec_.floor) +
                         " active=" + (rec_.active_slot.empty() ? "-" : rec_.active_slot) +
                         " pending=" + (rec_.pending_slot.empty() ? "-" : rec_.pending_slot) +
                         ")");
        compacted.insert(compacted.end(), txn_lines.begin(), txn_lines.end());
        i = j + 1;
    }

    // 日志压实：把撕裂/未提交内容从持久介质上移除，后续事务获得干净的序号空间
    journal_->clear();
    for (const auto& l : compacted) journal_->append(l);
}

void PersistentStore::reload() {
    // 模拟整机断电：内存工作副本完全失效，只凭日志重建
    txn_open_ = false;
    txn_label_.clear();
    buffered_.clear();
    replay();
}

} // namespace bootmgr
