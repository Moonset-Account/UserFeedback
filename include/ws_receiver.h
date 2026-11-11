// SPDX-License-Identifier: MIT
//
// ws_receiver.h — 安全的 WebSocket 接收生命周期模块（C++17）
//
// 本头文件只声明模块对外契约，实现见 ws_receiver.cpp。
//
// 设计要点（对应任务要求）：
//   1. 缓冲池是“可记录”的：每次 acquire / release 都写入事件日志，
//      release 带“原因”（派发完成 / 陈旧任务清理 / 入站前拒绝不产生缓冲）。
//      缓冲句柄 BufferPool::Buffer 为 move-only，析构必定归还，
//      池内部记录每个缓冲是否已归还，重复释放直接 abort（恰好一次可验证）。
//   2. 任务队列是替身（TaskQueue double）：单线程、FIFO、手动 drain，
//      入站 / 执行 / 排空时清理都记录事件，使测试能精确控制时序。
//   3. Receiver 用“代次（generation）令牌”区分新旧连接：
//      每个入站任务捕获自己入站时的代次，执行时若代次已不匹配则拒绝派发，
//      因此关闭（以及重连）后队列里的过期消息只会被清理、不会被派发。
//   4. 关闭是幂等状态机：Running -> Closing -> Closed。
//      beginShutdown() 可与在途异步回调任意交错；重复调用只记录一次事件。
//   5. 重连 = 用同一个缓冲池/队列/日志构造一个新代次的 Receiver，
//      旧代次残留任务由代次令牌拦截，新连接独立接收完整消息。

#ifndef WS_RECEIVER_WS_RECEIVER_H
#define WS_RECEIVER_WS_RECEIVER_H

#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

// ---------------------------------------------------------------------------
// 事件日志：所有关键动作按发生顺序记录，测试用它证明释放与派发顺序
// ---------------------------------------------------------------------------
class EventLog {
public:
    struct Event {
        std::size_t seq;         // 全局单调序号（从 0 开始）
        std::int64_t generation; // 代次；不属于某代次的队列事件用 -1
        std::string action;      // 动作名，见 .cpp 中的常量
        std::string detail;      // 人类可读细节（缓冲 id、消息 id、原因等）
    };

    void record(std::int64_t generation, std::string action, std::string detail);
    const std::vector<Event>& events() const noexcept { return events_; }
    // 过滤某动作的所有事件（按发生顺序）
    std::vector<const Event*> find(std::string action) const;
    // 取出所有 (action, detail) 对，便于测试断言顺序
    std::vector<std::pair<std::string, std::string>> pairs() const;
    void clear() noexcept { events_.clear(); }
    void dump(const std::string& title) const;

private:
    std::vector<Event> events_;
};

// ---------------------------------------------------------------------------
// 类型擦除的 move-only 任务（std::function 要求可拷贝，无法承载 move-only
// 的缓冲句柄，因此自建一个最小的 unique_function 替身）
// ---------------------------------------------------------------------------
class Task {
public:
    Task() = default;
    template <class F>
    explicit Task(F&& f) : impl_(std::make_unique<Model<std::decay_t<F>>>(std::forward<F>(f))) {}

    Task(Task&&) noexcept = default;
    Task& operator=(Task&&) noexcept = default;
    Task(const Task&) = delete;
    Task& operator=(const Task&) = delete;

    void operator()() { impl_->invoke(); }
    explicit operator bool() const noexcept { return static_cast<bool>(impl_); }

private:
    struct Concept {
        virtual ~Concept() = default;
        virtual void invoke() = 0;
    };
    template <class F>
    struct Model final : Concept {
        explicit Model(F f) : fn(std::move(f)) {}
        void invoke() override { fn(); }
        F fn;
    };
    std::unique_ptr<Concept> impl_;
};

// ---------------------------------------------------------------------------
// 可记录的缓冲池：固定容量，move-only 句柄，恰好一次释放，双重释放即 abort
// ---------------------------------------------------------------------------
class BufferPool {
public:
    // move-only 缓冲句柄：析构时自动归还池，保证“每个缓冲最终恰好释放一次”
    class Buffer {
    public:
        Buffer() = default;
    private:
        friend class BufferPool;
        Buffer(BufferPool* pool, int id, std::int64_t generation, std::string payload);

    public:
        Buffer(Buffer&& other) noexcept;
        Buffer& operator=(Buffer&& other) noexcept;
        Buffer(const Buffer&) = delete;
        Buffer& operator=(const Buffer&) = delete;
        ~Buffer();

        int id() const noexcept { return id_; }
        std::int64_t generation() const noexcept { return generation_; }
        const std::string& payload() const noexcept { return payload_; }
        std::string& payload() noexcept { return payload_; }
        explicit operator bool() const noexcept { return pool_ != nullptr; }

    private:
        void reset();
        BufferPool* pool_ = nullptr;
        int id_ = -1;
        std::int64_t generation_ = -1;
        std::string payload_;
    };

    BufferPool(std::size_t capacity, EventLog& log);
    ~BufferPool();

    BufferPool(const BufferPool&) = delete;
    BufferPool& operator=(const BufferPool&) = delete;

    // 池耗尽时返回空句柄（bool(...) == false），事件中记录 pool-exhausted
    Buffer acquire(std::int64_t generation, std::string payload);
    // 正常情况下由 Buffer 析构调用；releaseReason 用于事件日志
    void release(int id, std::int64_t generation, const std::string& releaseReason);

    std::size_t outstanding() const noexcept { return outstanding_; }
    std::size_t totalAcquired() const noexcept { return totalAcquired_; }
    std::size_t totalReleased() const noexcept { return totalReleased_; }
    std::size_t capacity() const noexcept { return capacity_; }
    // 每个曾经发放的缓冲当前是否仍在外（id -> 是否未归还）
    const std::deque<bool>& liveSlots() const noexcept { return slotLive_; }

private:
    std::size_t capacity_;
    std::size_t nextId_ = 0;
    std::size_t outstanding_ = 0;
    std::size_t totalAcquired_ = 0;
    std::size_t totalReleased_ = 0;
    std::deque<int> freeIds_;
    std::deque<bool> slotLive_; // 下标即缓冲 id
    EventLog& log_;
};

// ---------------------------------------------------------------------------
// 任务队列替身：单线程 FIFO，手动排空，全部动作可记录
// ---------------------------------------------------------------------------
class TaskQueue {
public:
    explicit TaskQueue(EventLog& log);
    // 析构时清理仍排队的任务：记录 queue.drop-queued，
    // 任务捕获的 Buffer 随之析构并归还池（恰好一次释放的排队残留路径）
    ~TaskQueue();

    void post(Task task, std::int64_t generation, std::string label);
    bool empty() const noexcept { return queue_.empty(); }
    std::size_t size() const noexcept { return queue_.size(); }

    // 执行队首一个任务，返回是否执行过（空队列返回 false）
    bool runOne();
    // 按 FIFO 执行全部任务；每个任务执行前重新检查队列
    std::size_t drainAll();
    // 显式清理仍排队的任务（关闭路径）：逐任务记录 queue.drop-queued，
    // 任务捕获的 Buffer 随任务析构归还池
    std::size_t clearQueued();
    // 队首任务的入站代次（空队列返回 -1）
    std::int64_t frontGeneration() const;

private:
    struct Entry {
        Task task;
        std::int64_t generation;
        std::string label;
    };
    std::deque<Entry> queue_;
    EventLog& log_;
};

// ---------------------------------------------------------------------------
// 本地类型化事件 Sink：只接收仍然有效的代次派发出来的事件
// ---------------------------------------------------------------------------
class Sink {
public:
    struct Fragment {
        std::int64_t generation;
        int bufferId;
        std::string payload;
    };
    struct Message {
        std::int64_t generation;
        int messageId;
        std::string assembled; // 按派发顺序拼接的完整消息
    };

    void onFragment(std::int64_t generation, int messageId,
                    int bufferId, const std::string& payload);
    void onMessageComplete(std::int64_t generation, int messageId);

    const std::vector<Fragment>& fragments() const noexcept { return fragments_; }
    const std::vector<Message>& messages() const noexcept { return messages_; }

private:
    std::vector<Fragment> fragments_;
    std::vector<Message> messages_;
    // 按 messageId 累积片段，完成时落盘为一条 Message
    std::unordered_map<int, std::string> assembling_;
};

// ---------------------------------------------------------------------------
// Receiver：接收生命周期状态机
//
// 处理四类本地类型化事件：
//   onFragment(...)      片段到达           —— 获取缓冲并异步入站
//   onMessageComplete()  消息完成           —— 异步入站完成事件
//   beginShutdown()      开始关闭           —— 幂等，可与回调任意交错
//   endCallback(...)     异步回调结束       —— 在途回调计数归零且关闭中则落定
// ---------------------------------------------------------------------------
class Receiver {
public:
    enum class State { Running, Closing, Closed };

    // 首个连接代次从 initialGeneration 开始
    Receiver(std::int64_t initialGeneration,
             BufferPool& pool,
             TaskQueue& queue,
             EventLog& log,
             Sink& sink);
    ~Receiver();

    Receiver(const Receiver&) = delete;
    Receiver& operator=(const Receiver&) = delete;

    // 片段到达：关闭后直接拒绝（不获取缓冲）；运行中则获取缓冲并异步入站
    void onFragment(int messageId, std::string payload);
    // 消息完成：关闭后拒绝；运行中异步入站“消息完成”事件
    void onMessageComplete(int messageId);
    // 开始关闭：幂等。无在途回调时立即落定 Closed，否则进入 Closing
    void beginShutdown(const std::string& reason);
    // 异步回调结束：在途计数减一；若关闭中且计数归零则落定
    void endCallback();
    // 重连：必须先处于 Closed。清理队列中旧代残留（全部视为 stale），
    // 代次 +1 后回到 Running，新连接独立接收完整消息
    void reconnect();

    State state() const noexcept { return state_; }
    bool isClosed() const noexcept { return state_ == State::Closed; }
    std::int64_t generation() const noexcept { return generation_; }
    std::size_t pendingCallbacks() const noexcept { return pendingCallbacks_; }

    // 测试接缝：确定性制造一个“已开始执行、尚未结束”的在途回调，
    // 用于覆盖 shutdown 与 endCallback 的 Closing 交错。生产代码不要调用。
    void enterInFlightForTest() {
        if (state_ != State::Running) return;
        ++pendingCallbacks_;
    }

private:
    // 代次校验：仅当本接收器仍存活且代次匹配时才允许派发
    bool currentGeneration() const;
    // 关闭落定：状态 -> Closed，记录 closed 事件
    void finishClose(const std::string& reason);
    // 任务执行入口：登记在途回调。返回 false 表示代次已失效（调用方应丢弃）
    bool enterCallback();
    // 丢弃一个尚未执行就失效的任务时使用的事件名（本代关闭 / 旧代残留）
    const char* discardReason(std::int64_t taskGeneration) const;

    std::int64_t generation_;
    BufferPool& pool_;
    TaskQueue& queue_;
    EventLog& log_;
    Sink& sink_;
    State state_ = State::Running;
    std::size_t pendingCallbacks_ = 0;
};

#endif // WS_RECEIVER_WS_RECEIVER_H
