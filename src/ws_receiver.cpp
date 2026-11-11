// SPDX-License-Identifier: MIT
//
// ws_receiver.cpp — 接收生命周期模块实现
//
// 关键不变量：
//   I1 每个成功 acquire 的缓冲最终恰好 release 一次：
//      - 正常路径：片段任务执行（派发或因陈旧被丢弃）后 Buffer 析构归还；
//      - 排队残留：队列清空/析构时 Task 析构连带 Buffer 析构归还；
//      - 关闭后到达：根本不 acquire，无缓冲可泄漏。
//   I2 关闭后不派发任何新消息：入口状态检查 + 任务执行前代次令牌双重校验。
//   I3 关闭与异步回调可任意交错：pendingCallbacks_ 计数驱动 Closing -> Closed。
//   I4 重连接收器使用新代次，旧代次残留任务全部被代次令牌拦截。

#include "ws_receiver.h"

#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <sstream>

namespace {

// 统一的动作名常量，测试断言直接引用这些字符串
constexpr const char* kPoolAcquire        = "pool.acquire";
constexpr const char* kPoolRelease        = "pool.release";
constexpr const char* kPoolExhausted      = "pool.exhausted";
constexpr const char* kQueuePost          = "queue.post";
constexpr const char* kQueueRun           = "queue.run";
constexpr const char* kQueueDiscardStale  = "queue.discard-stale";
constexpr const char* kQueueDiscardClosed = "queue.discard-closed";
constexpr const char* kQueueDropQueued    = "queue.drop-queued";
constexpr const char* kFragArrive         = "fragment.arrive";
constexpr const char* kFragRejectClosed   = "fragment.reject-closed";
constexpr const char* kFragDispatch       = "fragment.dispatch";
constexpr const char* kCompleteArrive     = "complete.arrive";
constexpr const char* kCompleteReject     = "complete.reject-closed";
constexpr const char* kCompleteDispatch   = "complete.dispatch";
constexpr const char* kCallbackEnd        = "callback.end";
constexpr const char* kShutdownBegin      = "shutdown.begin";
constexpr const char* kShutdownDuplicate  = "shutdown.duplicate";
constexpr const char* kClosed             = "closed";
constexpr const char* kReconnectBegin     = "reconnect.begin";
constexpr const char* kReconnectReady     = "reconnect.ready";
constexpr const char* kReceiverDestroyed  = "receiver.destroyed";
constexpr const char* kReceiverDestroyBusy = "receiver.destroy-while-active";

std::string bufferDetail(int id, const std::string& reason) {
    std::ostringstream os;
    os << "buffer=" << id << " reason=" << reason;
    return os.str();
}

} // namespace

// ---------------------------------------------------------------------------
// EventLog
// ---------------------------------------------------------------------------
void EventLog::record(std::int64_t generation, std::string action, std::string detail) {
    events_.push_back(Event{events_.size(), generation, std::move(action), std::move(detail)});
}

std::vector<const EventLog::Event*> EventLog::find(std::string action) const {
    std::vector<const Event*> out;
    for (const auto& e : events_) {
        if (e.action == action) out.push_back(&e);
    }
    return out;
}

std::vector<std::pair<std::string, std::string>> EventLog::pairs() const {
    std::vector<std::pair<std::string, std::string>> out;
    out.reserve(events_.size());
    for (const auto& e : events_) out.emplace_back(e.action, e.detail);
    return out;
}

void EventLog::dump(const std::string& title) const {
    std::cout << "\n=== " << title << " ===\n";
    std::cout << std::left << std::setw(4) << "seq"
              << std::setw(5) << "gen"
              << std::setw(24) << "action"
              << "detail\n";
    std::cout << std::string(70, '-') << '\n';
    for (const auto& e : events_) {
        std::cout << std::left << std::setw(4) << e.seq
                  << std::setw(5) << e.generation
                  << std::setw(24) << e.action
                  << e.detail << '\n';
    }
}

// ---------------------------------------------------------------------------
// BufferPool
// ---------------------------------------------------------------------------
BufferPool::Buffer::Buffer(BufferPool* pool, int id,
                           std::int64_t generation, std::string payload)
    : pool_(pool), id_(id), generation_(generation), payload_(std::move(payload)) {}

BufferPool::Buffer::Buffer(Buffer&& other) noexcept
    : pool_(other.pool_),
      id_(other.id_),
      generation_(other.generation_),
      payload_(std::move(other.payload_)) {
    other.pool_ = nullptr;
    other.id_ = -1;
    other.generation_ = -1;
}

BufferPool::Buffer& BufferPool::Buffer::operator=(Buffer&& other) noexcept {
    if (this != &other) {
        reset();
        pool_ = other.pool_;
        id_ = other.id_;
        generation_ = other.generation_;
        payload_ = std::move(other.payload_);
        other.pool_ = nullptr;
        other.id_ = -1;
        other.generation_ = -1;
    }
    return *this;
}

BufferPool::Buffer::~Buffer() { reset(); }

void BufferPool::Buffer::reset() {
    if (pool_) {
        // 正常路径：缓冲随片段任务生命周期结束而归还
        pool_->release(id_, generation_, "fragment-task-end");
        pool_ = nullptr;
        id_ = -1;
        generation_ = -1;
        payload_.clear();
    }
}

BufferPool::BufferPool(std::size_t capacity, EventLog& log)
    : capacity_(capacity), slotLive_(capacity, false), log_(log) {}

BufferPool::~BufferPool() {
    // 正确使用时所有句柄都应已析构；这里只做诊断，不静默“兜底释放”，
    // 否则会掩盖生命周期缺陷。
    for (std::size_t id = 0; id < slotLive_.size(); ++id) {
        if (slotLive_[id]) {
            std::cerr << "BufferPool destructor: buffer " << id
                      << " still outstanding — lifecycle bug\n";
        }
    }
}

BufferPool::Buffer BufferPool::acquire(std::int64_t generation, std::string payload) {
    int id;
    if (!freeIds_.empty()) {
        id = freeIds_.front();
        freeIds_.pop_front();
    } else if (nextId_ < capacity_) {
        id = static_cast<int>(nextId_++);
    } else {
        log_.record(generation, kPoolExhausted, "capacity=" + std::to_string(capacity_));
        return Buffer{};
    }

    slotLive_[static_cast<std::size_t>(id)] = true;
    ++outstanding_;
    ++totalAcquired_;
    log_.record(generation, kPoolAcquire, "buffer=" + std::to_string(id));
    return Buffer(this, id, generation, std::move(payload));
}

void BufferPool::release(int id, std::int64_t generation, const std::string& releaseReason) {
    const auto slot = static_cast<std::size_t>(id);
    if (id < 0 || slot >= slotLive_.size() || !slotLive_[slot]) {
        // 双重释放是致命错误：直接终止，防止“恰好一次”被破坏后静默继续
        std::cerr << "FATAL: double release of buffer id=" << id << '\n';
        std::abort();
    }
    slotLive_[slot] = false;
    freeIds_.push_back(id);
    --outstanding_;
    ++totalReleased_;
    log_.record(generation, kPoolRelease, bufferDetail(id, releaseReason));
}

// ---------------------------------------------------------------------------
// TaskQueue
// ---------------------------------------------------------------------------
TaskQueue::TaskQueue(EventLog& log) : log_(log) {}

TaskQueue::~TaskQueue() {
    if (!queue_.empty()) clearQueued();
}

void TaskQueue::post(Task task, std::int64_t generation, std::string label) {
    log_.record(generation, kQueuePost, "label=" + label + " depth=" + std::to_string(queue_.size()));
    queue_.push_back(Entry{std::move(task), generation, std::move(label)});
}

bool TaskQueue::runOne() {
    if (queue_.empty()) return false;
    Entry entry = std::move(queue_.front());
    queue_.pop_front();
    log_.record(entry.generation, kQueueRun,
                "label=" + entry.label + " depth=" + std::to_string(queue_.size()));
    entry.task(); // 任务内部自行做代次校验；Buffer 在 task 析构时归还
    return true;
}

std::size_t TaskQueue::drainAll() {
    std::size_t executed = 0;
    while (!queue_.empty()) {
        runOne();
        ++executed;
    }
    return executed;
}

std::size_t TaskQueue::clearQueued() {
    std::size_t dropped = 0;
    while (!queue_.empty()) {
        Entry entry = std::move(queue_.front());
        queue_.pop_front();
        // 先记录清理事件；entry 随后析构，其任务捕获的 Buffer 析构并归还池
        log_.record(entry.generation, kQueueDropQueued, "label=" + entry.label);
        dropped++;
    }
    return dropped;
}

std::int64_t TaskQueue::frontGeneration() const {
    return queue_.empty() ? -1 : queue_.front().generation;
}

// ---------------------------------------------------------------------------
// Sink
// ---------------------------------------------------------------------------
void Sink::onFragment(std::int64_t generation, int messageId,
                      int bufferId, const std::string& payload) {
    fragments_.push_back(Fragment{generation, bufferId, payload});
    assembling_[messageId].append(payload);
}

void Sink::onMessageComplete(std::int64_t generation, int messageId) {
    auto it = assembling_.find(messageId);
    std::string assembled = (it == assembling_.end()) ? std::string{} : std::move(it->second);
    assembling_.erase(it);
    messages_.push_back(Message{generation, messageId, std::move(assembled)});
}

// ---------------------------------------------------------------------------
// Receiver
// ---------------------------------------------------------------------------
Receiver::Receiver(std::int64_t initialGeneration,
                   BufferPool& pool,
                   TaskQueue& queue,
                   EventLog& log,
                   Sink& sink)
    : generation_(initialGeneration), pool_(pool), queue_(queue), log_(log), sink_(sink) {}

Receiver::~Receiver() {
    if (state_ != State::Closed || pendingCallbacks_ != 0) {
        // 诊断事件：测试保证每个连接都显式关闭且无在途回调后才析构
        log_.record(generation_, kReceiverDestroyBusy,
                    "state=" + std::to_string(static_cast<int>(state_)) +
                    " pending=" + std::to_string(pendingCallbacks_));
    }
    log_.record(generation_, kReceiverDestroyed, "");
}

bool Receiver::currentGeneration() const {
    // 代次令牌校验：关闭后的本代任务、重连后残留的旧代任务都会在此被拦截
    return state_ != State::Closed;
}

const char* Receiver::discardReason(std::int64_t taskGeneration) const {
    // 任务自带代次与当前代次不同 -> 重连后的旧代残留；
    // 代次相同但已关闭 -> 本代关闭后轮到执行的排队任务。
    return taskGeneration == generation_ ? kQueueDiscardClosed : kQueueDiscardStale;
}

bool Receiver::enterCallback() {
    // 计数只统计“已经开始执行”的回调：排队任务被 clearQueued 清理时
    // 不会进入这里，因此不会污染 pendingCallbacks_，关闭计数仍可归零。
    if (!currentGeneration()) return false;
    ++pendingCallbacks_;
    return true;
}

void Receiver::finishClose(const std::string& reason) {
    state_ = State::Closed;
    log_.record(generation_, kClosed, "reason=" + reason);
}

void Receiver::onFragment(int messageId, std::string payload) {
    log_.record(generation_, kFragArrive,
                "message=" + std::to_string(messageId) + " bytes=" + std::to_string(payload.size()));

    if (state_ != State::Running) {
        // 关闭后到达：拒绝在获取缓冲之前，杜绝“排队缓冲残留”
        log_.record(generation_, kFragRejectClosed, "message=" + std::to_string(messageId));
        return;
    }

    BufferPool::Buffer buffer = pool_.acquire(generation_, std::move(payload));
    if (!buffer) {
        // 池耗尽：acquire 已记录 pool.exhausted，调用方应退避重试
        return;
    }
    const int bufferId = buffer.id();
    const std::int64_t gen = generation_; // 捕获入站时刻的代次令牌

    queue_.post(
        Task([this, gen, messageId, bufferId, buf = std::move(buffer)]() mutable {
            // 异步派发时刻做唯一入口校验：代次令牌不匹配（重连）或已关闭，
            // 都拒绝派发；未登记在途故不调用 endCallback，
            // 缓冲仍随 buf 析构恰好释放一次。
            if (gen != generation_ || !enterCallback()) {
                log_.record(gen, discardReason(gen),
                            "buffer=" + std::to_string(bufferId) +
                            " message=" + std::to_string(messageId));
                return;
            }
            log_.record(gen, kFragDispatch,
                        "buffer=" + std::to_string(bufferId) +
                        " message=" + std::to_string(messageId) +
                        " payload=" + buf.payload());
            sink_.onFragment(gen, messageId, bufferId, buf.payload());
            endCallback();
            // buf 在 lambda 析构时归还池（pool.release, reason=fragment-task-end）
        }),
        gen,
        "fragment#" + std::to_string(bufferId));
}

void Receiver::onMessageComplete(int messageId) {
    log_.record(generation_, kCompleteArrive, "message=" + std::to_string(messageId));

    if (state_ != State::Running) {
        log_.record(generation_, kCompleteReject, "message=" + std::to_string(messageId));
        return;
    }
    const std::int64_t gen = generation_;

    queue_.post(
        Task([this, gen, messageId]() {
            if (gen != generation_ || !enterCallback()) {
                log_.record(gen, discardReason(gen),
                            "complete message=" + std::to_string(messageId));
                return;
            }
            log_.record(gen, kCompleteDispatch, "message=" + std::to_string(messageId));
            sink_.onMessageComplete(gen, messageId);
            endCallback();
        }),
        gen,
        "complete#" + std::to_string(messageId));
}

void Receiver::endCallback() {
    if (pendingCallbacks_ == 0) {
        std::cerr << "FATAL: endCallback underflow on generation " << generation_ << '\n';
        std::abort();
    }
    --pendingCallbacks_;
    log_.record(generation_, kCallbackEnd, "pending=" + std::to_string(pendingCallbacks_));

    // 回调先结束、关闭后到：此处 state 仍为 Running，什么都不做；
    // 关闭先到、回调后结束：Closing + 计数归零 -> 落定 Closed。
    if (state_ == State::Closing && pendingCallbacks_ == 0) {
        finishClose("callbacks-drained-after-shutdown");
    }
}

void Receiver::beginShutdown(const std::string& reason) {
    if (state_ != State::Running) {
        // Closing / Closed 下重复调用：幂等，仅记录
        log_.record(generation_, kShutdownDuplicate, "reason=" + reason);
        return;
    }
    log_.record(generation_, kShutdownBegin, "reason=" + reason +
                " pending=" + std::to_string(pendingCallbacks_));

    // 清理队列中尚未开始执行的任务：其缓冲随任务析构归还池，
    // 事件日志给出每个排队任务的 drop-queued 记录。
    const std::size_t dropped = queue_.clearQueued();
    log_.record(generation_, kQueueDropQueued, "count=" + std::to_string(dropped));

    if (pendingCallbacks_ == 0) {
        finishClose(reason); // 无在途回调：立即落定
    } else {
        state_ = State::Closing; // 等待最后一个 endCallback 落定
    }
}

void Receiver::reconnect() {
    if (state_ != State::Closed) {
        std::cerr << "FATAL: reconnect() requires Closed state, current="
                  << static_cast<int>(state_) << '\n';
        std::abort();
    }
    log_.record(generation_, kReconnectBegin,
                "old-generation=" + std::to_string(generation_));

    // 旧代次残留任务（例如关闭路径没经过队列清理的边界）全部清掉，
    // 其缓冲随任务析构归还池；日志里它们的代次是旧代，可与新代区分。
    const std::size_t dropped = queue_.clearQueued();
    log_.record(generation_, kQueueDropQueued,
                "count=" + std::to_string(dropped) + " scope=reconnect");

    ++generation_;
    state_ = State::Running;
    // pendingCallbacks_ 在 Closed 时必为 0（关闭落定的前置条件）
    log_.record(generation_, kReconnectReady,
                "new-generation=" + std::to_string(generation_));
}
