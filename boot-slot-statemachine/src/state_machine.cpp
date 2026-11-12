#include "bootmgr.hpp"

#include <algorithm>
#include <sstream>
#include <stdexcept>

namespace bootmgr {

StateMachine::StateMachine(PersistentStore& store, std::vector<std::string>& trace)
    : store_(store), trace_(trace) {
    trace_.push_back("state machine (re)started; rebuilding decisions purely from persisted state");
    flushAudit(); // 重放/已提交事务会在这里进入报告
}

void StateMachine::flushAudit() {
    const auto& audit = store_.auditLog();
    while (audit_seen_ < audit.size()) {
        trace_.push_back("  [persist] " + audit[audit_seen_++]);
    }
}

SelectionDecision StateMachine::selectCandidate(const char* tag) {
    DeviceRecord rec = store_.snapshot();
    SelectionDecision d;

    {
        std::ostringstream os;
        os << "[select:" << tag << "] floor=" << rec.floor
           << " active=" << (rec.active_slot.empty() ? "-" : rec.active_slot)
           << " pending=" << (rec.pending_slot.empty() ? "-" : rec.pending_slot);
        trace_.push_back(os.str());
    }

    std::vector<const SlotRecord*> eligible;
    for (const auto& [id, slot] : rec.slots) {
        std::string verdict;
        bool ok = true;
        if (slot.state == SlotState::Empty) {
            ok = false;
            verdict = "rejected: slot empty";
        } else if (slot.state == SlotState::Unbootable) {
            ok = false;
            verdict = "rejected: previously marked unbootable";
        } else if (slot.sig != SigStatus::Valid) {
            ok = false;
            verdict = std::string("rejected: signature ") + toString(slot.sig);
        } else if (slot.version < rec.floor) {
            ok = false;
            std::ostringstream os;
            os << "rejected: version " << slot.version << " below security floor " << rec.floor;
            verdict = os.str();
        }

        std::ostringstream os;
        os << "    slot " << id << " v" << slot.version << " sig=" << toString(slot.sig)
           << " state=" << toString(slot.state)
           << (slot.state == SlotState::Unconfirmed
                   ? " retries=" + std::to_string(slot.retries_left)
                   : "");
        if (ok) {
            os << " -> eligible";
            eligible.push_back(&slot);
        } else {
            os << " -> " << verdict;
            d.rejections.push_back({id, verdict});
        }
        trace_.push_back(os.str());
    }

    if (eligible.empty()) {
        d.enters_recovery = true;
        trace_.push_back("  => no eligible slot (floor enforced, no silent rollback) -> ENTER RECOVERY");
        return d;
    }

    // 优先级：pending（待确认更新）> active（当前已确认槽）> 版本更高者 > id
    std::sort(eligible.begin(), eligible.end(), [&](const SlotRecord* a, const SlotRecord* b) {
        auto rank = [&](const SlotRecord* s) {
            if (s->id == rec.pending_slot) return 0;
            if (s->id == rec.active_slot) return 1;
            return 2;
        };
        int ra = rank(a), rb = rank(b);
        if (ra != rb) return ra < rb;
        if (a->version != b->version) return a->version > b->version;
        return a->id < b->id;
    });

    const SlotRecord* pick = eligible.front();
    d.candidate = pick->id;
    std::string why;
    if (pick->id == rec.pending_slot) why = "pending unconfirmed update";
    else if (pick->id == rec.active_slot) why = "active confirmed slot";
    else why = "highest eligible version";
    trace_.push_back("  => candidate = " + pick->id + " (v" +
                     std::to_string(pick->version) + ", " + why + ")");
    return d;
}

void StateMachine::handle(const Event& ev) {
    switch (ev.kind) {
    case Event::Kind::Update: onUpdate(ev); break;
    case Event::Kind::Boot: onBoot(ev.outcome); break;
    case Event::Kind::Confirm: onConfirm(); break;
    case Event::Kind::PowerLoss:
        // 断电由驱动层执行 store.reload() 并重建状态机；走到这里说明场景配置错误
        throw std::runtime_error("power-loss must be handled by the driver via reload()");
    }
    flushAudit();
}

void StateMachine::onUpdate(const Event& ev) {
    trace_.push_back("event: UPDATE target=" + ev.target + " v" + std::to_string(ev.version) +
                     " min_version=" + std::to_string(ev.min_version) +
                     " sig=" + toString(ev.sig));

    DeviceRecord rec = store_.snapshot();
    auto it = rec.slots.find(ev.target);
    if (it == rec.slots.end()) throw std::runtime_error("unknown target slot: " + ev.target);

    if (ev.sig != SigStatus::Valid) {
        trace_.push_back("  => update REJECTED before any write: signature " +
                         std::string(toString(ev.sig)) + "; floor stays " +
                         std::to_string(rec.floor));
        return;
    }
    if (ev.version < rec.floor) {
        trace_.push_back("  => update REJECTED before any write: version " +
                         std::to_string(ev.version) + " is below persisted floor " +
                         std::to_string(rec.floor) + " (rollback blocked)");
        return;
    }

    // 写入时机（单一事务，提交点在首次重启之前）：
    //   1) 目标槽改为 unconfirmed，重试次数初始化为 max_retries
    //   2) pending 指向目标槽
    //   3) 安全下限抬高到 max(当前 floor, 包内 min_version)——下限只升不降
    int new_floor = std::max(rec.floor, ev.min_version);
    store_.beginTxn("update-install-commit " + ev.target);
    SlotRecord s = it->second;
    s.version = ev.version;
    s.sig = SigStatus::Valid;
    s.state = SlotState::Unconfirmed;
    s.retries_left = rec.max_retries;
    store_.putSlot(s);
    store_.putPending(ev.target);
    store_.putFloor(new_floor);
    store_.commit();

    trace_.push_back("  => install committed: slot " + ev.target +
                     " unconfirmed; floor " + std::to_string(rec.floor) + " -> " +
                     std::to_string(new_floor) +
                     "; next boot must select it via persisted pending pointer");
    state_ = MachineState::Select;
}

void StateMachine::onBoot(BootOutcome outcome) {
    trace_.push_back(std::string("event: BOOT ") +
                     (outcome == BootOutcome::Ok ? "ok" : "FAIL"));

    SelectionDecision d = selectCandidate("boot");
    if (d.enters_recovery) {
        state_ = MachineState::Recovery;
        candidate_.clear();
        return;
    }
    candidate_ = *d.candidate;

    DeviceRecord rec = store_.snapshot();
    SlotRecord slot = rec.slots.at(candidate_);

    // 未确认槽：重试计数在尝试开始前就持久化递减，
    // 这样启动过程中断电也会消耗一次尝试（防止无限重试受损镜像）
    if (slot.state == SlotState::Unconfirmed) {
        slot.retries_left -= 1;
        store_.beginTxn("boot-attempt " + candidate_);
        store_.putSlot(slot);
        store_.commit();
        trace_.push_back("  retries decremented before attempt -> " +
                         std::to_string(slot.retries_left) + " left");
    }

    if (outcome == BootOutcome::Ok) {
        if (slot.state == SlotState::Unconfirmed) {
            state_ = MachineState::AwaitingConfirm;
            trace_.push_back("  boot ok on unconfirmed slot " + candidate_ +
                             "; awaiting update-confirmation record (not yet committed)");
        } else {
            state_ = MachineState::Running;
            trace_.push_back("  boot ok on confirmed slot " + candidate_ + "; running");
        }
        return;
    }

    // ---- 启动失败 ----
    trace_.push_back("  boot FAILED on " + candidate_);
    rec = store_.snapshot();
    slot = rec.slots.at(candidate_);

    if (slot.state == SlotState::Unconfirmed && slot.retries_left > 0) {
        state_ = MachineState::Select;
        trace_.push_back("  unconfirmed slot still has retries; retry same candidate on next boot");
        return;
    }

    // 重试耗尽（或已确认槽损坏）：永久标记不可启动，并解除 pending 指向
    store_.beginTxn("mark-unbootable " + candidate_);
    slot.state = SlotState::Unbootable;
    slot.retries_left = 0;
    store_.putSlot(slot);
    if (rec.pending_slot == candidate_) store_.putPending("");
    store_.commit();
    trace_.push_back("  slot " + candidate_ + " marked unbootable; pending cleared");

    // 关键安全点：失败后的回退选择同样强制套用已持久化的 floor
    SelectionDecision fb = selectCandidate("fallback after failure");
    if (fb.enters_recovery) {
        state_ = MachineState::Recovery;
        candidate_.clear();
    } else {
        candidate_ = *fb.candidate;
        state_ = MachineState::Select;
    }
}

void StateMachine::onConfirm() {
    trace_.push_back("event: CONFIRM (write update-confirmation record)");
    if (state_ != MachineState::AwaitingConfirm) {
        trace_.push_back("  => ignored: no unconfirmed slot awaiting confirmation; no write");
        return;
    }

    DeviceRecord rec = store_.snapshot();
    std::string id = rec.pending_slot;
    if (id.empty() || !rec.slots.count(id)) {
        throw std::runtime_error("awaiting confirm but persisted pending pointer is invalid");
    }
    SlotRecord slot = rec.slots.at(id);

    // 写入时机：只有新槽实际启动成功后才写确认记录
    store_.beginTxn("update-confirmation " + id);
    slot.state = SlotState::Confirmed;
    slot.retries_left = 0;
    store_.putSlot(slot);
    store_.putActive(id);
    store_.putPending("");
    store_.commit();

    trace_.push_back("  => slot " + id + " confirmed; active pointer updated; pending cleared");
    state_ = MachineState::Running;
}

} // namespace bootmgr
