package eventbuffer

import (
	"errors"
	"fmt"
	"sync"
	"testing"
)

// findDelivery 从 Dispatch 结果中按队列名取出投递记录。
func findDelivery(t *testing.T, rs []DispatchResult, name string) *Delivery {
	t.Helper()
	for _, r := range rs {
		if r.Name == name {
			if r.Err != nil {
				t.Fatalf("dispatch to %s unexpected err: %v", name, r.Err)
			}
			return r.Delivery
		}
	}
	t.Fatalf("queue %s not found in dispatch results", name)
	return nil
}

// checkInvariants 在锁内核对缓冲内部记账的全部关键不变量。
func checkInvariants(t *testing.T, b *Buffer) {
	t.Helper()
	b.mu.Lock()
	defer b.mu.Unlock()

	// 1. order 与 messages 严格一致且升序；occupancy 等于驻留消息大小之和。
	sum := 0
	prev := -1
	for _, id := range b.order {
		if id <= prev {
			t.Fatalf("order not strictly increasing: %v", b.order)
		}
		prev = id
		msg, ok := b.messages[id]
		if !ok {
			t.Fatalf("order references missing message %d", id)
		}
		sum += msg.Size
	}
	if len(b.order) != len(b.messages) {
		t.Fatalf("order/messages length mismatch: %d vs %d", len(b.order), len(b.messages))
	}
	if sum != b.occupancy {
		t.Fatalf("occupancy %d != sum of resident sizes %d", b.occupancy, sum)
	}

	// 2. 每条驻留消息的 refs 等于其在途投递数。
	pendingByMsg := map[int]int{}
	pendingSizeByQueue := map[int]int{}
	pendingCountByQueue := map[int]int{}
	for _, d := range b.deliveries {
		if d.Outcome != Pending {
			continue
		}
		pendingByMsg[d.MessageID]++
		pendingSizeByQueue[d.QueueID] += d.Size
		pendingCountByQueue[d.QueueID]++
	}
	for id, msg := range b.messages {
		if got := pendingByMsg[id]; got != msg.refs {
			t.Fatalf("message %d refs=%d but pending deliveries=%d", id, msg.refs, got)
		}
		if msg.refs < 0 {
			t.Fatalf("message %d negative refs", id)
		}
	}

	// 3. 每个队列的在途字节/在途数量与投递记录一致，且不超过额度。
	for id, q := range b.queues {
		if q.inFlight != pendingSizeByQueue[id] {
			t.Fatalf("queue %d inflight=%d but deliveries sum=%d", id, q.inFlight, pendingSizeByQueue[id])
		}
		if q.pending != pendingCountByQueue[id] {
			t.Fatalf("queue %d pending=%d but delivery count=%d", id, q.pending, pendingCountByQueue[id])
		}
		if q.inFlight > q.quota {
			t.Fatalf("queue %d inflight %d exceeds quota %d", id, q.inFlight, q.quota)
		}
		if q.inFlight < 0 || q.pending < 0 {
			t.Fatalf("queue %d negative accounting: inflight=%d pending=%d", id, q.inFlight, q.pending)
		}
	}

	// 4. names 仅指向 active 队列；每个 active 队列都在 names 中。
	activeByName := map[int]bool{}
	for name, id := range b.names {
		q := b.queues[id]
		if q == nil || q.state != stateActive || q.name != name {
			t.Fatalf("name %s maps to non-active queue %v", name, q)
		}
		activeByName[id] = true
	}
	for id, q := range b.queues {
		if q.state == stateActive && !activeByName[id] {
			t.Fatalf("active queue %d missing from names", id)
		}
	}
}

func TestPayloadStoredOnceAndSharedAcrossSubscribers(t *testing.T) {
	b := NewBuffer()
	q1 := b.Subscribe("a", 100)
	q2 := b.Subscribe("b", 100)

	input := []byte("hello")
	m1 := b.Publish(input)
	m2 := b.Publish([]byte("world!!"))

	// Publish 必须复制输入，调用方之后修改不影响缓冲内负载。
	input[0] = 'X'
	if got := string(m1.Payload()); got != "hello" {
		t.Fatalf("payload not copied at publish, got %q", got)
	}

	d1a, err := b.Deliver(q1, m1)
	if err != nil {
		t.Fatal(err)
	}
	d2a, err := b.Deliver(q1, m2)
	if err != nil {
		t.Fatal(err)
	}
	d1b, err := b.Deliver(q2, m1)
	if err != nil {
		t.Fatal(err)
	}

	// 所有投递指向同一条消息对象：负载在缓冲中只有一份。
	if b.messages[d1a.MessageID] != m1 || b.messages[d1b.MessageID] != m1 {
		t.Fatal("deliveries do not resolve to the single shared message object")
	}
	if d1a.MessageID != 0 || d2a.MessageID != 1 {
		t.Fatalf("unexpected message ids: %d %d", d1a.MessageID, d2a.MessageID)
	}

	if got, _ := b.Inflight(q1); got != 12 { // 5 + 7
		t.Fatalf("q1 inflight = %d, want 12", got)
	}
	if got, _ := b.Inflight(q2); got != 5 {
		t.Fatalf("q2 inflight = %d, want 5", got)
	}
	if b.Occupancy() != 12 {
		t.Fatalf("occupancy = %d, want 12", b.Occupancy())
	}
	if m1.refs != 2 || m2.refs != 1 {
		t.Fatalf("refs = %d/%d, want 2/1", m1.refs, m2.refs)
	}
	checkInvariants(t, b)
}

func TestQuotaCheckedPerSubscriberBeforeDispatch(t *testing.T) {
	b := NewBuffer()
	small := b.Subscribe("small", 10)
	big := b.Subscribe("big", 100)
	msg := b.Publish(make([]byte, 6))

	d1, err := b.Deliver(small, msg)
	if err != nil {
		t.Fatal(err)
	}
	// 第二次派送会超出 small 自己的额度：必须在派送前拦截，不留任何记录。
	if _, err := b.Deliver(small, msg); !errors.Is(err, ErrQuotaExceeded) {
		t.Fatalf("want ErrQuotaExceeded, got %v", err)
	}
	if got, _ := b.Inflight(small); got != 6 {
		t.Fatalf("small inflight = %d, want 6", got)
	}
	if msg.refs != 1 {
		t.Fatalf("refs = %d, want 1 (failed dispatch must not take a ref)", msg.refs)
	}
	if len(b.deliveries) != 1 {
		t.Fatalf("deliveries = %d, want 1", len(b.deliveries))
	}

	// 一个订阅额满不影响其他订阅的独立额度。
	if _, err := b.Deliver(big, msg); err != nil {
		t.Fatalf("big queue should be unaffected: %v", err)
	}
	if got, _ := b.Inflight(big); got != 6 {
		t.Fatalf("big inflight = %d, want 6", got)
	}

	// 结算释放额度后可再次派送。
	if err := b.Ack(d1); err != nil {
		t.Fatal(err)
	}
	if _, err := b.Deliver(small, msg); err != nil {
		t.Fatalf("redeliver after ack should succeed: %v", err)
	}

	// Dispatch 的逐订阅结果：额度不足的订阅报错，其他订阅照常成功。
	rs := b.Dispatch(msg)
	if len(rs) != 2 {
		t.Fatalf("dispatch results = %d, want 2", len(rs))
	}
	for _, r := range rs {
		switch r.Name {
		case "small":
			if !errors.Is(r.Err, ErrQuotaExceeded) {
				t.Fatalf("small: want quota err, got %v", r.Err)
			}
		case "big":
			if r.Err != nil || r.Delivery == nil {
				t.Fatalf("big: unexpected %v", r.Err)
			}
		}
	}
	checkInvariants(t, b)
}

func TestAckAndCancelReclaimOnlyOwnQuota(t *testing.T) {
	b := NewBuffer()
	q1 := b.Subscribe("a", 100)
	q2 := b.Subscribe("b", 100)
	msg := b.Publish(make([]byte, 10))

	d1, _ := b.Deliver(q1, msg)
	d2, _ := b.Deliver(q2, msg)

	if err := b.Ack(d1); err != nil {
		t.Fatal(err)
	}
	if got, _ := b.Inflight(q1); got != 0 {
		t.Fatalf("q1 inflight = %d, want 0", got)
	}
	if got, _ := b.Inflight(q2); got != 10 {
		t.Fatalf("q2 inflight must stay 10, got %d", got)
	}
	if msg.refs != 1 {
		t.Fatalf("refs = %d, want 1", msg.refs)
	}

	if err := b.Cancel(d2); err != nil {
		t.Fatal(err)
	}
	if got, _ := b.Inflight(q2); got != 0 {
		t.Fatalf("q2 inflight = %d, want 0", got)
	}
	if msg.refs != 0 {
		t.Fatalf("refs = %d, want 0", msg.refs)
	}
	if d1.Outcome != Acknowledged || d2.Outcome != Cancelled {
		t.Fatalf("outcomes = %q/%q", d1.Outcome, d2.Outcome)
	}

	// 重复结算必须被幂等拦截，不能重复回收。
	if err := b.Ack(d1); !errors.Is(err, ErrAlreadySettled) {
		t.Fatalf("want ErrAlreadySettled, got %v", err)
	}
	if err := b.Nack(d2); !errors.Is(err, ErrAlreadySettled) {
		t.Fatalf("want ErrAlreadySettled, got %v", err)
	}
	if err := b.Ack(nil); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("nil ack: want ErrUnknownSubscription, got %v", err)
	}

	ranges, occ := b.Reclaim()
	if len(ranges) != 1 || ranges[0] != (ReleasableRange{0, 0}) || occ != 0 {
		t.Fatalf("reclaim = %+v occ=%d, want [{0 0}] 0", ranges, occ)
	}
	checkInvariants(t, b)
}

func TestMessageHeldUntilLastReferenceSettles(t *testing.T) {
	b := NewBuffer()
	var queues []*QueueSnapshot
	for i := 0; i < 3; i++ {
		queues = append(queues, b.Subscribe(fmt.Sprintf("q%d", i), 100))
	}
	msg := b.Publish(make([]byte, 8))

	var dels []*Delivery
	for _, q := range queues {
		d, err := b.Deliver(q, msg)
		if err != nil {
			t.Fatal(err)
		}
		dels = append(dels, d)
	}

	// 无结算时不可释放。
	if rs, occ := b.Reclaim(); len(rs) != 0 || occ != 8 {
		t.Fatalf("before settle: ranges=%v occ=%d", rs, occ)
	}
	// 结算前两个引用后仍有一个在途，仍不可释放。
	if err := b.Ack(dels[0]); err != nil {
		t.Fatal(err)
	}
	if err := b.Cancel(dels[1]); err != nil {
		t.Fatal(err)
	}
	if rs, _ := b.Reclaim(); len(rs) != 0 {
		t.Fatalf("with one ref open, reclaim returned %v", rs)
	}
	if _, ok := b.messages[0]; !ok {
		t.Fatal("message must remain resident while a reference is open")
	}
	// 最后一个有效引用结束后立即可释放。
	if err := b.Ack(dels[2]); err != nil {
		t.Fatal(err)
	}
	rs, occ := b.Reclaim()
	if len(rs) != 1 || rs[0] != (ReleasableRange{0, 0}) || occ != 0 {
		t.Fatalf("final reclaim = %+v occ=%d", rs, occ)
	}
	checkInvariants(t, b)
}

func TestReclaimRangesWithHolesAndOccupancy(t *testing.T) {
	b := NewBuffer()
	b.Subscribe("a", 1000)
	b.Subscribe("c", 1000)

	sizes := []int{10, 20, 30, 40}
	var msgs []*Message
	for _, s := range sizes {
		msgs = append(msgs, b.Publish(make([]byte, s)))
	}
	var aDel, cDel []*Delivery
	for _, m := range msgs {
		rs := b.Dispatch(m)
		aDel = append(aDel, findDelivery(t, rs, "a"))
		cDel = append(cDel, findDelivery(t, rs, "c"))
	}

	// m0 双方都结算；m1 只保留 a 的在途；m2 双方都结算；m3 只保留 c 的在途。
	if err := b.Ack(aDel[0]); err != nil {
		t.Fatal(err)
	}
	if err := b.Ack(cDel[0]); err != nil {
		t.Fatal(err)
	}
	if err := b.Ack(cDel[1]); err != nil {
		t.Fatal(err)
	}
	if err := b.Ack(aDel[2]); err != nil {
		t.Fatal(err)
	}
	if err := b.Ack(cDel[2]); err != nil {
		t.Fatal(err)
	}
	if err := b.Ack(aDel[3]); err != nil {
		t.Fatal(err)
	}

	wantRanges := []ReleasableRange{{0, 0}, {2, 2}}
	rs, occ := b.Releasable()
	if fmt.Sprint(rs) != fmt.Sprint(wantRanges) || occ != 100 {
		t.Fatalf("releasable = %v occ=%d, want %v 100", rs, occ, wantRanges)
	}
	// Releasable 是只读观察：消息仍驻留。
	if len(b.messages) != 4 {
		t.Fatalf("Releasable must not remove messages, got %d resident", len(b.messages))
	}

	rs, occ = b.Reclaim()
	if fmt.Sprint(rs) != fmt.Sprint(wantRanges) || occ != 60 { // 20 + 40 仍驻留
		t.Fatalf("reclaim = %v occ=%d, want %v 60", rs, occ, wantRanges)
	}
	if _, ok := b.messages[1]; !ok {
		t.Fatal("message 1 should remain")
	}
	if _, ok := b.messages[3]; !ok {
		t.Fatal("message 3 should remain")
	}

	// 剩余引用结束后，非连续 ID 形成两个独立范围。
	if err := b.Ack(aDel[1]); err != nil {
		t.Fatal(err)
	}
	if err := b.Ack(cDel[3]); err != nil {
		t.Fatal(err)
	}
	rs, occ = b.Reclaim()
	if fmt.Sprint(rs) != fmt.Sprint([]ReleasableRange{{1, 1}, {3, 3}}) || occ != 0 {
		t.Fatalf("final reclaim = %v occ=%d", rs, occ)
	}

	// 无任何投递的新消息 refs 为 0，连续 ID 合并为单范围。
	for i := 0; i < 3; i++ {
		b.Publish(make([]byte, 1))
	}
	rs, occ = b.Reclaim()
	if fmt.Sprint(rs) != fmt.Sprint([]ReleasableRange{{4, 6}}) || occ != 0 {
		t.Fatalf("contiguous reclaim = %v occ=%d", rs, occ)
	}
	checkInvariants(t, b)
}

func TestResubscribeSameNameFreshQuotaAndOldDeliverySettlement(t *testing.T) {
	b := NewBuffer()
	old := b.Subscribe("svc", 10)
	msg := b.Publish(make([]byte, 4))

	dOld, err := b.Deliver(old, msg)
	if err != nil {
		t.Fatal(err)
	}

	fresh := b.Subscribe("svc", 100) // 同名重订
	if fresh.ID == old.ID {
		t.Fatal("resubscribe must allocate a new queue id")
	}
	if fresh.Generation != 2 || fresh.InFlight != 0 || fresh.Quota != 100 || !fresh.Active {
		t.Fatalf("fresh queue snapshot wrong: %+v", fresh)
	}
	byName, err := b.QueueByName("svc")
	if err != nil || byName.ID != fresh.ID {
		t.Fatalf("name should point to fresh queue: %+v err=%v", byName, err)
	}

	oldSnap, _ := b.Queue(old.ID)
	if oldSnap.Active || oldSnap.State != stateReplaced || oldSnap.InFlight != 4 || oldSnap.Pending != 1 {
		t.Fatalf("old queue wrong: %+v", oldSnap)
	}

	// 旧队列句柄不能再接收派送。
	if _, err := b.Deliver(old, msg); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("deliver to replaced queue: want ErrUnknownSubscription, got %v", err)
	}
	// 新队列有独立额度，同一条消息取得自己的新引用。
	dNew, err := b.Deliver(fresh, msg)
	if err != nil {
		t.Fatal(err)
	}
	if msg.refs != 2 {
		t.Fatalf("refs = %d, want 2", msg.refs)
	}

	// 旧确认仍归到旧投递记录，回收旧队列额度，不影响新队列。
	if err := b.Ack(dOld); err != nil {
		t.Fatalf("ack against old delivery must still work: %v", err)
	}
	if dOld.Outcome != Acknowledged {
		t.Fatalf("old delivery outcome = %q", dOld.Outcome)
	}
	oldSnap, _ = b.Queue(old.ID)
	if oldSnap.InFlight != 0 || oldSnap.Pending != 0 {
		t.Fatalf("old queue after ack: %+v", oldSnap)
	}
	if got, _ := b.Inflight(fresh); got != 4 {
		t.Fatalf("fresh queue inflight = %d, want 4", got)
	}
	if msg.refs != 1 {
		t.Fatalf("refs = %d, want 1", msg.refs)
	}
	if err := b.Ack(dNew); err != nil {
		t.Fatal(err)
	}
	rs, occ := b.Reclaim()
	if len(rs) != 1 || occ != 0 {
		t.Fatalf("reclaim = %v occ=%d", rs, occ)
	}
	checkInvariants(t, b)
}

func TestRenameIssuesFreshQueueAndKeepsOldDeliveries(t *testing.T) {
	b := NewBuffer()
	old := b.Subscribe("old", 50)
	other := b.Subscribe("other", 50)
	msg := b.Publish(make([]byte, 7))
	dOld, err := b.Deliver(old, msg)
	if err != nil {
		t.Fatal(err)
	}

	renamed, err := b.Rename(old, "new", 500)
	if err != nil {
		t.Fatal(err)
	}
	if renamed.Name != "new" || renamed.Quota != 500 || renamed.InFlight != 0 || renamed.Generation != 1 {
		t.Fatalf("renamed queue wrong: %+v", renamed)
	}
	if _, err := b.QueueByName("old"); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("old name lookup: want error, got %v", err)
	}
	if q, err := b.QueueByName("new"); err != nil || q.ID != renamed.ID {
		t.Fatalf("new name lookup wrong: %+v %v", q, err)
	}
	oldSnap, _ := b.Queue(old.ID)
	if oldSnap.State != stateReplaced || oldSnap.InFlight != 7 {
		t.Fatalf("old queue after rename wrong: %+v", oldSnap)
	}

	// 旧投递仍可结算到旧记录。
	if err := b.Ack(dOld); err != nil {
		t.Fatal(err)
	}
	// 陈旧句柄不能再更名；目标名冲突与同名更名都要拒绝。
	if _, err := b.Rename(old, "x", 1); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("rename stale handle: want ErrUnknownSubscription, got %v", err)
	}
	if _, err := b.Rename(renamed, "new", 1); !errors.Is(err, ErrNameInUse) {
		t.Fatalf("rename to same name: want ErrNameInUse, got %v", err)
	}
	if _, err := b.Rename(renamed, "other", 1); !errors.Is(err, ErrNameInUse) {
		t.Fatalf("rename to taken name: want ErrNameInUse, got %v", err)
	}
	// other 队列未受影响。
	if q, _ := b.QueueByName("other"); q.ID != other.ID {
		t.Fatal("other queue must remain intact")
	}
	checkInvariants(t, b)
}

func TestSlowSubscriberLeavesFastAckedMessagesReclaimableAfterSettle(t *testing.T) {
	b := NewBuffer()
	slow := b.Subscribe("slow", 100)
	fast := b.Subscribe("fast", 100)

	var msgs []*Message
	var slowDel, fastDel []*Delivery
	for i := 0; i < 3; i++ {
		m := b.Publish(make([]byte, 10))
		msgs = append(msgs, m)
		rs := b.Dispatch(m)
		slowDel = append(slowDel, findDelivery(t, rs, "slow"))
		fastDel = append(fastDel, findDelivery(t, rs, "fast"))
	}

	// 快订阅全部确认；慢订阅一条未动。
	for _, d := range fastDel {
		if err := b.Ack(d); err != nil {
			t.Fatal(err)
		}
	}
	if got, _ := b.Inflight(fast); got != 0 {
		t.Fatalf("fast inflight = %d, want 0", got)
	}
	if got, _ := b.Inflight(slow); got != 30 {
		t.Fatalf("slow inflight = %d, want 30", got)
	}
	// 慢订阅仍持有全部引用：无可释放范围，占用不变。
	if rs, occ := b.Reclaim(); len(rs) != 0 || occ != 30 {
		t.Fatalf("before slow settle: ranges=%v occ=%d", rs, occ)
	}

	// 慢订阅退出：旧投递记录保留，仍可逐条结算。
	if err := b.Unsubscribe(slow); err != nil {
		t.Fatal(err)
	}
	if q, _ := b.Queue(slow.ID); q.Active || q.State != stateClosed || q.Pending != 3 {
		t.Fatalf("slow after unsubscribe: %+v", q)
	}
	if _, err := b.QueueByName("slow"); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("lookup after unsubscribe: want error, got %v", err)
	}
	// 退订本身不自动取消在途投递：引用仍有效。
	if rs, _ := b.Reclaim(); len(rs) != 0 {
		t.Fatalf("unsubscribe alone must not release messages: %v", rs)
	}
	// 重复退订报错。
	if err := b.Unsubscribe(slow); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("double unsubscribe: want ErrUnknownSubscription, got %v", err)
	}

	// 慢订阅的在途全部取消后，快订阅早已确认的消息立刻整体可释放。
	for _, d := range slowDel {
		if err := b.Cancel(d); err != nil {
			t.Fatal(err)
		}
	}
	rs, occ := b.Reclaim()
	if fmt.Sprint(rs) != fmt.Sprint([]ReleasableRange{{0, 2}}) || occ != 0 {
		t.Fatalf("reclaim after slow settles = %v occ=%d", rs, occ)
	}
	if got, _ := b.Inflight(slow); got != 0 {
		t.Fatalf("slow inflight after cancel = %d, want 0", got)
	}
	checkInvariants(t, b)
}

func TestZeroByteMessagesAndZeroQuota(t *testing.T) {
	b := NewBuffer()
	noQuota := b.Subscribe("zero", 0)

	empty := b.Publish(nil) // 0 字节
	if empty.Size != 0 {
		t.Fatalf("size = %d, want 0", empty.Size)
	}
	// 0 字节消息不消耗额度，零额度队列也可派送。
	d, err := b.Deliver(noQuota, empty)
	if err != nil {
		t.Fatalf("zero-byte deliver: %v", err)
	}
	if err := b.Ack(d); err != nil {
		t.Fatal(err)
	}
	rs, occ := b.Reclaim()
	if len(rs) != 1 || occ != 0 {
		t.Fatalf("zero-byte reclaim = %v occ=%d", rs, occ)
	}

	one := b.Publish([]byte{1})
	if _, err := b.Deliver(noQuota, one); !errors.Is(err, ErrQuotaExceeded) {
		t.Fatalf("zero quota + 1 byte: want ErrQuotaExceeded, got %v", err)
	}
	checkInvariants(t, b)
}

func TestConcurrentDispatchSettleReclaim(t *testing.T) {
	b := NewBuffer()
	const nQueues = 4
	const nMsgs = 200
	var queues []*QueueSnapshot
	for i := 0; i < nQueues; i++ {
		queues = append(queues, b.Subscribe(fmt.Sprintf("q%d", i), nMsgs*16))
	}

	var msgs []*Message
	for i := 0; i < nMsgs; i++ {
		msgs = append(msgs, b.Publish(make([]byte, i%11)))
	}

	stop := make(chan struct{})
	var observerWg sync.WaitGroup
	observerWg.Add(1)
	go func() {
		defer observerWg.Done()
		for {
			select {
			case <-stop:
				return
			default:
				checkInvariants(t, b)
				_ = b.Snapshot()
				_, _ = b.Releasable()
			}
		}
	}()

	var wg sync.WaitGroup
	for qi, q := range queues {
		qi, q := qi, q
		wg.Add(1)
		go func() {
			defer wg.Done()
			for mi, m := range msgs {
				d, err := b.Deliver(q, m)
				if err != nil {
					t.Errorf("q%d m%d deliver: %v", qi, mi, err)
					return
				}
				// 交错确认：部分立即确认，部分在第二轮统一确认。
				if (qi+mi)%2 == 0 {
					if err := b.Ack(d); err != nil {
						t.Errorf("q%d m%d ack: %v", qi, mi, err)
						return
					}
				}
			}
		}()
	}
	wg.Wait()

	// 并发回收循环：只能回收 refs 已归零的消息，累计实际回收的范围。
	var reclaimMu sync.Mutex
	var reclaimed []ReleasableRange
	var reclaimWg sync.WaitGroup
	reclaimWg.Add(1)
	go func() {
		defer reclaimWg.Done()
		for {
			select {
			case <-stop:
				return
			default:
				rs, _ := b.Reclaim()
				if len(rs) > 0 {
					reclaimMu.Lock()
					reclaimed = append(reclaimed, rs...)
					reclaimMu.Unlock()
				}
			}
		}
	}()

	// 第二轮：把剩余在途全部结算（确认/取消交错）。
	for qi, q := range queues {
		qi, q := qi, q
		wg.Add(1)
		go func() {
			defer wg.Done()
			for _, m := range msgs {
				for _, d := range b.deliveries {
					if d.QueueID == q.ID && d.MessageID == m.ID && d.Outcome == Pending {
						var err error
						if qi%2 == 0 {
							err = b.Ack(d)
						} else {
							err = b.Cancel(d)
						}
						if err != nil {
							t.Errorf("settle q%d m%d: %v", qi, m.ID, err)
						}
					}
				}
			}
		}()
	}
	wg.Wait()
	close(stop)
	observerWg.Wait()
	reclaimWg.Wait()

	// 全部引用结束后，最终一次回收应清空缓冲。
	rs, occ := b.Reclaim()
	reclaimMu.Lock()
	reclaimed = append(reclaimed, rs...)
	reclaimMu.Unlock()
	if occ != 0 || len(b.messages) != 0 || len(b.order) != 0 {
		t.Fatalf("final state: ranges=%v occ=%d msgs=%d", rs, occ, len(b.messages))
	}
	// 并发回收的范围累计起来必须恰好覆盖全部消息 ID，且无重叠、无越界。
	covered := make([]bool, nMsgs)
	for _, r := range reclaimed {
		if r.From < 0 || r.To >= nMsgs || r.From > r.To {
			t.Fatalf("invalid range %+v", r)
		}
		for id := r.From; id <= r.To; id++ {
			if covered[id] {
				t.Fatalf("message %d reclaimed twice", id)
			}
			covered[id] = true
		}
	}
	for id, ok := range covered {
		if !ok {
			t.Fatalf("message %d was never reclaimed", id)
		}
	}
	for _, q := range queues {
		if got, _ := b.Inflight(q); got != 0 {
			t.Fatalf("queue %d inflight = %d, want 0", q.ID, got)
		}
	}
	checkInvariants(t, b)
}

func TestNilAndDefensiveInputs(t *testing.T) {
	b := NewBuffer()
	msg := b.Publish([]byte("x"))

	if _, err := b.Deliver(nil, msg); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("nil queue: got %v", err)
	}
	q := b.Subscribe("q", 10)
	if _, err := b.Deliver(q, nil); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("nil message: got %v", err)
	}
	if _, err := b.Inflight(nil); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("inflight nil: got %v", err)
	}
	if _, err := b.Rename(nil, "x", 1); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("rename nil: got %v", err)
	}
	if err := b.Unsubscribe(nil); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("unsubscribe nil: got %v", err)
	}
	if _, err := b.QueueByName("missing"); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("missing name: got %v", err)
	}
	if _, err := b.Queue(9999); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("missing id: got %v", err)
	}
	if _, err := b.Inflight(&QueueSnapshot{ID: 9999}); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("inflight unknown id: got %v", err)
	}
	stale := &QueueSnapshot{ID: 9999, Name: "ghost"}
	if _, err := b.Deliver(stale, msg); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("unknown queue deliver: got %v", err)
	}
	if _, err := b.Rename(stale, "x", 1); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("unknown queue rename: got %v", err)
	}
	if err := b.Unsubscribe(stale); !errors.Is(err, ErrUnknownSubscription) {
		t.Fatalf("unknown queue unsubscribe: got %v", err)
	}
	// Refs 访问器与公开状态快照保持一致。
	d, err := b.Deliver(q, msg)
	if err != nil {
		t.Fatal(err)
	}
	if msg.Refs() != 1 {
		t.Fatalf("Refs = %d, want 1", msg.Refs())
	}
	if err := b.Ack(d); err != nil {
		t.Fatal(err)
	}
	if msg.Refs() != 0 {
		t.Fatalf("Refs = %d, want 0", msg.Refs())
	}
	checkInvariants(t, b)
}

func TestReclaimedMessageRejectsNewDelivery(t *testing.T) {
	b := NewBuffer()
	q := b.Subscribe("q", 100)
	msg := b.Publish([]byte("abc"))
	// 没有任何投递，消息 refs=0，可直接回收。
	if rs, _ := b.Reclaim(); len(rs) != 1 {
		t.Fatalf("reclaim = %v", rs)
	}
	if _, err := b.Deliver(q, msg); err == nil {
		t.Fatal("delivery against reclaimed message must fail")
	}
	if rs := b.Dispatch(msg); rs[0].Err == nil {
		t.Fatal("dispatch of reclaimed message must report an error")
	}
	checkInvariants(t, b)
}
