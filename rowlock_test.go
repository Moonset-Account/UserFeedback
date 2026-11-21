package rowlock

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

func mustBegin(t *testing.T, m *Manager, id TxnID) {
	t.Helper()
	if err := m.Begin(id); err != nil {
		t.Fatalf("Begin(%q) failed: %v", id, err)
	}
}

func mustLock(t *testing.T, m *Manager, id TxnID, row string, mode Mode) LockResult {
	t.Helper()
	res, err := m.Lock(id, row, mode)
	if err != nil {
		t.Fatalf("Lock(%q, %q, %v) failed: %v", id, row, mode, err)
	}
	return res
}

func mustRelease(t *testing.T, m *Manager, id TxnID, row string) StepResult {
	t.Helper()
	res, err := m.Release(id, row)
	if err != nil {
		t.Fatalf("Release(%q, %q) failed: %v", id, row, err)
	}
	return res
}

func mustEnd(t *testing.T, m *Manager, id TxnID) StepResult {
	t.Helper()
	res, err := m.End(id)
	if err != nil {
		t.Fatalf("End(%q) failed: %v", id, err)
	}
	return res
}

// holders returns row holders as a txn -> mode map.
func holders(t *testing.T, m *Manager, row string) map[TxnID]Mode {
	t.Helper()
	out := map[TxnID]Mode{}
	for _, snap := range m.Snapshot() {
		if snap.Row != row {
			continue
		}
		for _, h := range snap.Holders {
			out[h.Txn] = h.Mode
		}
	}
	return out
}

// queue returns the row queue as a txn -> mode map, preserving order in q.
func queue(t *testing.T, m *Manager, row string) map[TxnID]Mode {
	t.Helper()
	out := map[TxnID]Mode{}
	for _, q := range rowQueue(t, m, row) {
		out[q.Txn] = q.Mode
	}
	return out
}

func rowQueue(t *testing.T, m *Manager, row string) []QueuedRequest {
	t.Helper()
	for _, snap := range m.Snapshot() {
		if snap.Row == row {
			return snap.Queue
		}
	}
	return nil
}

func assertNoCycles(t *testing.T, m *Manager) {
	t.Helper()
	if cycles := m.Cycles(); len(cycles) != 0 {
		t.Fatalf("expected no deadlock cycles, got %v", cycles)
	}
}

func assertEdges(t *testing.T, m *Manager, want []WaitEdge) {
	t.Helper()
	got := m.WaitFor()
	if len(got) == 0 && len(want) == 0 {
		return
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("wait-for edges mismatch\n got: %v\nwant: %v", got, want)
	}
}

// TestCompatibleSharedRunGrantedTogether covers the acceptable path: a run of
// consecutive compatible readers is admitted in a single grant pass.
func TestCompatibleSharedRunGrantedTogether(t *testing.T) {
	m := NewManager()
	for _, id := range []TxnID{"T1", "T2", "T3"} {
		mustBegin(t, m, id)
	}

	for _, id := range []TxnID{"T1", "T2", "T3"} {
		res := mustLock(t, m, id, "row", Shared)
		if !res.Granted || res.Waiting || res.Aborted {
			t.Fatalf("%s shared request should be granted immediately, got %+v", id, res)
		}
	}

	got := holders(t, m, "row")
	want := map[TxnID]Mode{"T1": Shared, "T2": Shared, "T3": Shared}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("holders = %v, want %v", got, want)
	}
	if q := rowQueue(t, m, "row"); len(q) != 0 {
		t.Fatalf("queue should be empty, got %v", q)
	}

	// A later compatible request against an idle-of-writers row is also granted
	// in one pass; this is the "consecutive compatible readers" rule.
	mustBegin(t, m, "T4")
	if res := mustLock(t, m, "T4", "row", Shared); !res.Granted {
		t.Fatalf("T4 shared should be granted, got %+v", res)
	}
	if len(holders(t, m, "row")) != 4 {
		t.Fatalf("expected 4 shared holders, got %v", holders(t, m, "row"))
	}
	assertNoCycles(t, m)
}

// TestWriterBlocksAndQueueOrderStopsBarging covers the rejected path: a later
// writer may not pass an earlier waiting writer, and readers may not pass a
// queued writer either.
func TestWriterBlocksAndQueueOrderStopsBarging(t *testing.T) {
	m := NewManager()
	for _, id := range []TxnID{"T1", "T2", "T3", "T4"} {
		mustBegin(t, m, id)
	}

	if res := mustLock(t, m, "T1", "row", Exclusive); !res.Granted {
		t.Fatalf("T1 X should be granted on an idle row, got %+v", res)
	}
	if res := mustLock(t, m, "T2", "row", Exclusive); !res.Waiting || res.Granted {
		t.Fatalf("T2 X must wait for T1, got %+v", res)
	}
	if res := mustLock(t, m, "T3", "row", Exclusive); !res.Waiting {
		t.Fatalf("T3 X must wait behind T2, got %+v", res)
	}
	if res := mustLock(t, m, "T4", "row", Shared); !res.Waiting {
		t.Fatalf("T4 S must not barge past queued writers, got %+v", res)
	}

	// T4's direct blockers are the holder T1 and the earlier queued writers.
	assertEdges(t, m, []WaitEdge{
		{Waiter: "T2", Blocker: "T1", Row: "row"},
		{Waiter: "T3", Blocker: "T1", Row: "row"},
		{Waiter: "T3", Blocker: "T2", Row: "row"},
		{Waiter: "T4", Blocker: "T1", Row: "row"},
		{Waiter: "T4", Blocker: "T2", Row: "row"},
		{Waiter: "T4", Blocker: "T3", Row: "row"},
	})
	assertNoCycles(t, m)

	q := rowQueue(t, m, "row")
	wantQ := []QueuedRequest{
		{Txn: "T2", Mode: Exclusive},
		{Txn: "T3", Mode: Exclusive},
		{Txn: "T4", Mode: Shared},
	}
	if !reflect.DeepEqual(q, wantQ) {
		t.Fatalf("queue = %+v, want %+v", q, wantQ)
	}

	// Releasing the holder hands the row to exactly one queued writer.
	step := mustRelease(t, m, "T1", "row")
	wantGrants := []Grant{{Txn: "T2", Row: "row", Mode: Exclusive}}
	if !reflect.DeepEqual(step.Grants, wantGrants) {
		t.Fatalf("release grants = %+v, want %+v", step.Grants, wantGrants)
	}
	if got := holders(t, m, "row"); !reflect.DeepEqual(got, map[TxnID]Mode{"T2": Exclusive}) {
		t.Fatalf("holders after T1 release = %v", got)
	}
	if q := queue(t, m, "row"); !reflect.DeepEqual(q, map[TxnID]Mode{"T3": Exclusive, "T4": Shared}) {
		t.Fatalf("queue after T1 release = %v", q)
	}

	// Ending T2 grants the next writer only; the queued reader still waits.
	step = mustEnd(t, m, "T2")
	if !reflect.DeepEqual(step.Grants, []Grant{{Txn: "T3", Row: "row", Mode: Exclusive}}) {
		t.Fatalf("end grants = %+v", step.Grants)
	}
	if res := mustLock(t, m, "T4", "row", Shared); !res.Waiting {
		t.Fatalf("T4 must keep waiting behind T3's X, got %+v", res)
	}

	// T3 finishing admits the queued reader.
	step = mustEnd(t, m, "T3")
	if !reflect.DeepEqual(step.Grants, []Grant{{Txn: "T4", Row: "row", Mode: Shared}}) {
		t.Fatalf("end grants = %+v", step.Grants)
	}
	if got := holders(t, m, "row"); !reflect.DeepEqual(got, map[TxnID]Mode{"T4": Shared}) {
		t.Fatalf("holders = %v", got)
	}
}

// TestUpgradeKeepsSharedAndIgnoresOwnLock covers the upgrade boundary: the
// upgrading transaction retains its read lock while queued, and its own lock
// never appears as a wait-for blocker.
func TestUpgradeKeepsSharedAndIgnoresOwnLock(t *testing.T) {
	m := NewManager()
	mustBegin(t, m, "T1")
	mustBegin(t, m, "T2")
	mustLock(t, m, "T1", "row", Shared)
	mustLock(t, m, "T2", "row", Shared)

	res := mustLock(t, m, "T1", "row", Exclusive)
	if res.Granted || res.Waiting == false || res.Upgraded {
		t.Fatalf("upgrade must wait without being granted, got %+v", res)
	}
	if got := holders(t, m, "row")["T1"]; got != Shared {
		t.Fatalf("T1 must keep its shared lock during upgrade, got %v", got)
	}
	if q := rowQueue(t, m, "row"); len(q) != 1 || !q[0].Upgrade || q[0].Mode != Exclusive {
		t.Fatalf("expected one pending upgrade request, got %+v", q)
	}

	// Only T1 -> T2 exists: no self-edge from T1's own shared lock.
	assertEdges(t, m, []WaitEdge{{Waiter: "T1", Blocker: "T2", Row: "row"}})
	assertNoCycles(t, m)

	// Once the last other reader leaves, the queued upgrade is granted in place.
	step := mustEnd(t, m, "T2")
	wantGrants := []Grant{{Txn: "T1", Row: "row", Mode: Exclusive, Upgrade: true}}
	if !reflect.DeepEqual(step.Grants, wantGrants) {
		t.Fatalf("end grants = %+v, want %+v", step.Grants, wantGrants)
	}
	if got := holders(t, m, "row")["T1"]; got != Exclusive {
		t.Fatalf("T1 should hold X after upgrade, got %v", got)
	}
	if q := rowQueue(t, m, "row"); len(q) != 0 {
		t.Fatalf("queue should be empty after upgrade, got %+v", q)
	}
	assertNoCycles(t, m)
}

// TestTwoUpgradersDeadlockAbortsLatest covers the interleaved boundary: two
// readers upgrade at once, the later-started one is rolled back, and the older
// one completes its in-place upgrade.
func TestTwoUpgradersDeadlockAbortsLatest(t *testing.T) {
	m := NewManager()
	mustBegin(t, m, "T1")
	mustBegin(t, m, "T2")
	mustLock(t, m, "T1", "row", Shared)
	mustLock(t, m, "T2", "row", Shared)

	mustLock(t, m, "T1", "row", Exclusive) // queued upgrade
	res := mustLock(t, m, "T2", "row", Exclusive)

	if !res.Aborted || res.Granted {
		t.Fatalf("T2 should be the deadlock victim, got %+v", res)
	}
	if len(res.Deadlocks) != 1 {
		t.Fatalf("expected one deadlock round, got %+v", res.Deadlocks)
	}
	ev := res.Deadlocks[0]
	if !reflect.DeepEqual(ev.Cycle, []TxnID{"T1", "T2"}) {
		t.Fatalf("cycle = %v, want [T1 T2]", ev.Cycle)
	}
	if ev.Victim != "T2" {
		t.Fatalf("victim = %s, want T2 (latest started)", ev.Victim)
	}
	wantReleased := []HeldLock{{Txn: "T2", Row: "row", Mode: Shared}}
	if !reflect.DeepEqual(ev.Released, wantReleased) {
		t.Fatalf("released = %+v, want %+v", ev.Released, wantReleased)
	}

	// The victim's lock is gone from every row and it is no longer active.
	if st, _ := m.Status("T2"); st != Aborted {
		t.Fatalf("T2 status = %v, want aborted", st)
	}
	if got := holders(t, m, "row"); !reflect.DeepEqual(got, map[TxnID]Mode{"T1": Exclusive}) {
		t.Fatalf("holders after resolution = %v", got)
	}
	assertNoCycles(t, m)

	// A third reader now waits behind the winner and proceeds once it ends.
	mustBegin(t, m, "T3")
	if res := mustLock(t, m, "T3", "row", Shared); !res.Waiting {
		t.Fatalf("T3 must wait behind T1's X, got %+v", res)
	}
	step := mustEnd(t, m, "T1")
	if !reflect.DeepEqual(step.Grants, []Grant{{Txn: "T3", Row: "row", Mode: Shared}}) {
		t.Fatalf("T3 should be granted after T1 ends, got %+v", step.Grants)
	}

	// Ending an already-aborted transaction is a safe no-op.
	if step := mustEnd(t, m, "T2"); len(step.Grants) != 0 || len(step.Deadlocks) != 0 {
		t.Fatalf("End(aborted) should be a no-op, got %+v", step)
	}
}

// TestThirdReaderQueuedBehindTwoUpgrades is the headline interleaving: two
// readers upgrade, a third reader lines up behind both requests, and the
// manager explains who rolls back, who upgrades, and when the third proceeds.
func TestThirdReaderQueuedBehindTwoUpgrades(t *testing.T) {
	m := NewManager()
	mustBegin(t, m, "T1")
	mustBegin(t, m, "T2")
	mustBegin(t, m, "T3")

	mustLock(t, m, "T1", "row", Shared)
	mustLock(t, m, "T2", "row", Shared)

	if res := mustLock(t, m, "T1", "row", Exclusive); !res.Waiting {
		t.Fatalf("T1 upgrade should wait for T2, got %+v", res)
	}
	// T3 holds nothing yet; it queues behind T1's pending X.
	if res := mustLock(t, m, "T3", "row", Shared); !res.Waiting {
		t.Fatalf("T3 must queue behind the pending upgrade, got %+v", res)
	}

	// T2's matching upgrade closes a three-way cycle. The queue is
	// [T1:X, T3:S, T2:X], so T1 waits on T2's shared lock, T3 waits behind
	// T1's queued X, and T2 waits behind T3's queued S: T3 is on the cycle too
	// because T2 is queued after it. Resolution therefore aborts two
	// transactions: T3 (latest started), then T2, until no cycle remains.
	res := mustLock(t, m, "T2", "row", Exclusive)

	if !res.Aborted {
		t.Fatalf("T2 should be aborted, got %+v", res)
	}
	if len(res.Deadlocks) != 2 {
		t.Fatalf("expected two resolution rounds, got %+v", res.Deadlocks)
	}
	if got := res.Deadlocks[0]; got.Victim != "T3" ||
		!reflect.DeepEqual(got.Cycle, []TxnID{"T1", "T2", "T3"}) ||
		len(got.Released) != 0 {
		t.Fatalf("first round = %+v, want victim T3 on cycle [T1 T2 T3] with no locks released", got)
	}
	if got := res.Deadlocks[1]; got.Victim != "T2" ||
		!reflect.DeepEqual(got.Cycle, []TxnID{"T1", "T2"}) ||
		!reflect.DeepEqual(got.Released, []HeldLock{{Txn: "T2", Row: "row", Mode: Shared}}) {
		t.Fatalf("second round = %+v, want victim T2 on cycle [T1 T2]", got)
	}

	// T1 wins the row with an in-place upgrade; both other readers rolled back.
	if got := holders(t, m, "row"); !reflect.DeepEqual(got, map[TxnID]Mode{"T1": Exclusive}) {
		t.Fatalf("holders = %v, want T1 X", got)
	}
	if !containsGrant(res.Grants, Grant{Txn: "T1", Row: "row", Mode: Exclusive, Upgrade: true}) {
		t.Fatalf("T1's in-place upgrade should be reported, got %+v", res.Grants)
	}
	for _, id := range []TxnID{"T2", "T3"} {
		if st, _ := m.Status(id); st != Aborted {
			t.Fatalf("%s status = %v, want aborted", id, st)
		}
	}
	if q := rowQueue(t, m, "row"); len(q) != 0 {
		t.Fatalf("queue should be drained, got %+v", q)
	}
	assertNoCycles(t, m)

	// The explanation names both rollbacks, the released lock and the upgrade.
	explanation := res.String()
	for _, want := range []string{"T2", "T3", "aborted", "in-place upgrade T1"} {
		if !strings.Contains(explanation, want) {
			t.Fatalf("explanation %q should mention %q", explanation, want)
		}
	}

	// The aborted third reader cannot reuse its id; it restarts as a new
	// transaction and continues only after winner T1 releases the row.
	mustBegin(t, m, "T3b")
	if res := mustLock(t, m, "T3b", "row", Shared); !res.Waiting {
		t.Fatalf("restarted reader should queue behind T1's X, got %+v", res)
	}
	step := mustEnd(t, m, "T1")
	if !reflect.DeepEqual(step.Grants, []Grant{{Txn: "T3b", Row: "row", Mode: Shared}}) {
		t.Fatalf("restarted reader should proceed after T1 ends, got %+v", step.Grants)
	}
}

func containsGrant(grants []Grant, want Grant) bool {
	for _, g := range grants {
		if g == want {
			return true
		}
	}
	return false
}

// TestThreeWayCycleAbortsOnlyLatest covers a plain three-transaction cycle and
// checks that only the latest-started member is rolled back.
func TestThreeWayCycleAbortsOnlyLatest(t *testing.T) {
	m := NewManager()
	for _, id := range []TxnID{"T1", "T2", "T3"} {
		mustBegin(t, m, id)
	}
	mustLock(t, m, "T1", "a", Exclusive)
	mustLock(t, m, "T2", "b", Exclusive)
	mustLock(t, m, "T3", "c", Exclusive)

	if res := mustLock(t, m, "T1", "b", Exclusive); !res.Waiting {
		t.Fatalf("T1 -> b should wait, got %+v", res)
	}
	if res := mustLock(t, m, "T2", "c", Exclusive); !res.Waiting {
		t.Fatalf("T2 -> c should wait, got %+v", res)
	}
	res := mustLock(t, m, "T3", "a", Exclusive)

	if !res.Aborted || len(res.Deadlocks) != 1 || res.Deadlocks[0].Victim != "T3" {
		t.Fatalf("T3 should be the single victim, got %+v", res)
	}
	if got := res.Deadlocks[0].Released; !reflect.DeepEqual(got, []HeldLock{{Txn: "T3", Row: "c", Mode: Exclusive}}) {
		t.Fatalf("released = %+v", got)
	}
	if len(res.Deadlocks[0].Cycle) != 3 {
		t.Fatalf("cycle should contain all three transactions, got %v", res.Deadlocks[0].Cycle)
	}

	// T2's request unblocks; T1 stays queued behind the new holder of "b".
	if got := holders(t, m, "b"); !reflect.DeepEqual(got, map[TxnID]Mode{"T2": Exclusive}) {
		t.Fatalf("holders on b = %v", got)
	}
	// T3's lock on "a" is released; T1's queued request for "a" is then
	// granted, so "a" belongs to T1.
	if got := holders(t, m, "a"); !reflect.DeepEqual(got, map[TxnID]Mode{"T1": Exclusive}) {
		t.Fatalf("T3's lock on a should be released and handed to T1, got %v", got)
	}
	assertNoCycles(t, m)

	if res := mustLock(t, m, "T1", "b", Exclusive); !res.Waiting {
		t.Fatalf("T1 must keep waiting for T2's lock on b, got %+v", res)
	}
	step := mustEnd(t, m, "T2")
	if !containsGrant(step.Grants, Grant{Txn: "T1", Row: "b", Mode: Exclusive}) {
		t.Fatalf("T1 should acquire b when T2 ends, got %+v", step.Grants)
	}
}

// TestEndReleasesEveryRow checks that transaction end releases all rows and
// re-grants queued requests across rows in deterministic row order.
func TestEndReleasesEveryRow(t *testing.T) {
	m := NewManager()
	mustBegin(t, m, "T1")
	mustBegin(t, m, "T2")
	mustLock(t, m, "T1", "b", Exclusive)
	mustLock(t, m, "T1", "a", Shared)
	mustLock(t, m, "T2", "a", Shared)
	mustLock(t, m, "T2", "b", Exclusive) // waits on T1

	step := mustEnd(t, m, "T1")
	if !reflect.DeepEqual(step.Grants, []Grant{{Txn: "T2", Row: "b", Mode: Exclusive}}) {
		t.Fatalf("grants = %+v, want T2 on b", step.Grants)
	}
	// Ending T1 releases both rows; T2's queued request on "a" is granted.
	if got := holders(t, m, "a"); !reflect.DeepEqual(got, map[TxnID]Mode{"T2": Shared}) {
		t.Fatalf("T1's lock on a should be released and handed to T2, got %v", got)
	}
	if st, _ := m.Status("T1"); st != Committed {
		t.Fatalf("T1 status = %v, want committed", st)
	}
}

// TestReadsPoolAfterWritersDrain covers the boundary between the two queue
// rules: while a writer waits nothing passes it, but once writers drain a run
// of consecutive compatible readers is granted together in one pass.
func TestReadsPoolAfterWritersDrain(t *testing.T) {
	m := NewManager()
	for _, id := range []TxnID{"W", "R1", "R2", "R3"} {
		mustBegin(t, m, id)
	}
	mustLock(t, m, "W", "row", Exclusive)
	mustLock(t, m, "R1", "row", Shared) // waits behind W
	mustLock(t, m, "R2", "row", Shared) // waits behind W and R1
	mustLock(t, m, "R3", "row", Shared) // waits behind W, R1, R2

	step := mustEnd(t, m, "W")
	want := []Grant{
		{Txn: "R1", Row: "row", Mode: Shared},
		{Txn: "R2", Row: "row", Mode: Shared},
		{Txn: "R3", Row: "row", Mode: Shared},
	}
	if !reflect.DeepEqual(step.Grants, want) {
		t.Fatalf("draining writer should admit the whole reader run at once, got %+v", step.Grants)
	}
	if got := holders(t, m, "row"); len(got) != 3 {
		t.Fatalf("expected three readers admitted, got %v", got)
	}
	assertNoCycles(t, m)
}

// TestQueuedUpgradeBlocksLateReaderWithoutCycle checks that a pending upgrade
// blocks a later reader on queue order alone, and that ordering creates no
// false cycle when no request leads back to the upgrade owner.
func TestQueuedUpgradeBlocksLateReaderWithoutCycle(t *testing.T) {
	m := NewManager()
	for _, id := range []TxnID{"A", "B", "C"} {
		mustBegin(t, m, id)
	}
	mustLock(t, m, "A", "row", Shared)
	mustLock(t, m, "B", "row", Shared)
	if res := mustLock(t, m, "A", "row", Exclusive); !res.Waiting {
		t.Fatalf("A upgrade should wait for B, got %+v", res)
	}
	res := mustLock(t, m, "C", "row", Shared)
	if res.Granted || !res.Waiting {
		t.Fatalf("C must not barge past A's pending upgrade, got %+v", res)
	}
	// C waits on A (earlier queued X). It does not wait on B: B's shared lock
	// is compatible with C's shared request, and only incompatible holders
	// contribute a direct-blocking edge.
	assertEdges(t, m, []WaitEdge{
		{Waiter: "A", Blocker: "B", Row: "row"},
		{Waiter: "C", Blocker: "A", Row: "row"},
	})
	assertNoCycles(t, m)

	// B leaving lets A upgrade; only then does the queued reader get the row.
	step := mustEnd(t, m, "B")
	if !reflect.DeepEqual(step.Grants, []Grant{{Txn: "A", Row: "row", Mode: Exclusive, Upgrade: true}}) {
		t.Fatalf("A's upgrade should be granted first, got %+v", step.Grants)
	}
	if got := queue(t, m, "row"); !reflect.DeepEqual(got, map[TxnID]Mode{"C": Shared}) {
		t.Fatalf("C should still be queued behind A's X, got %v", got)
	}
	step = mustEnd(t, m, "A")
	if !reflect.DeepEqual(step.Grants, []Grant{{Txn: "C", Row: "row", Mode: Shared}}) {
		t.Fatalf("C should be granted after A ends, got %+v", step.Grants)
	}
}

// TestCascadingCyclesResolvedUntilClear checks that resolution repeats after
// each abort until the graph is acyclic. One request can close two cycles at
// once, and each victim's released locks must let the next round proceed.
func TestCascadingCyclesResolvedUntilClear(t *testing.T) {
	m := NewManager()
	for _, id := range []TxnID{"T1", "T2", "T3"} {
		mustBegin(t, m, id)
	}
	// T1 holds d; T2 and T3 both hold x shared and both queue for d.
	mustLock(t, m, "T1", "d", Exclusive)
	mustLock(t, m, "T2", "x", Shared)
	mustLock(t, m, "T3", "x", Shared)
	mustLock(t, m, "T2", "d", Exclusive) // waits on T1
	mustLock(t, m, "T3", "d", Exclusive) // waits on T1

	// T1's X on x is blocked by both readers, so this single request closes a
	// cycle through T2 and T3. The three transactions form one strongly
	// connected component, so the latest (T3) is aborted first; that leaves the
	// T1/T2 cycle, which is then resolved by aborting T2.
	res := mustLock(t, m, "T1", "x", Exclusive)
	if res.Aborted {
		t.Fatalf("T1 started first and must survive, got %+v", res)
	}
	if len(res.Deadlocks) != 2 {
		t.Fatalf("expected two resolution rounds, got %+v", res.Deadlocks)
	}
	first, second := res.Deadlocks[0], res.Deadlocks[1]
	if first.Victim != "T3" || !reflect.DeepEqual(first.Cycle, []TxnID{"T1", "T2", "T3"}) {
		t.Fatalf("first round = %+v, want victim T3 in cycle [T1 T2 T3]", first)
	}
	if second.Victim != "T2" || !reflect.DeepEqual(second.Cycle, []TxnID{"T1", "T2"}) {
		t.Fatalf("second round = %+v, want victim T2 in cycle [T1 T2]", second)
	}
	assertNoCycles(t, m)

	// Both readers rolled back, so T1 owns d and finally x.
	if got := holders(t, m, "d"); !reflect.DeepEqual(got, map[TxnID]Mode{"T1": Exclusive}) {
		t.Fatalf("T1 should own d, got %v", got)
	}
	if got := holders(t, m, "x"); !reflect.DeepEqual(got, map[TxnID]Mode{"T1": Exclusive}) {
		t.Fatalf("T1 should own x after both readers abort, got %v", got)
	}
	if got := m.WaitFor(); len(got) != 0 {
		t.Fatalf("no waits should remain, got %v", got)
	}
	for _, id := range []TxnID{"T2", "T3"} {
		if st, _ := m.Status(id); st != Aborted {
			t.Fatalf("%s status = %v, want aborted", id, st)
		}
	}
}

// TestErrors covers rejected inputs and lifecycle misuse.
func TestErrors(t *testing.T) {
	m := NewManager()
	if err := m.Begin(""); !errors.Is(err, ErrEmptyTxnID) {
		t.Fatalf("empty id error = %v, want ErrEmptyTxnID", err)
	}
	mustBegin(t, m, "T1")
	if err := m.Begin("T1"); !errors.Is(err, ErrDuplicateTxn) {
		t.Fatalf("duplicate id error = %v, want ErrDuplicateTxn", err)
	}
	if _, err := m.Lock("T9", "row", Shared); !errors.Is(err, ErrUnknownTxn) {
		t.Fatalf("unknown txn error = %v, want ErrUnknownTxn", err)
	}
	if _, err := m.Lock("T1", "row", Mode(99)); !errors.Is(err, ErrInvalidMode) {
		t.Fatalf("invalid mode error = %v, want ErrInvalidMode", err)
	}
	mustLock(t, m, "T1", "row", Shared)
	mustBegin(t, m, "T2")
	mustLock(t, m, "T2", "row", Shared)
	mustLock(t, m, "T1", "row", Exclusive) // queued upgrade

	if _, err := m.Release("T2", "missing"); !errors.Is(err, ErrNotHeld) {
		t.Fatalf("release missing row error = %v, want ErrNotHeld", err)
	}

	// Asking for S while holding S is a no-op success, not a downgrade.
	if res := mustLock(t, m, "T1", "row", Shared); !res.Granted || res.Waiting {
		t.Fatalf("re-requesting a held S should be granted, got %+v", res)
	}

	// A transaction that holds nothing but has a pending X may not quietly
	// weaken it to S by re-requesting.
	mustBegin(t, m, "T4")
	mustLock(t, m, "T4", "other", Exclusive)
	mustBegin(t, m, "T5")
	if res := mustLock(t, m, "T5", "other", Exclusive); !res.Waiting {
		t.Fatalf("T5 should wait for T4, got %+v", res)
	}
	if _, err := m.Lock("T5", "other", Shared); !errors.Is(err, ErrCannotDowngrade) {
		t.Fatalf("downgrade error = %v, want ErrCannotDowngrade", err)
	}

	// Releasing a pending-only request cancels it without error.
	mustEnd(t, m, "T2")
	if _, err := m.Release("T1", "row"); err != nil {
		t.Fatalf("release should drop the pending upgrade: %v", err)
	}
	if q := rowQueue(t, m, "row"); len(q) != 0 {
		t.Fatalf("queue should be empty after release, got %+v", q)
	}

	mustBegin(t, m, "T3")
	mustEnd(t, m, "T3")
	if _, err := m.Lock("T3", "row", Shared); !errors.Is(err, ErrTxnNotActive) {
		t.Fatalf("lock after commit error = %v, want ErrTxnNotActive", err)
	}

	// Startup order is unique and strictly increasing.
	o1, _ := m.StartOrder("T1")
	o2, _ := m.StartOrder("T2")
	o3, _ := m.StartOrder("T3")
	if !(o1 < o2 && o2 < o3) {
		t.Fatalf("startup order not increasing: %d %d %d", o1, o2, o3)
	}
	if _, err := m.StartOrder("nope"); !errors.Is(err, ErrUnknownTxn) {
		t.Fatalf("StartOrder(unknown) error = %v", err)
	}
}
