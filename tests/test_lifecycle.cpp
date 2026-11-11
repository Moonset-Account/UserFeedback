// SPDX-License-Identifier: MIT
//
// test_lifecycle.cpp — 接收生命周期测试（无外部依赖，自包含断言）
//
// 覆盖情形：
//   1. ShutdownBeforeCallback  关闭先于回调（含已排队缓冲的清理路径）
//   2. CallbackBeforeShutdown  回调先于关闭（在途回调计数驱动落定）
//   3. NormalCompletion        正常完成（片段按序派发、消息拼装）
//   4. DuplicateShutdown       重复关闭（幂等）
//   5. ReconnectIndependent    重连独立接收（旧代任务 stale、新代完整消息）
//   6. PoolExhaustion          缓冲池耗尽不产生泄漏（补充情形）
//
// 每个情形结束后断言：
//   - 池 acquire 总数 == release 总数，outstanding == 0（恰好一次释放）
//   - Sink 只收到有效代次的派发（关闭后无新消息）
//   - 关键事件在日志中的相对顺序符合预期

#include "ws_receiver.h"

#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

namespace {

int g_failures = 0;
int g_checks = 0;

#define CHECK(cond)                                                            \
    do {                                                                       \
        ++g_checks;                                                            \
        if (!(cond)) {                                                         \
            ++g_failures;                                                      \
            std::cerr << "FAIL [" << __FILE__ << ':' << __LINE__               \
                      << "] " #cond "\n";                                      \
        }                                                                     \
    } while (0)

#define CHECK_EQ(a, b)                                                         \
    do {                                                                       \
        ++g_checks;                                                            \
        auto va_ = (a);                                                        \
        auto vb_ = (b);                                                        \
        if (!(va_ == vb_)) {                                                   \
            ++g_failures;                                                      \
            std::cerr << "FAIL [" << __FILE__ << ':' << __LINE__               \
                      << "] " #a " == " #b " got " << va_                      \
                      << " vs " << vb_ << '\n';                                \
        }                                                                     \
    } while (0)

// 断言事件动作序列中，actions 按顺序出现（子序列匹配，允许中间有其他事件）
bool checkOrder(const EventLog& log, const std::vector<std::string>& actions) {
    std::size_t pos = 0;
    for (const auto& e : log.events()) {
        if (pos < actions.size() && e.action == actions[pos]) ++pos;
    }
    if (pos != actions.size()) {
        ++g_failures;
        std::cerr << "FAIL: order mismatch, missing action '" << actions[pos]
                  << "'\n";
        return false;
    }
    return true;
}

// 恰好一次释放：获取/释放总数相等、无在外缓冲、每个槽位都已归还
void checkPoolBalanced(const BufferPool& pool) {
    CHECK_EQ(pool.totalAcquired(), pool.totalReleased());
    CHECK_EQ(pool.outstanding(), static_cast<std::size_t>(0));
    for (bool live : pool.liveSlots()) CHECK(!live);
}

std::size_t countAction(const EventLog& log, const std::string& action) {
    return log.find(action).size();
}

// 每个测试独立持有一套替身，保证事件日志互不污染
struct Fixture {
    EventLog log;
    BufferPool pool{8, log};
    TaskQueue queue{log};
    Sink sink;
    Receiver receiver{1, pool, queue, log, sink};
};

// --- 情形 1：关闭先于回调（含排队缓冲） ------------------------------------
void testShutdownBeforeCallback() {
    Fixture f;
    // 两个片段入站：第一个执行并在途，第二个仍在队列中
    f.receiver.onFragment(7, "AA");
    f.receiver.onFragment(7, "BB");
    CHECK_EQ(f.queue.size(), static_cast<std::size_t>(2));
    CHECK_EQ(countAction(f.log, "pool.acquire"), static_cast<std::size_t>(2));

    f.queue.runOne(); // 执行片段 1：派发并结束回调
    CHECK_EQ(f.receiver.pendingCallbacks(), static_cast<std::size_t>(0));
    CHECK_EQ(f.sink.fragments().size(), static_cast<std::size_t>(1));

    // 关闭发生在片段 2 的回调之前：排队任务被清理，其缓冲随任务析构释放
    f.receiver.beginShutdown("peer-gone");
    CHECK(f.receiver.isClosed());
    CHECK_EQ(f.queue.size(), static_cast<std::size_t>(0));
    CHECK_EQ(countAction(f.log, "queue.drop-queued"), static_cast<std::size_t>(2)); // 1 汇总 + 1 逐任务

    // 关闭后到达的新事件被入口拒绝，且不获取任何缓冲
    const auto acquiresBefore = f.pool.totalAcquired();
    f.receiver.onFragment(7, "CC");
    f.receiver.onMessageComplete(7);
    CHECK_EQ(f.pool.totalAcquired(), acquiresBefore);
    CHECK_EQ(countAction(f.log, "fragment.reject-closed"), static_cast<std::size_t>(1));
    CHECK_EQ(countAction(f.log, "complete.reject-closed"), static_cast<std::size_t>(1));

    checkPoolBalanced(f.pool);
    CHECK_EQ(f.sink.fragments().size(), static_cast<std::size_t>(1)); // 关闭后无派发
    checkOrder(f.log, {
        "pool.acquire", "pool.acquire",
        "queue.run", "fragment.dispatch", "callback.end", "pool.release",
        "shutdown.begin", "queue.drop-queued", "closed",
        "fragment.reject-closed", "complete.reject-closed",
    });
    f.log.dump("情形1 关闭先于回调（排队缓冲清理 + 关闭后拒绝）");
}

// --- 情形 2：回调先于关闭 ---------------------------------------------------
void testCallbackBeforeShutdown() {
    Fixture f;
    f.receiver.onFragment(3, "X");
    f.queue.runOne(); // 回调完整执行结束
    CHECK_EQ(f.receiver.pendingCallbacks(), static_cast<std::size_t>(0));

    f.receiver.beginShutdown("graceful"); // 无在途回调，立即落定
    CHECK(f.receiver.isClosed());
    checkPoolBalanced(f.pool);
    CHECK_EQ(f.sink.fragments().size(), static_cast<std::size_t>(1));
    checkOrder(f.log, {
        "pool.acquire", "queue.run", "fragment.dispatch",
        "callback.end", "pool.release",
        "shutdown.begin", "closed",
    });
    f.log.dump("情形2 回调先于关闭（回调已排空，立即落定）");
}

// --- 情形 2b：Closing 中间态 —— 关闭时仍有在途回调，最后一个 endCallback 落定
void testClosingAwaitsLastCallback() {
    Fixture f;
    // 通过测试接缝制造一个“已开始执行、尚未结束”的在途回调
    f.receiver.enterInFlightForTest();
    CHECK_EQ(f.receiver.pendingCallbacks(), static_cast<std::size_t>(1));

    // 关闭先到：进入 Closing 而非 Closed，排队任务仍被清理
    f.receiver.onFragment(6, "queued-but-dropped"); // 入站一个排队任务
    f.receiver.beginShutdown("drain-inflight");
    CHECK(!f.receiver.isClosed());
    CHECK_EQ(static_cast<int>(f.receiver.state()),
             static_cast<int>(Receiver::State::Closing));
    CHECK_EQ(f.queue.size(), static_cast<std::size_t>(0)); // 排队任务已清理并释放

    // 关闭后到达的事件仍被入口拒绝
    const auto acquiresBefore = f.pool.totalAcquired();
    f.receiver.onFragment(6, "rejected");
    CHECK_EQ(f.pool.totalAcquired(), acquiresBefore);

    // 在途回调的结束信号后到：计数归零 -> 落定 Closed
    f.receiver.endCallback();
    CHECK(f.receiver.isClosed());
    CHECK_EQ(f.receiver.pendingCallbacks(), static_cast<std::size_t>(0));

    // 再次关闭幂等
    f.receiver.beginShutdown("again");
    CHECK_EQ(countAction(f.log, "shutdown.duplicate"), static_cast<std::size_t>(1));
    checkPoolBalanced(f.pool);
    checkOrder(f.log, {
        "shutdown.begin", "queue.drop-queued",
        "fragment.reject-closed",
        "callback.end", "closed", "shutdown.duplicate",
    });
    f.log.dump("情形2b Closing 中间态（关闭等在途回调，最后结束信号落定）");
}

// --- 情形 3：正常完成 -------------------------------------------------------
void testNormalCompletion() {
    Fixture f;
    f.receiver.onFragment(10, "Hello, ");
    f.receiver.onFragment(10, "world!");
    f.receiver.onMessageComplete(10);
    f.queue.drainAll();

    checkPoolBalanced(f.pool);
    CHECK_EQ(f.sink.fragments().size(), static_cast<std::size_t>(2));
    CHECK_EQ(f.sink.messages().size(), static_cast<std::size_t>(1));
    CHECK_EQ(f.sink.messages().at(0).assembled, std::string("Hello, world!"));
    CHECK_EQ(f.receiver.pendingCallbacks(), static_cast<std::size_t>(0));

    // 两条独立消息互不串台
    f.receiver.onFragment(11, "one");
    f.receiver.onMessageComplete(11);
    f.receiver.onFragment(12, "two");
    f.receiver.onMessageComplete(12);
    f.queue.drainAll();
    CHECK_EQ(f.sink.messages().size(), static_cast<std::size_t>(3));
    CHECK_EQ(f.sink.messages().at(1).assembled, std::string("one"));
    CHECK_EQ(f.sink.messages().at(2).assembled, std::string("two"));
    checkPoolBalanced(f.pool);

    f.receiver.beginShutdown("normal-eof");
    CHECK(f.receiver.isClosed());
    f.log.dump("情形3 正常完成（按序派发 + 消息拼装 + 多消息隔离）");
}

// --- 情形 4：重复关闭 -------------------------------------------------------
void testDuplicateShutdown() {
    Fixture f;
    f.receiver.onFragment(5, "D");
    f.receiver.beginShutdown("first");   // 排队任务被清理，立即关闭
    CHECK(f.receiver.isClosed());
    f.receiver.beginShutdown("second");  // 幂等：只记录 duplicate
    f.receiver.beginShutdown("third");   // 再次重复同样幂等
    CHECK(f.receiver.isClosed());

    CHECK_EQ(countAction(f.log, "shutdown.begin"), static_cast<std::size_t>(1));
    CHECK_EQ(countAction(f.log, "shutdown.duplicate"), static_cast<std::size_t>(2));
    CHECK_EQ(countAction(f.log, "closed"), static_cast<std::size_t>(1));
    checkPoolBalanced(f.pool);
    f.log.dump("情形4 重复关闭（幂等，仅首次生效）");
}

// --- 情形 5：重连独立接收完整消息 -------------------------------------------
void testReconnectIndependent() {
    Fixture f;
    // 第一代：一个片段入站但不执行，随后关闭
    f.receiver.onFragment(1, "old-");
    f.receiver.beginShutdown("reset");
    CHECK(f.receiver.isClosed());
    CHECK_EQ(f.receiver.generation(), static_cast<std::int64_t>(1));

    // 重连：代次提升，回到 Running
    f.receiver.reconnect();
    CHECK_EQ(f.receiver.generation(), static_cast<std::int64_t>(2));
    CHECK(!f.receiver.isClosed());

    // 新代次独立接收一条完整消息
    f.receiver.onFragment(2, "new-");
    f.receiver.onFragment(2, "message");
    f.receiver.onMessageComplete(2);
    f.queue.drainAll();

    CHECK_EQ(f.sink.messages().size(), static_cast<std::size_t>(1));
    CHECK_EQ(f.sink.messages().at(0).generation, static_cast<std::int64_t>(2));
    CHECK_EQ(f.sink.messages().at(0).assembled, std::string("new-message"));
    // 旧代片段从未派发给 Sink
    for (const auto& frag : f.sink.fragments()) {
        CHECK_EQ(frag.generation, static_cast<std::int64_t>(2));
    }
    checkPoolBalanced(f.pool);
    checkOrder(f.log, {
        "shutdown.begin", "closed",
        "reconnect.begin", "reconnect.ready",
        "fragment.dispatch", "fragment.dispatch", "complete.dispatch",
    });

    // 再关一次，重连后的连接也能干净关闭
    f.receiver.beginShutdown("bye");
    CHECK(f.receiver.isClosed());
    checkPoolBalanced(f.pool);
    f.log.dump("情形5 重连独立接收（代次提升，旧代不派发，新代完整消息）");
}

// --- 情形 5b：旧代任务在重连后才轮到执行 —— 代次令牌拦截 --------------------
void testStaleGenerationToken() {
    Fixture f;
    // 第一代片段入站并执行一个在途回调；关闭时它正好结束
    f.receiver.onFragment(1, "a");
    f.queue.runOne();
    // 再入站一个片段但不执行
    f.receiver.onFragment(1, "b");
    f.receiver.beginShutdown("reset"); // 清掉排队任务
    f.receiver.reconnect();

    // 直接向队列替身投递一个手工构造的“旧代残片”任务，
    // 模拟真实环境里关闭与队列排空竞争时漏网的过期任务
    auto stale = f.pool.acquire(/*generation=*/1, "stale");
    CHECK(static_cast<bool>(stale));
    const int staleId = stale.id();
    f.queue.post(
        Task([&f, stale = std::move(stale)]() mutable {
            // 与 Receiver 内部任务同构的代次检查
            if (f.receiver.generation() != stale.generation() || f.receiver.isClosed()) {
                f.log.record(stale.generation(), "queue.discard-stale",
                             "buffer=" + std::to_string(stale.id()));
                return;
            }
            f.sink.onFragment(stale.generation(), 99, stale.id(), stale.payload());
        }),
        /*generation=*/1, "fragment#stale");
    f.queue.runOne();

    // 旧代任务被识别为 stale，没有任何派发
    CHECK_EQ(countAction(f.log, "queue.discard-stale"), static_cast<std::size_t>(1));
    CHECK_EQ(f.sink.fragments().size(), static_cast<std::size_t>(1)); // 只有第一代的 "a"
    for (const auto& frag : f.sink.fragments()) CHECK_EQ(frag.payload, std::string("a"));
    (void)staleId;
    checkPoolBalanced(f.pool);
    f.log.dump("情形5b 旧代残留任务被代次令牌拦截（不派发，缓冲仍释放）");
}

// --- 情形 6：池耗尽不造成泄漏，释放后的槽位可复用 ---------------------------
void testPoolExhaustion() {
    EventLog log;
    BufferPool pool(2, log); // 极小池
    TaskQueue queue(log);
    Sink sink;
    Receiver receiver(1, pool, queue, log, sink);

    receiver.onFragment(1, "p"); // 占槽 0
    receiver.onFragment(1, "q"); // 占槽 1
    receiver.onFragment(1, "r"); // 池耗尽，拒绝且无缓冲入账
    CHECK_EQ(pool.totalAcquired(), static_cast<std::size_t>(2));
    CHECK_EQ(countAction(log, "pool.exhausted"), static_cast<std::size_t>(1));

    queue.runOne(); // 释放槽 0
    CHECK_EQ(pool.outstanding(), static_cast<std::size_t>(1));
    receiver.onFragment(1, "s"); // 复用槽 0 成功
    CHECK_EQ(pool.totalAcquired(), static_cast<std::size_t>(3));
    queue.drainAll();
    checkPoolBalanced(pool);

    receiver.beginShutdown("done");
    log.dump("情形6 缓冲池耗尽（拒绝不泄漏，槽位释放后可复用）");
}

} // namespace

int main() {
    testShutdownBeforeCallback();
    testCallbackBeforeShutdown();
    testClosingAwaitsLastCallback();
    testNormalCompletion();
    testDuplicateShutdown();
    testReconnectIndependent();
    testStaleGenerationToken();
    testPoolExhaustion();

    std::cout << "\n----------------------------------------\n";
    std::cout << "checks=" << g_checks << " failures=" << g_failures << '\n';
    if (g_failures != 0) {
        std::cout << "RESULT: FAIL\n";
        return EXIT_FAILURE;
    }
    std::cout << "RESULT: PASS (all scenarios verified)\n";
    return EXIT_SUCCESS;
}
