package neighborqueue

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

const (
	ifA  IfIndex = 1
	ifB  IfIndex = 2
	t1   Target  = "10.0.0.1"
	t2   Target  = "10.0.0.2"
	macA         = LinkAddr("aa:aa:aa:aa:aa:01")
	macB         = LinkAddr("bb:bb:bb:bb:bb:02")
)

func testQueue(t *testing.T, start time.Time) *Queue {
	t.Helper()
	return New(start, Config{
		ReachableTTL:   10 * time.Second,
		ConfirmTimeout: 5 * time.Second,
		ResolveTimeout: 5 * time.Second,
		QueueCap:       3,
	})
}

func pkt(id PacketID) Packet { return Packet{ID: id, Payload: []byte(fmt.Sprintf("p%d", id))} }

// 1. 从未解析过的地址：从第一包开始排队，解析完成后按 FIFO 释放。
func TestIncompleteQueuesFromFirstPacketAndFlushes(t *testing.T) {
	q := testQueue(t, time.Unix(0, 0))
	q.SetRoute(t1, ifA)

	r1 := q.Send(t1, pkt(1))
	if !r1.Enqueued || r1.Transmitted != nil || r1.Failed != nil {
		t.Fatalf("first packet must be enqueued, got %+v", r1)
	}
	if got := q.State(ifA, t1); got != Incomplete {
		t.Fatalf("state = %s, want Incomplete", got)
	}
	if got := q.Queued(ifA, t1); len(got) != 1 || got[0] != 1 {
		t.Fatalf("queued = %v, want [1]", got)
	}

	if r := q.Send(t1, pkt(2)); !r.Enqueued {
		t.Fatalf("second packet must be enqueued, got %+v", r)
	}

	res := q.Resolve(ifA, t1, macA, Receipt{}, nil)
	if res.Err != nil || res.State != Reachable {
		t.Fatalf("resolve = %+v", res)
	}
	if len(res.Flushed) != 2 {
		t.Fatalf("flushed %d packets, want 2", len(res.Flushed))
	}
	// FIFO order and all packets use the freshly resolved link address.
	for i, rec := range res.Flushed {
		if rec.Packet.ID != PacketID(i+1) {
			t.Fatalf("flush order wrong at %d: %d", i, rec.Packet.ID)
		}
		if rec.Link != macA || rec.IfIdx != ifA || !rec.Receipt.Valid() {
			t.Fatalf("flushed record wrong: %+v", rec)
		}
	}
	if got := q.Queued(ifA, t1); len(got) != 0 {
		t.Fatalf("queue not drained: %v", got)
	}
	if ll, ok := q.LinkAddr(ifA, t1); !ok || ll != macA {
		t.Fatalf("link addr = %q,%v", ll, ok)
	}

	// Reachable 后续包直接发送。
	r3 := q.Send(t1, pkt(3))
	if r3.Transmitted == nil || r3.Transmitted.Link != macA {
		t.Fatalf("reachable send = %+v", r3)
	}
}

// 2. 空链路地址的成功解析必须被拒绝。
func TestIncompleteRejectsEmptyLink(t *testing.T) {
	q := testQueue(t, time.Unix(0, 0))
	q.SetRoute(t1, ifA)
	q.Send(t1, pkt(1))

	res := q.Resolve(ifA, t1, "", Receipt{}, nil)
	if !errors.Is(res.Err, ErrBadLinkAddr) {
		t.Fatalf("err = %v, want ErrBadLinkAddr", res.Err)
	}
	if q.State(ifA, t1) != Incomplete {
		t.Fatalf("state must remain Incomplete, got %s", q.State(ifA, t1))
	}
	if got := q.Queued(ifA, t1); len(got) != 1 {
		t.Fatalf("queue must be retained, got %v", got)
	}
}

// 3. 解析超时：tick 触发失败并返回受影响的包。
func TestIncompleteTimeoutReturnsPackets(t *testing.T) {
	start := time.Unix(0, 0)
	q := testQueue(t, start)
	q.SetRoute(t1, ifA)
	q.Send(t1, pkt(1))
	q.Send(t1, pkt(2))

	tr := q.Tick(start.Add(5 * time.Second))
	if len(tr.Expired) != 1 || len(tr.Failed) != 2 {
		t.Fatalf("tick = %+v", tr)
	}
	for i, fp := range tr.Failed {
		if fp.Reason != FailResolveTimeout || fp.Packet.ID != PacketID(i+1) || fp.IfIdx != ifA {
			t.Fatalf("failed packet wrong: %+v", fp)
		}
	}
	if q.State(ifA, t1) != Failed {
		t.Fatalf("state = %s, want Failed", q.State(ifA, t1))
	}
	// 临界前一拍不应超时。
	q2 := testQueue(t, start)
	q2.SetRoute(t2, ifA)
	q2.Send(t2, pkt(9))
	early := q2.Tick(start.Add(5*time.Second - time.Nanosecond))
	if len(early.Failed) != 0 || q2.State(ifA, t2) != Incomplete {
		t.Fatalf("boundary: early tick = %+v state=%s", early, q2.State(ifA, t2))
	}
}

// 4. 显式解析失败：返回队列中的包，条目进入 Failed。
func TestIncompleteExplicitFailure(t *testing.T) {
	q := testQueue(t, time.Unix(0, 0))
	q.SetRoute(t1, ifA)
	q.Send(t1, pkt(1))
	q.Send(t1, pkt(2))

	resolveErr := errors.New("neighbor probe unreachable")
	res := q.Resolve(ifA, t1, "", Receipt{}, resolveErr)
	if len(res.Failed) != 2 {
		t.Fatalf("failed = %d, want 2", len(res.Failed))
	}
	for _, fp := range res.Failed {
		if fp.Reason != FailResolveRejected || !errors.Is(fp.Err, resolveErr) {
			t.Fatalf("wrong failed packet: %+v", fp)
		}
	}
	if q.State(ifA, t1) != Failed || len(q.Queued(ifA, t1)) != 0 {
		t.Fatalf("post-failure state wrong: %s queue=%v", q.State(ifA, t1), q.Queued(ifA, t1))
	}

	// Failed 后再来包：重新进入新一轮解析并重新排队。
	r := q.Send(t1, pkt(3))
	if !r.Enqueued || q.State(ifA, t1) != Incomplete {
		t.Fatalf("retry = %+v state=%s", r, q.State(ifA, t1))
	}
}

// 5. 记录过期（Reachable -> Stale）：旧地址先发一包并进入 Confirm，
// 后续包进入有限队列；匹配回执确认后 FIFO 释放。
func TestStaleOneProbeThenConfirmAndFlush(t *testing.T) {
	start := time.Unix(0, 0)
	q := testQueue(t, start)
	q.SetRoute(t1, ifA)
	q.Send(t1, pkt(1))
	q.Resolve(ifA, t1, macA, Receipt{}, nil)

	// 提前一拍仍然 Reachable。
	q.Tick(start.Add(10*time.Second - time.Nanosecond))
	if q.State(ifA, t1) != Reachable {
		t.Fatalf("boundary state = %s, want Reachable", q.State(ifA, t1))
	}

	// TTL 到期转为 Stale。
	tr := q.Tick(start.Add(10 * time.Second))
	if len(tr.Expired) != 1 || q.State(ifA, t1) != Stale {
		t.Fatalf("expiry tick = %+v state=%s", tr, q.State(ifA, t1))
	}

	// Stale 的第一包：用旧地址发一包，进入 Confirm。
	r2 := q.Send(t1, pkt(2))
	if r2.Transmitted == nil || r2.Transmitted.Link != macA {
		t.Fatalf("stale probe = %+v", r2)
	}
	probe := r2.Transmitted.Receipt
	if q.State(ifA, t1) != Confirm {
		t.Fatalf("state = %s, want Confirm", q.State(ifA, t1))
	}
	if pend := q.PendingReceipt(ifA, t1); pend != probe {
		t.Fatalf("pending = %+v, want %+v", pend, probe)
	}
	// 探测包在线路上，不在队列里。
	if got := q.Queued(ifA, t1); len(got) != 0 {
		t.Fatalf("probe must not be queued, got %v", got)
	}

	// Confirm 期间后续包进入有限队列。
	for _, id := range []PacketID{3, 4} {
		if r := q.Send(t1, pkt(id)); !r.Enqueued {
			t.Fatalf("packet %d must queue during confirm: %+v", id, r)
		}
	}
	if got := q.Queued(ifA, t1); len(got) != 2 || got[0] != 3 || got[1] != 4 {
		t.Fatalf("queue = %v, want [3 4]", got)
	}

	// 错误的回执不能释放队列。
	bad := Receipt{ID: probe.ID + 999, IfIdx: ifA, Target: t1}
	if res := q.Resolve(ifA, t1, "", bad, nil); !errors.Is(res.Err, ErrReceiptMismatch) {
		t.Fatalf("foreign receipt err = %v", res.Err)
	}
	if q.State(ifA, t1) != Confirm || len(q.Queued(ifA, t1)) != 2 {
		t.Fatalf("state/queue must be unchanged after mismatch: %s %v", q.State(ifA, t1), q.Queued(ifA, t1))
	}

	// 匹配回执确认，队列释放；新地址可选更新。
	res := q.Resolve(ifA, t1, "aa:aa:aa:aa:aa:99", probe, nil)
	if res.Err != nil || res.State != Reachable || len(res.Flushed) != 2 {
		t.Fatalf("confirm = %+v", res)
	}
	if ll, _ := q.LinkAddr(ifA, t1); ll != "aa:aa:aa:aa:aa:99" {
		t.Fatalf("link not updated: %q", ll)
	}
	if q.PendingReceipt(ifA, t1).Valid() {
		t.Fatal("pending receipt must be cleared")
	}
}

// 6. Confirm 状态下显式失败且回执不匹配时，必须忽略该事件。
func TestConfirmFailureRequiresMatchingReceipt(t *testing.T) {
	q, probe := staleConfirmSetup(t)

	bad := Receipt{ID: probe.ID + 1, IfIdx: ifA, Target: t1}
	res := q.Resolve(ifA, t1, "", bad, errors.New("nack"))
	if !errors.Is(res.Err, ErrReceiptMismatch) {
		t.Fatalf("err = %v, want mismatch", res.Err)
	}
	if q.State(ifA, t1) != Confirm {
		t.Fatalf("state = %s, want Confirm", q.State(ifA, t1))
	}

	// 匹配回执的失败：返回受影响的包。
	res = q.Resolve(ifA, t1, "", probe, errors.New("nack"))
	if len(res.Failed) != 1 || res.Failed[0].Reason != FailConfirmRejected {
		t.Fatalf("confirm failure = %+v", res)
	}
	if q.State(ifA, t1) != Failed {
		t.Fatalf("state = %s, want Failed", q.State(ifA, t1))
	}
}

// 7. Confirm 超时：tick 返回排队包，探测包已在线路上故不重复返回。
func TestConfirmTimeout(t *testing.T) {
	start := time.Unix(100, 0)
	// setup 内部时钟已推进到 start+10s，Confirm 截止时间为 start+15s；
	// setup 已在 Confirm 队列中放入 pkt(3)。
	q, _ := staleConfirmSetupAt(t, start)

	// 临界前一拍不超时。
	early := q.Tick(start.Add(15*time.Second - time.Nanosecond))
	if len(early.Failed) != 0 || q.State(ifA, t1) != Confirm {
		t.Fatalf("boundary: early tick = %+v state=%s", early, q.State(ifA, t1))
	}

	tr := q.Tick(start.Add(15 * time.Second))
	if len(tr.Failed) != 1 || tr.Failed[0].Packet.ID != 3 ||
		tr.Failed[0].Reason != FailConfirmTimeout {
		t.Fatalf("tick = %+v", tr)
	}
	if q.State(ifA, t1) != Failed {
		t.Fatalf("state = %s, want Failed", q.State(ifA, t1))
	}
}

// 8. 队列容量边界：溢出包立即返回，且不影响已排队内容。
func TestQueueOverflow(t *testing.T) {
	q := testQueue(t, time.Unix(0, 0))
	q.SetRoute(t1, ifA)
	for id := PacketID(1); id <= 3; id++ {
		if r := q.Send(t1, pkt(id)); !r.Enqueued {
			t.Fatalf("packet %d should fit: %+v", id, r)
		}
	}
	overflow := q.Send(t1, pkt(4))
	if overflow.Failed == nil || overflow.Failed.Reason != FailOverflow || overflow.Failed.Packet.ID != 4 {
		t.Fatalf("overflow = %+v", overflow)
	}
	if got := q.Queued(ifA, t1); len(got) != 3 {
		t.Fatalf("queue = %v, want 3 retained", got)
	}
	// 解析成功后只释放已入队的 3 包，溢出包由调用方自行处理。
	res := q.Resolve(ifA, t1, macA, Receipt{}, nil)
	if len(res.Flushed) != 3 {
		t.Fatalf("flushed = %d, want 3", len(res.Flushed))
	}
	stats, total := q.Stats()
	if stats[ifA].Enqueued != 3 || stats[ifA].Failed != 1 || total.Failed != 1 {
		t.Fatalf("stats = %+v total=%+v", stats[ifA], total)
	}
}

// 9. 路由切换：新接口拥有独立邻居状态；旧接口的在途/排队包按原结果独立记账。
func TestInterfaceChangeIndependentState(t *testing.T) {
	start := time.Unix(0, 0)
	q := testQueue(t, start)
	q.SetRoute(t1, ifA)
	q.Send(t1, pkt(1))
	q.Resolve(ifA, t1, macA, Receipt{}, nil)

	// 切换前在 ifA 上再制造一个 Confirm + 排队包。
	q.Tick(start.Add(10 * time.Second)) // Reachable -> Stale
	r := q.Send(t1, pkt(2))             // ifA 探测包
	probeA := r.Transmitted.Receipt
	q.Send(t1, pkt(3)) // ifA 队列
	if q.State(ifA, t1) != Confirm {
		t.Fatalf("ifA state = %s", q.State(ifA, t1))
	}

	// 路由切到 ifB。
	ic := q.InterfaceChange(t1, ifB)
	if ic.Old != ifA || ic.New != ifB || !ic.Created {
		t.Fatalf("interface change = %+v", ic)
	}

	// 新接口上从未解析：从第一包开始排队，与 ifA 完全隔离。
	nr := q.Send(t1, pkt(4))
	if nr.IfIdx != ifB || !nr.Enqueued {
		t.Fatalf("new-if send = %+v", nr)
	}
	if q.State(ifB, t1) != Incomplete || q.State(ifA, t1) != Confirm {
		t.Fatalf("states: ifA=%s ifB=%s", q.State(ifA, t1), q.State(ifB, t1))
	}
	if got := q.Queued(ifA, t1); len(got) != 1 || got[0] != 3 {
		t.Fatalf("ifA queue disturbed: %v", got)
	}
	if got := q.Queued(ifB, t1); len(got) != 1 || got[0] != 4 {
		t.Fatalf("ifB queue = %v", got)
	}

	// ifB 解析到不同的链路地址。
	resB := q.Resolve(ifB, t1, macB, Receipt{}, nil)
	if len(resB.Flushed) != 1 || resB.Flushed[0].Link != macB ||
		resB.Flushed[0].Packet.ID != 4 || resB.Flushed[0].IfIdx != ifB {
		t.Fatalf("ifB flush = %+v", resB)
	}

	// ifA 用自己原来的回执完成确认，只释放 ifA 的队列。
	resA := q.Resolve(ifA, t1, macA, probeA, nil)
	if len(resA.Flushed) != 1 || resA.Flushed[0].Packet.ID != 3 ||
		resA.Flushed[0].IfIdx != ifA {
		t.Fatalf("ifA flush = %+v", resA)
	}

	// 两个接口各记各的账。ifA 发送：pkt1(flush)、pkt2(探测)、pkt3(确认后 flush)。
	stats, total := q.Stats()
	if stats[ifA].Transmitted != 3 || stats[ifB].Transmitted != 1 {
		t.Fatalf("transmitted ifA=%d ifB=%d", stats[ifA].Transmitted, stats[ifB].Transmitted)
	}
	if total.Transmitted != 4 || total.Enqueued != 3 || total.Failed != 0 {
		t.Fatalf("total = %+v", total)
	}
}

// 10. 路由切换后，旧接口 Confirm 超时只失败旧接口的包。
func TestInterfaceChangeOldIfTimeoutIndependent(t *testing.T) {
	start := time.Unix(0, 0)
	q := testQueue(t, start)
	q.SetRoute(t1, ifA)
	q.Send(t1, pkt(1))
	q.Resolve(ifA, t1, macA, Receipt{}, nil)
	q.Tick(start.Add(10 * time.Second))
	q.Send(t1, pkt(2)) // ifA 探测
	q.Send(t1, pkt(3)) // ifA 排队

	q.InterfaceChange(t1, ifB)
	q.Send(t1, pkt(4)) // ifB 排队，Incomplete

	tr := q.Tick(start.Add(10*time.Second + 5*time.Second))
	// 同时有 ifA Confirm 超时和 ifB Incomplete 超时。
	if len(tr.Failed) != 2 {
		t.Fatalf("tick failed = %+v", tr.Failed)
	}
	seen := map[IfIndex]FailReason{}
	for _, fp := range tr.Failed {
		seen[fp.IfIdx] = fp.Reason
	}
	if seen[ifA] != FailConfirmTimeout || seen[ifB] != FailResolveTimeout {
		t.Fatalf("reasons = %v", seen)
	}
}

// 11. 回执的接口归属校验：ifB 的回执不能确认 ifA 的条目。
func TestReceiptInterfaceScoping(t *testing.T) {
	q, probeA := staleConfirmSetup(t)

	foreign := Receipt{ID: probeA.ID, IfIdx: ifB, Target: t1}
	res := q.Resolve(ifA, t1, macA, foreign, nil)
	if !errors.Is(res.Err, ErrReceiptMismatch) {
		t.Fatalf("foreign-interface receipt err = %v", res.Err)
	}
	// 同 ID 但 target 不匹配同样拒绝。
	wrongTarget := Receipt{ID: probeA.ID, IfIdx: ifA, Target: t2}
	res = q.Resolve(ifA, t1, macA, wrongTarget, nil)
	if !errors.Is(res.Err, ErrReceiptMismatch) {
		t.Fatalf("wrong-target receipt err = %v", res.Err)
	}
	if q.State(ifA, t1) != Confirm {
		t.Fatalf("state = %s, want Confirm", q.State(ifA, t1))
	}
}

// 12. 不存在的条目上解析：返回 UnknownEntry，不凭空建条目。
func TestResolveUnknownEntry(t *testing.T) {
	q := testQueue(t, time.Unix(0, 0))
	res := q.Resolve(ifA, t1, macA, Receipt{}, nil)
	if !errors.Is(res.Err, ErrUnknownEntry) {
		t.Fatalf("err = %v, want ErrUnknownEntry", res.Err)
	}
	if q.State(ifA, t1) != NoEntry {
		t.Fatalf("state = %s, want NoEntry", q.State(ifA, t1))
	}
}

// 13. 无路由发送：立即失败并计入 NoRoute，不创建条目。
func TestSendNoRoute(t *testing.T) {
	q := testQueue(t, time.Unix(0, 0))
	r := q.Send(t1, pkt(1))
	if r.Failed == nil || r.Failed.Reason != FailNoRoute || !errors.Is(r.Failed.Err, ErrNoRoute) {
		t.Fatalf("no-route send = %+v", r)
	}
	if q.State(ifA, t1) != NoEntry {
		t.Fatalf("entry must not be created, state=%s", q.State(ifA, t1))
	}
	stats, total := q.Stats()
	if total.NoRoute != 1 || total.Failed != 0 {
		t.Fatalf("stats=%+v total=%+v", stats, total)
	}
}

// 14. Reachable 状态下收到主动通告（gratuitous）：刷新地址与 TTL，不产生 flush。
func TestGratuitousUpdateOnReachable(t *testing.T) {
	start := time.Unix(0, 0)
	q := testQueue(t, start)
	q.SetRoute(t1, ifA)
	q.Send(t1, pkt(1))
	q.Resolve(ifA, t1, macA, Receipt{}, nil)

	newMac := LinkAddr("aa:aa:aa:aa:aa:77")
	res := q.Resolve(ifA, t1, newMac, Receipt{}, nil)
	if !errors.Is(res.Err, ErrReceiptMismatch) || len(res.Flushed) != 0 {
		t.Fatalf("gratuitous resolve = %+v", res)
	}
	if ll, _ := q.LinkAddr(ifA, t1); ll != newMac {
		t.Fatalf("link = %q, want %q", ll, newMac)
	}
	// TTL 被刷新：原到期时间之后仍 Reachable。
	q.Tick(start.Add(10*time.Second - time.Nanosecond))
	if q.State(ifA, t1) != Reachable {
		t.Fatalf("state = %s, want Reachable after refresh", q.State(ifA, t1))
	}
}

// 15. 切到相同接口是幂等的，不重复建条目、不丢状态。
func TestInterfaceChangeSameInterface(t *testing.T) {
	q := testQueue(t, time.Unix(0, 0))
	q.SetRoute(t1, ifA)
	q.Send(t1, pkt(1))
	ic := q.InterfaceChange(t1, ifA)
	if ic.Created {
		t.Fatalf("same-interface change must not create an entry: %+v", ic)
	}
	if q.State(ifA, t1) != Incomplete {
		t.Fatalf("state = %s", q.State(ifA, t1))
	}
}

// 16. HandleEvent 统一事件入口分发四类事件。
func TestHandleEventDispatch(t *testing.T) {
	start := time.Unix(0, 0)
	q := testQueue(t, start)

	er := q.HandleEvent(InterfaceChangeEvent{Target: t1, NewIf: ifA})
	if er.InterfaceChange == nil || er.Err != nil {
		t.Fatalf("interface event = %+v", er)
	}
	er = q.HandleEvent(SendEvent{Target: t1, Packet: pkt(1)})
	if er.Send == nil || !er.Send.Enqueued {
		t.Fatalf("send event = %+v", er)
	}
	er = q.HandleEvent(ResolveEvent{IfIdx: ifA, Target: t1, Link: macA})
	if er.Resolve == nil || len(er.Resolve.Flushed) != 1 {
		t.Fatalf("resolve event = %+v", er)
	}
	er = q.HandleEvent(TickEvent{Now: start.Add(10 * time.Second)})
	if er.Tick == nil || len(er.Tick.Expired) != 1 {
		t.Fatalf("tick event = %+v", er)
	}
	er = q.HandleEvent(nil)
	if er.Err == nil {
		t.Fatal("nil event must report an error")
	}
}

// 17. 交错场景：解析成功后立即过期、确认、再排队、再超时的完整循环。
func TestInterleavedLifecycle(t *testing.T) {
	start := time.Unix(0, 0)
	q := testQueue(t, start)
	q.SetRoute(t1, ifA)

	// Incomplete -> Reachable
	q.Send(t1, pkt(1))
	q.Resolve(ifA, t1, macA, Receipt{}, nil)
	// Reachable -> Stale -> Confirm -> Reachable
	q.Tick(start.Add(10 * time.Second))
	r := q.Send(t1, pkt(2))
	probe := r.Transmitted.Receipt
	q.Send(t1, pkt(3))
	res := q.Resolve(ifA, t1, macA, probe, nil)
	if len(res.Flushed) != 1 || res.Flushed[0].Packet.ID != 3 {
		t.Fatalf("first confirm flush = %+v", res)
	}
	// 再来一轮 Stale -> Confirm -> 超时 -> Failed。
	q.Tick(start.Add(20 * time.Second))
	if q.State(ifA, t1) != Stale {
		t.Fatalf("state = %s, want Stale", q.State(ifA, t1))
	}
	q.Send(t1, pkt(4)) // 探测
	q.Send(t1, pkt(5)) // 排队
	tr := q.Tick(start.Add(20*time.Second + 5*time.Second))
	if len(tr.Failed) != 1 || tr.Failed[0].Packet.ID != 5 {
		t.Fatalf("second timeout = %+v", tr)
	}
	if q.State(ifA, t1) != Failed {
		t.Fatalf("state = %s, want Failed", q.State(ifA, t1))
	}
	// Failed 后重新解析一轮。
	q.Send(t1, pkt(6))
	if q.State(ifA, t1) != Incomplete {
		t.Fatalf("state = %s, want Incomplete", q.State(ifA, t1))
	}
	q.Resolve(ifA, t1, macB, Receipt{}, nil)
	if ll, _ := q.LinkAddr(ifA, t1); ll != macB {
		t.Fatalf("link = %q, want %q", ll, macB)
	}
	if r := q.Send(t1, pkt(7)); r.Transmitted == nil || r.Transmitted.Link != macB {
		t.Fatalf("final send = %+v", r)
	}
}

// 18. 并发烟雾测试：race detector 下并发发送、解析、tick 与查询应保持一致计数。
func TestConcurrentAccess(t *testing.T) {
	start := time.Unix(0, 0)
	q := New(start, DefaultConfig())
	q.SetRoute(t1, ifA)

	var wg sync.WaitGroup
	for g := 0; g < 4; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for k := 0; k < 50; k++ {
				id := PacketID(g*100 + k)
				sr := q.Send(t1, pkt(id))
				switch {
				case sr.Transmitted != nil:
					q.Resolve(ifA, t1, macA, sr.Transmitted.Receipt, nil)
				case sr.Enqueued:
					q.Resolve(ifA, t1, macA, Receipt{}, nil)
				}
				q.Tick(start.Add(time.Duration(k) * time.Second))
				_ = q.State(ifA, t1)
				_ = q.Queued(ifA, t1)
			}
		}(g)
	}
	wg.Wait()

	stats, total := q.Stats()
	sum := uint64(0)
	for _, s := range stats {
		sum += s.Transmitted + s.Enqueued + s.Failed + s.NoRoute
	}
	if total.Transmitted+total.Enqueued+total.Failed+total.NoRoute != sum {
		t.Fatalf("per-interface totals do not match aggregate: stats=%+v total=%+v", stats, total)
	}
}

// staleConfirmSetup builds: Reachable -> (tick) Stale -> Send -> Confirm with
// one extra queued packet, returning the queue and the probe receipt.
func staleConfirmSetup(t *testing.T) (*Queue, Receipt) {
	t.Helper()
	return staleConfirmSetupAt(t, time.Unix(0, 0))
}

func staleConfirmSetupAt(t *testing.T, start time.Time) (*Queue, Receipt) {
	t.Helper()
	q := testQueue(t, start)
	q.SetRoute(t1, ifA)
	q.Send(t1, pkt(1))
	q.Resolve(ifA, t1, macA, Receipt{}, nil)
	q.Tick(start.Add(10 * time.Second)) // Stale
	r := q.Send(t1, pkt(2))             // probe -> Confirm
	if r.Transmitted == nil {
		t.Fatalf("setup probe not transmitted: %+v", r)
	}
	q.Send(t1, pkt(3)) // queued during Confirm
	return q, r.Transmitted.Receipt
}
