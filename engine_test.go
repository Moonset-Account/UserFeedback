package officeaccess

import (
	"errors"
	"reflect"
	"sort"
	"testing"
)

const (
	segA = "10.10.1.0/24"
	segB = "10.10.2.0/24"
)

func mustAllocate(t *testing.T, e *Engine, seq int64, receipt, session, segment, addr string) {
	t.Helper()
	if err := e.HandleAllocation(Allocation{
		Sequence: seq, ReceiptID: receipt, Session: session, Segment: segment, Address: addr,
	}); err != nil {
		t.Fatalf("HandleAllocation(%s,%s) unexpected error: %v", session, addr, err)
	}
}

func mustRelease(t *testing.T, e *Engine, seq int64, receipt, session, segment, addr string) {
	t.Helper()
	if err := e.HandleAllocation(Allocation{
		Sequence: seq, ReceiptID: receipt, Session: session, Segment: segment, Address: addr, Released: true,
	}); err != nil {
		t.Fatalf("release(%s,%s) unexpected error: %v", session, addr, err)
	}
}

func claim(t *testing.T, e *Engine, seq int64, port, session, segment, addr string) Decision {
	t.Helper()
	d, err := e.HandleClaim(Claim{
		Sequence: seq, PortID: port, Session: session, Segment: segment, Address: addr,
	})
	if err != nil {
		t.Fatalf("HandleClaim(%s,%s) unexpected error: %v", session, addr, err)
	}
	return d
}

func assertBindingPorts(t *testing.T, snap Snapshot, want map[string][]Binding) {
	t.Helper()
	if !reflect.DeepEqual(snap.PortBindings, want) {
		t.Fatalf("port bindings mismatch\n got: %#v\nwant: %#v", snap.PortBindings, want)
	}
}

func assertIsolatedAddrs(t *testing.T, snap Snapshot, want []addrKey) {
	t.Helper()
	var got []addrKey
	for _, rec := range snap.Isolations {
		got = append(got, key(rec.Segment, rec.Address))
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("isolation records mismatch\n got: %v\nwant: %v", got, want)
	}
}

// 可接受结果：多个已确认地址，且网段/地址/当前会话三者同时匹配，全部入绑定表；
// 同一绑定的重复声明幂等，不产生重复记录。
func TestAcceptedClaimsBuildBindings(t *testing.T) {
	e := NewEngine()
	if err := e.HandlePortEvent(PortEvent{Sequence: 1, PortID: "p1", Session: "s1", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	mustAllocate(t, e, 2, "r-1", "s1", segA, "10.10.1.11")
	mustAllocate(t, e, 3, "r-2", "s1", segA, "10.10.1.12")
	mustAllocate(t, e, 4, "r-3", "s1", segB, "10.10.2.21")

	d := claim(t, e, 5, "p1", "s1", segA, "10.10.1.11")
	if !d.Accepted || d.Reason != ReasonAccepted || d.Binding == nil {
		t.Fatalf("first claim should be accepted, got %+v", d)
	}
	if d.Binding.PortID != "p1" || d.Binding.Session != "s1" {
		t.Fatalf("binding fields wrong: %+v", d.Binding)
	}
	claim(t, e, 6, "p1", "s1", segA, "10.10.1.12")
	// Repeating an established binding is idempotent.
	again := claim(t, e, 7, "p1", "s1", segA, "10.10.1.11")
	if !again.Accepted || again.Reason != ReasonAlreadyBound {
		t.Fatalf("duplicate claim should be already_bound, got %+v", again)
	}
	claim(t, e, 8, "p1", "s1", segB, "10.10.2.21")

	snap := e.Snapshot()
	assertBindingPorts(t, snap, map[string][]Binding{
		"p1": {
			{PortID: "p1", Session: "s1", Segment: segA, Address: "10.10.1.11", Sequence: 5},
			{PortID: "p1", Session: "s1", Segment: segA, Address: "10.10.1.12", Sequence: 6},
			{PortID: "p1", Session: "s1", Segment: segB, Address: "10.10.2.21", Sequence: 8},
		},
	})
	if len(snap.Isolations) != 0 || len(snap.RejectedClaims) != 0 {
		t.Fatalf("expected no isolation/rejection, got %+v", snap)
	}

	// Deterministic ordering on repeated snapshots.
	snap2 := e.Snapshot()
	if !reflect.DeepEqual(snap.PortBindings, snap2.PortBindings) {
		t.Fatal("snapshot ordering not stable")
	}
}

// 应被阻止：非当前端口会话、缺少确认分配、网段三元组不匹配的声明全部拒绝。
// 同时覆盖“分配先于端口事件到达”的交错场景——分配被记录但在端口接入前不能绑定。
func TestRejectedClaimsAndInterleavedArrival(t *testing.T) {
	e := NewEngine()
	// Allocation arrives before any port attachment.
	mustAllocate(t, e, 1, "r-1", "s1", segA, "10.10.1.11")
	early := claim(t, e, 2, "p1", "s1", segA, "10.10.1.11")
	if early.Accepted || early.Reason != ReasonSessionNotOnPort {
		t.Fatalf("claim before port-up must be refused, got %+v", early)
	}

	if err := e.HandlePortEvent(PortEvent{Sequence: 3, PortID: "p1", Session: "s1", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	// Now the previously-arrived allocation makes the claim admissible.
	if d := claim(t, e, 4, "p1", "s1", segA, "10.10.1.11"); !d.Accepted {
		t.Fatalf("claim after port-up should be accepted, got %+v", d)
	}

	// Claiming a port whose current attachment is a different session.
	if err := e.HandlePortEvent(PortEvent{Sequence: 5, PortID: "p2", Session: "s2", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	wrongSession := claim(t, e, 6, "p2", "s1", segA, "10.10.1.11")
	if wrongSession.Accepted || wrongSession.Reason != ReasonSessionNotOnPort {
		t.Fatalf("cross-session claim must be refused, got %+v", wrongSession)
	}

	// Address never confirmed by the allocator.
	noAlloc := claim(t, e, 7, "p1", "s1", segA, "10.10.1.99")
	if noAlloc.Accepted || noAlloc.Reason != ReasonNoConfirmedAllocation {
		t.Fatalf("unconfirmed address must be refused, got %+v", noAlloc)
	}

	// Address confirmed in another segment: the full triple must match.
	mustAllocate(t, e, 8, "r-9", "s1", segB, "10.10.2.77")
	wrongSegment := claim(t, e, 9, "p1", "s1", segA, "10.10.2.77")
	if wrongSegment.Accepted || wrongSegment.Reason != ReasonNoConfirmedAllocation {
		t.Fatalf("segment mismatch must be refused, got %+v", wrongSegment)
	}

	snap := e.Snapshot()
	if len(snap.RejectedClaims) != 4 {
		t.Fatalf("expected 4 rejected claims, got %d", len(snap.RejectedClaims))
	}
	reasons := []string{
		ReasonSessionNotOnPort,
		ReasonSessionNotOnPort,
		ReasonNoConfirmedAllocation,
		ReasonNoConfirmedAllocation,
	}
	gotReasons := make([]string, 0, len(snap.RejectedClaims))
	for _, r := range snap.RejectedClaims {
		gotReasons = append(gotReasons, r.Reason)
	}
	sort.Strings(gotReasons)
	sort.Strings(reasons)
	if !reflect.DeepEqual(gotReasons, reasons) {
		t.Fatalf("rejection reasons mismatch: got %v want %v", gotReasons, reasons)
	}
	assertBindingPorts(t, snap, map[string][]Binding{
		"p1": {{PortID: "p1", Session: "s1", Segment: segA, Address: "10.10.1.11", Sequence: 4}},
		"p2": {},
	})
}

// 交叉约束：两个经确认的分配在同一网段/地址重叠时，受影响地址被隔离，
// 其他地址继续可绑定；释放其中一个冲突方后，隔离解除，剩余一方可以绑定。
func TestOverlappingAllocationsIsolateAffectedAddressOnly(t *testing.T) {
	e := NewEngine()
	if err := e.HandlePortEvent(PortEvent{Sequence: 1, PortID: "p1", Session: "s1", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	if err := e.HandlePortEvent(PortEvent{Sequence: 2, PortID: "p2", Session: "s2", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	mustAllocate(t, e, 3, "r-1", "s1", segA, "10.10.1.50")
	mustAllocate(t, e, 4, "r-2", "s1", segA, "10.10.1.51")
	mustAllocate(t, e, 5, "r-3", "s2", segA, "10.10.1.50") // overlap on .50
	mustAllocate(t, e, 6, "r-4", "s2", segB, "10.10.2.50")

	snap := e.Snapshot()
	assertIsolatedAddrs(t, snap, []addrKey{key(segA, "10.10.1.50")})
	rec := snap.Isolations[0]
	if !reflect.DeepEqual(rec.Sessions, []string{"s1", "s2"}) || rec.Sequence != 5 {
		t.Fatalf("isolation record wrong: %+v", rec)
	}

	// Both sessions are blocked on the isolated address.
	d1 := claim(t, e, 7, "p1", "s1", segA, "10.10.1.50")
	if d1.Accepted || d1.Reason != ReasonAddressIsolated {
		t.Fatalf("isolated address claim must be refused, got %+v", d1)
	}
	d2 := claim(t, e, 8, "p2", "s2", segA, "10.10.1.50")
	if d2.Accepted || d2.Reason != ReasonAddressIsolated {
		t.Fatalf("isolated address claim must be refused, got %+v", d2)
	}

	// Unaffected addresses remain fully usable on both ports.
	if d := claim(t, e, 9, "p1", "s1", segA, "10.10.1.51"); !d.Accepted {
		t.Fatalf("unaffected s1 address should bind: %+v", d)
	}
	if d := claim(t, e, 10, "p2", "s2", segB, "10.10.2.50"); !d.Accepted {
		t.Fatalf("unaffected s2 address should bind: %+v", d)
	}

	// Reconfirming the same overlapping receipt must not duplicate the record.
	mustAllocate(t, e, 11, "r-3", "s2", segA, "10.10.1.50")
	if got := len(e.Snapshot().Isolations); got != 1 {
		t.Fatalf("expected one isolation record, got %d", got)
	}

	// One party releases: active isolation clears, the remaining holder can bind.
	mustRelease(t, e, 12, "r-1", "s1", segA, "10.10.1.50")
	assertIsolatedAddrs(t, e.Snapshot(), []addrKey{key(segA, "10.10.1.50")}) // history retained
	if _, stillIsolated := e.isolated[key(segA, "10.10.1.50")]; stillIsolated {
		t.Fatal("active isolation set should be cleared after one party releases")
	}
	if d := claim(t, e, 13, "p2", "s2", segA, "10.10.1.50"); !d.Accepted {
		t.Fatalf("remaining holder should bind after overlap clears: %+v", d)
	}

	assertBindingPorts(t, e.Snapshot(), map[string][]Binding{
		"p1": {{PortID: "p1", Session: "s1", Segment: segA, Address: "10.10.1.51", Sequence: 9}},
		"p2": {
			{PortID: "p2", Session: "s2", Segment: segA, Address: "10.10.1.50", Sequence: 13},
			{PortID: "p2", Session: "s2", Segment: segB, Address: "10.10.2.50", Sequence: 10},
		},
	})

	// Re-emerging overlap is a new isolation epoch: a second record is emitted
	// and the previously accepted binding on that address is evicted, while the
	// port's unaffected bindings stay in place.
	mustAllocate(t, e, 14, "r-5", "s1", segA, "10.10.1.50")
	snap = e.Snapshot()
	assertIsolatedAddrs(t, snap, []addrKey{
		key(segA, "10.10.1.50"),
		key(segA, "10.10.1.50"),
	})
	if snap.Isolations[1].Sequence != 14 {
		t.Fatalf("second isolation epoch should be attributed to seq 14, got %+v", snap.Isolations[1])
	}
	assertBindingPorts(t, snap, map[string][]Binding{
		"p1": {{PortID: "p1", Session: "s1", Segment: segA, Address: "10.10.1.51", Sequence: 9}},
		"p2": {{PortID: "p2", Session: "s2", Segment: segB, Address: "10.10.2.50", Sequence: 10}},
	})
	if d := claim(t, e, 15, "p2", "s2", segA, "10.10.1.50"); d.Accepted || d.Reason != ReasonAddressIsolated {
		t.Fatalf("re-isolated address must be refused again, got %+v", d)
	}
}

// 边界/交错：换端口后，旧会话的迟到释放不能删除新绑定——既包括旧回执失配，
// 也包括同地址被新回执重新确认后旧释放到达的情况。
func TestPortSwitchLateReleaseCannotRemoveNewBinding(t *testing.T) {
	e := NewEngine()
	if err := e.HandlePortEvent(PortEvent{Sequence: 1, PortID: "p1", Session: "s1", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	mustAllocate(t, e, 2, "r-old", "s1", segA, "10.10.1.11")
	if d := claim(t, e, 3, "p1", "s1", segA, "10.10.1.11"); !d.Accepted {
		t.Fatalf("initial claim should bind: %+v", d)
	}

	// Move s1 to a new port; the old attachment and its binding are torn down.
	if err := e.HandlePortEvent(PortEvent{Sequence: 4, PortID: "p2", Session: "s1", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	// A fresh allocation re-confirms the same address under a new receipt.
	mustAllocate(t, e, 5, "r-new", "s1", segA, "10.10.1.11")
	if d := claim(t, e, 6, "p2", "s1", segA, "10.10.1.11"); !d.Accepted {
		t.Fatalf("re-claim on new port should bind: %+v", d)
	}

	// Late release carrying the stale receipt id must be a no-op.
	mustRelease(t, e, 7, "r-old", "s1", segA, "10.10.1.11")
	assertBindingPorts(t, e.Snapshot(), map[string][]Binding{
		"p2": {{PortID: "p2", Session: "s1", Segment: segA, Address: "10.10.1.11", Sequence: 6}},
	})

	// A late PortDown for the displaced port is likewise ignored.
	if err := e.HandlePortEvent(PortEvent{Sequence: 8, PortID: "p1", Session: "s1", Kind: PortDown}); err != nil {
		t.Fatal(err)
	}
	assertBindingPorts(t, e.Snapshot(), map[string][]Binding{
		"p2": {{PortID: "p2", Session: "s1", Segment: segA, Address: "10.10.1.11", Sequence: 6}},
	})

	// The current receipt's release does remove the current binding.
	mustRelease(t, e, 9, "r-new", "s1", segA, "10.10.1.11")
	if len(e.Snapshot().PortBindings["p2"]) != 0 {
		t.Fatalf("current receipt release should remove binding, got %+v", e.Snapshot().PortBindings)
	}
}

// 端口被另一个会话接管时，旧会话的释放不能影响新会话已经建立的绑定。
func TestDisplacedSessionReleaseDoesNotTouchNewSessionBinding(t *testing.T) {
	e := NewEngine()
	if err := e.HandlePortEvent(PortEvent{Sequence: 1, PortID: "p1", Session: "s1", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	mustAllocate(t, e, 2, "r-a", "s1", segA, "10.10.1.30")
	claim(t, e, 3, "p1", "s1", segA, "10.10.1.30")

	// s2 takes over p1; s1 is displaced and its allocations/bindings are torn
	// down as part of the port change.
	if err := e.HandlePortEvent(PortEvent{Sequence: 4, PortID: "p1", Session: "s2", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	mustAllocate(t, e, 5, "r-b", "s2", segA, "10.10.1.30")
	if d := claim(t, e, 6, "p1", "s2", segA, "10.10.1.30"); !d.Accepted {
		t.Fatalf("new session binding should be accepted: %+v", d)
	}

	// Stale release from the displaced session cannot remove s2's binding.
	mustRelease(t, e, 7, "r-a", "s1", segA, "10.10.1.30")
	assertBindingPorts(t, e.Snapshot(), map[string][]Binding{
		"p1": {{PortID: "p1", Session: "s2", Segment: segA, Address: "10.10.1.30", Sequence: 6}},
	})
}

// 交错场景：隔离仍在生效时，其中一方会话随端口下线，重叠消失，
// 活跃隔离必须解除，剩余一方可以绑定；历史隔离记录仍保留。
func TestIsolationClearsWhenOneOverlappingSessionDetaches(t *testing.T) {
	e := NewEngine()
	if err := e.HandlePortEvent(PortEvent{Sequence: 1, PortID: "p1", Session: "s1", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	if err := e.HandlePortEvent(PortEvent{Sequence: 2, PortID: "p2", Session: "s2", Kind: PortUp}); err != nil {
		t.Fatal(err)
	}
	mustAllocate(t, e, 3, "r-1", "s1", segA, "10.10.1.60")
	mustAllocate(t, e, 4, "r-2", "s2", segA, "10.10.1.60")
	assertIsolatedAddrs(t, e.Snapshot(), []addrKey{key(segA, "10.10.1.60")})

	// s1 leaves its port; the overlap should resolve, not stay stuck isolated.
	if err := e.HandlePortEvent(PortEvent{Sequence: 5, PortID: "p1", Session: "s1", Kind: PortDown}); err != nil {
		t.Fatal(err)
	}
	if _, stillIsolated := e.isolated[key(segA, "10.10.1.60")]; stillIsolated {
		t.Fatal("active isolation must clear once a conflicting session detaches")
	}
	// Historical record is retained.
	assertIsolatedAddrs(t, e.Snapshot(), []addrKey{key(segA, "10.10.1.60")})

	// The remaining live session can now bind the formerly isolated address.
	if d := claim(t, e, 6, "p2", "s2", segA, "10.10.1.60"); !d.Accepted {
		t.Fatalf("remaining session should bind after detach clears overlap: %+v", d)
	}
	assertBindingPorts(t, e.Snapshot(), map[string][]Binding{
		"p2": {{PortID: "p2", Session: "s2", Segment: segA, Address: "10.10.1.60", Sequence: 6}},
	})
}

// 格式错误的事件返回错误，且不改变引擎状态。
func TestMalformedInputsAreRejectedWithoutStateChange(t *testing.T) {
	e := NewEngine()
	before := e.Snapshot()

	if err := e.HandlePortEvent(PortEvent{PortID: "", Session: "s1", Kind: PortUp}); !errors.Is(err, ErrInvalidPortEvent) {
		t.Fatalf("expected ErrInvalidPortEvent, got %v", err)
	}
	if err := e.HandlePortEvent(PortEvent{PortID: "p1", Session: "s1", Kind: "bogus"}); !errors.Is(err, ErrInvalidPortEvent) {
		t.Fatalf("expected ErrInvalidPortEvent, got %v", err)
	}
	if err := e.HandleAllocation(Allocation{ReceiptID: "r", Session: "s1", Segment: "not-a-cidr", Address: "1.1.1.1"}); !errors.Is(err, ErrInvalidReceipt) {
		t.Fatalf("expected ErrInvalidReceipt for bad segment, got %v", err)
	}
	if err := e.HandleAllocation(Allocation{ReceiptID: "r", Session: "s1", Segment: segA, Address: "10.10.9.9"}); !errors.Is(err, ErrInvalidReceipt) {
		t.Fatalf("expected ErrInvalidReceipt for address outside segment, got %v", err)
	}
	if err := e.HandleAllocation(Allocation{ReceiptID: "r", Session: "s1", Segment: segA, Address: "not-ip"}); !errors.Is(err, ErrInvalidReceipt) {
		t.Fatalf("expected ErrInvalidReceipt for bad address, got %v", err)
	}
	if err := e.HandleAllocation(Allocation{ReceiptID: "", Session: "s1", Segment: segA, Address: "10.10.1.1"}); !errors.Is(err, ErrInvalidReceipt) {
		t.Fatalf("expected ErrInvalidReceipt for missing receipt id, got %v", err)
	}
	if _, err := e.HandleClaim(Claim{PortID: "p1", Session: "", Segment: segA, Address: "10.10.1.1"}); !errors.Is(err, ErrInvalidClaim) {
		t.Fatalf("expected ErrInvalidClaim, got %v", err)
	}

	if !reflect.DeepEqual(e.Snapshot(), before) {
		t.Fatal("malformed events must not mutate engine state")
	}
}
