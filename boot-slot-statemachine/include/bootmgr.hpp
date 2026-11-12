#pragma once

// 离线 A/B 启动槽状态机：核心类型与接口定义。
// 安全模型：安全下限 floor（最低可启动版本）是候选选择的强制过滤条件，
// 任何签名无效或 version < floor 的槽位都不得作为回退项，否则进入 RECOVERY。

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace bootmgr {

enum class SigStatus { Unknown, Valid, Invalid };
enum class SlotState { Empty, Unconfirmed, Confirmed, Unbootable };
enum class MachineState { Select, AwaitingConfirm, Running, Recovery };
enum class BootOutcome { Ok, Fail };

const char* toString(SigStatus s);
const char* toString(SlotState s);
const char* toString(MachineState s);
SigStatus parseSig(const std::string& token);
SlotState parseSlotState(const std::string& token);

// 单个槽位的持久化记录
struct SlotRecord {
    std::string id;
    int version = 0;
    SigStatus sig = SigStatus::Unknown;
    SlotState state = SlotState::Empty;
    int retries_left = 0; // 仅 Unconfirmed 槽位使用，启动尝试开始时递减
};

// 设备级持久化记录（全部位于持久化替身上）
struct DeviceRecord {
    int floor = 0;                       // 安全下限：最低可接受版本
    int max_retries = 3;                 // 未确认槽位的最大启动尝试次数
    std::string active_slot;             // 最近一次确认运行的槽位，空串表示无
    std::string pending_slot;            // 已安装但尚未确认的更新目标槽位
    std::map<std::string, SlotRecord> slots;
};

struct Event {
    enum class Kind { Update, Boot, Confirm, PowerLoss } kind = Kind::Boot;
    // Update 事件字段
    std::string target;
    int version = 0;
    int min_version = 0;
    SigStatus sig = SigStatus::Unknown;
    // Boot 事件字段
    BootOutcome outcome = BootOutcome::Ok;
};

struct Rejection {
    std::string slot;
    std::string reason;
};

struct SelectionDecision {
    std::optional<std::string> candidate;
    std::vector<Rejection> rejections;
    bool enters_recovery = false;
};

// ---------------------------------------------------------------------------
// 持久化介质抽象（持久化替身）。FileJournal 模拟闪存上的追加日志；
// MemoryJournal 用于单元测试撕裂写入。
// ---------------------------------------------------------------------------
class IJournal {
public:
    virtual ~IJournal() = default;
    virtual void append(const std::string& line) = 0;
    virtual std::vector<std::string> readAll() const = 0;
    virtual void clear() = 0;
};

std::shared_ptr<IJournal> makeFileJournal(const std::string& path);
std::shared_ptr<IJournal> makeMemoryJournal();

// ---------------------------------------------------------------------------
// 事务日志式持久化存储。
// 所有状态变更都必须包在 beginTxn()/commit() 之间：
//   日志先写 TXN_BEGIN，再写若干 SET，最后写 TXN_COMMIT；
//   重放时只接收带 COMMIT 的事务，未提交的尾部（断电撕裂写入）直接丢弃。
// ---------------------------------------------------------------------------
class PersistentStore {
public:
    explicit PersistentStore(std::shared_ptr<IJournal> journal);

    // 出厂清单作为事务 #1 写入日志，保证断电重放有基线
    void loadFactoryRecord(const DeviceRecord& rec);

    // 模拟断电：丢弃内存工作副本，只凭日志重放重建
    void reload();

    DeviceRecord snapshot() const { return rec_; }
    const std::vector<std::string>& auditLog() const { return audit_; }

    void beginTxn(const std::string& label);
    void putFloor(int v);
    void putPending(const std::string& id);
    void putActive(const std::string& id);
    void putSlot(const SlotRecord& s);
    void commit();
    bool inTxn() const { return txn_open_; }

private:
    void applySet(const std::string& key, const std::string& value);
    void replay();

    std::shared_ptr<IJournal> journal_;
    DeviceRecord rec_;
    int seq_ = 0;
    bool txn_open_ = false;
    std::string txn_label_;
    std::vector<std::pair<std::string, std::string>> buffered_;
    std::vector<std::string> audit_;
};

// ---------------------------------------------------------------------------
// 离线状态机。重启后不保留任何 RAM 状态，完全依据 PersistentStore 的内容决策。
// ---------------------------------------------------------------------------
class StateMachine {
public:
    StateMachine(PersistentStore& store, std::vector<std::string>& trace);

    MachineState state() const { return state_; }
    std::string candidate() const { return candidate_; }

    void handle(const Event& ev);

    // 候选选择，单独暴露以便报告每次启动的完整判定过程
    SelectionDecision selectCandidate(const char* tag);

private:
    void onUpdate(const Event& ev);
    void onBoot(BootOutcome outcome);
    void onConfirm();
    void flushAudit();

    PersistentStore& store_;
    std::vector<std::string>& trace_;
    MachineState state_ = MachineState::Select;
    std::string candidate_;
    size_t audit_seen_ = 0;
};

} // namespace bootmgr
