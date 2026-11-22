package budget_test

import (
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"requestbudget"
)

// fakeClock is a manually advanced clock. Its methods are safe for use from
// concurrent tests.
type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func newFakeClock(start time.Time) *fakeClock {
	return &fakeClock{now: start}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

var baseTime = time.Date(2026, 11, 23, 10, 0, 0, 0, time.UTC)

const testDeadline = 10 * time.Second

func mustRoot(t *testing.T, limit int64, clock budget.Clock) *budget.Root {
	t.Helper()
	r, err := budget.NewRoot("root-1", limit, baseTime.Add(testDeadline), clock)
	if err != nil {
		t.Fatalf("NewRoot: %v", err)
	}
	return r
}

func mustReserveRoot(t *testing.T, r *budget.Root, amount int64) *budget.Reservation {
	t.Helper()
	n, err := r.Reserve(amount)
	if err != nil {
		t.Fatalf("root.Reserve(%d): %v", amount, err)
	}
	return n
}

func mustReserveChild(t *testing.T, parent *budget.Reservation, amount int64) *budget.Reservation {
	t.Helper()
	n, err := parent.Reserve(amount)
	if err != nil {
		t.Fatalf("%s.Reserve(%d): %v", parent.ReserveID(), amount, err)
	}
	return n
}

func mustSettle(t *testing.T, n *budget.Reservation, usage int64) budget.Settlement {
	t.Helper()
	s, err := n.Settle(usage)
	if err != nil {
		t.Fatalf("%s.Settle(%d): %v", n.ReserveID(), usage, err)
	}
	return s
}

func assertErrIs(t *testing.T, err error, want error) {
	t.Helper()
	if !errors.Is(err, want) {
		t.Fatalf("got error %v, want errors.Is(..., %v)", err, want)
	}
}

// assertAuditInvariants checks the documented per-node and root accounting
// identities on a full snapshot.
func assertAuditInvariants(t *testing.T, a budget.Audit) {
	t.Helper()
	if got, want := a.Charged+a.Held+a.Available, a.Limit; got != want {
		t.Fatalf("root invariant broken: charged(%d)+held(%d)+available(%d)=%d, limit=%d",
			a.Charged, a.Held, a.Available, got, want)
	}
	for _, n := range a.Branches {
		assertNodeInvariants(t, n)
	}
}

func assertNodeInvariants(t *testing.T, n budget.NodeAudit) {
	t.Helper()
	switch n.State {
	case budget.StateActive, budget.StateUnsettled:
		if got, want := n.Charged+n.Held+n.Available, n.Limit; got != want {
			t.Fatalf("%s (%s): charged(%d)+held(%d)+available(%d)=%d, limit=%d",
				n.ReserveID, n.State, n.Charged, n.Held, n.Available, got, n.Limit)
		}
		if n.Refunded != 0 {
			t.Fatalf("%s (%s): open node refunded %d", n.ReserveID, n.State, n.Refunded)
		}
	case budget.StateSettled, budget.StateCancelled:
		if got, want := n.Charged+n.Refunded, n.Limit; got != want {
			t.Fatalf("%s (%s): charged(%d)+refunded(%d)=%d, limit=%d",
				n.ReserveID, n.State, n.Charged, n.Refunded, got, n.Limit)
		}
		if n.Held != 0 || n.Available != 0 {
			t.Fatalf("%s (%s): closed node held=%d available=%d, want 0/0",
				n.ReserveID, n.State, n.Held, n.Available)
		}
	}
	for _, c := range n.Children {
		assertNodeInvariants(t, c)
	}
}

// TestNewRootValidation covers the configuration boundary.
func TestNewRootValidation(t *testing.T) {
	cases := []struct {
		name     string
		id       string
		limit    int64
		deadline time.Time
	}{
		{"empty id", "", 10, baseTime.Add(time.Second)},
		{"negative limit", "root", -1, baseTime.Add(time.Second)},
		{"zero deadline", "root", 10, time.Time{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := budget.NewRoot(tc.id, tc.limit, tc.deadline, nil)
			assertErrIs(t, err, budget.ErrInvalidConfiguration)
		})
	}

	// Zero quota is a valid configuration, but nothing can be reserved.
	r, err := budget.NewRoot("zero", 0, baseTime.Add(time.Second), newFakeClock(baseTime).Now)
	if err != nil {
		t.Fatalf("NewRoot with zero limit: %v", err)
	}
	_, err = r.Reserve(1)
	assertErrIs(t, err, budget.ErrInsufficientBudget)
}

// TestReserveBounds covers positive and boundary reservation amounts.
func TestReserveBounds(t *testing.T) {
	clock := newFakeClock(baseTime)
	r := mustRoot(t, 100, clock.Now)

	_, err := r.Reserve(0)
	assertErrIs(t, err, budget.ErrInvalidAmount)
	_, err = r.Reserve(-5)
	assertErrIs(t, err, budget.ErrInvalidAmount)
	_, err = r.Reserve(101)
	assertErrIs(t, err, budget.ErrInsufficientBudget)

	branch := mustReserveRoot(t, r, 100)
	if got := r.Available(); got != 0 {
		t.Fatalf("root available after full reserve = %d, want 0", got)
	}
	_, err = branch.Reserve(0)
	assertErrIs(t, err, budget.ErrInvalidAmount)
	_, err = branch.Reserve(101)
	assertErrIs(t, err, budget.ErrInsufficientBudget)
	// Exactly the remaining quota must succeed.
	child := mustReserveChild(t, branch, 100)
	if child.Limit() != 100 {
		t.Fatalf("child limit = %d, want 100", child.Limit())
	}
	if got := branch.Available(); got != 0 {
		t.Fatalf("branch available = %d, want 0", got)
	}
}

// TestSettleAndRefund covers the normal settle path, including a zero-usage
// settlement that refunds the whole reservation.
func TestSettleAndRefund(t *testing.T) {
	clock := newFakeClock(baseTime)
	r := mustRoot(t, 100, clock.Now)

	full := mustReserveRoot(t, r, 30)
	s := mustSettle(t, full, 0)
	if s.Refund != 30 {
		t.Fatalf("zero-usage refund = %d, want 30", s.Refund)
	}
	if s.ReservationID != full.ReserveID() {
		t.Fatalf("settlement reservation id = %q, want %q", s.ReservationID, full.ReserveID())
	}
	if s.ID == "" {
		t.Fatal("settlement id is empty")
	}
	if full.State() != budget.StateSettled {
		t.Fatalf("state = %s, want settled", full.State())
	}
	if got := r.Available(); got != 100 {
		t.Fatalf("root available after full refund = %d, want 100", got)
	}

	part := mustReserveRoot(t, r, 40)
	s = mustSettle(t, part, 25)
	if s.Usage != 25 || s.Refund != 15 {
		t.Fatalf("settlement = %+v, want usage 25 refund 15", s)
	}
	if got := r.Charged(); got != 25 {
		t.Fatalf("root charged = %d, want 25", got)
	}
	if got := r.Available(); got != 75 {
		t.Fatalf("root available = %d, want 75", got)
	}
	assertAuditInvariants(t, r.Audit())
}

// TestChildCannotExceedParentRemaining checks reallocation is limited to the
// parent's own not-yet-committed quota, across multiple children.
func TestChildCannotExceedParentRemaining(t *testing.T) {
	r := mustRoot(t, 100, newFakeClock(baseTime).Now)
	branch := mustReserveRoot(t, r, 50)

	c1 := mustReserveChild(t, branch, 20)
	_, err := branch.Reserve(31)
	assertErrIs(t, err, budget.ErrInsufficientBudget)
	c2 := mustReserveChild(t, branch, 30)
	if got := branch.Available(); got != 0 {
		t.Fatalf("branch available = %d, want 0", got)
	}
	_, err = branch.Reserve(1)
	assertErrIs(t, err, budget.ErrInsufficientBudget)

	// Settling a child returns only its unused quota, not its whole limit.
	mustSettle(t, c1, 5)
	if got := branch.Available(); got != 15 {
		t.Fatalf("branch available after child settle = %d, want 15", got)
	}
	_, err = branch.Reserve(16)
	assertErrIs(t, err, budget.ErrInsufficientBudget)
	// c2 still holds its full 30: only c1's refund is spendable.
	if got := c2.Limit(); got != 30 {
		t.Fatalf("c2 limit = %d, want 30", got)
	}
	mustReserveChild(t, branch, 15)

	// Root still holds the whole branch.
	if got := r.Held(); got != 50 {
		t.Fatalf("root held = %d, want 50", got)
	}
	assertAuditInvariants(t, r.Audit())
}

// TestSettleBlockedByOpenChildren requires a parent to wait until every child
// is settled or cancelled before it may settle.
func TestSettleBlockedByOpenChildren(t *testing.T) {
	r := mustRoot(t, 100, newFakeClock(baseTime).Now)
	branch := mustReserveRoot(t, r, 60)
	done := mustReserveChild(t, branch, 20)
	pending := mustReserveChild(t, branch, 15)

	mustSettle(t, done, 12)

	_, err := branch.Settle(10)
	assertErrIs(t, err, budget.ErrChildrenUnsettled)

	// A timed-out child is still open and still blocks the parent.
	if err := pending.Timeout(); err != nil {
		t.Fatalf("pending.Timeout: %v", err)
	}
	_, err = branch.Settle(10)
	assertErrIs(t, err, budget.ErrChildrenUnsettled)

	// Cannot cancel the parent either while a child is unsettled.
	_, err = branch.Cancel()
	assertErrIs(t, err, budget.ErrChildrenOutstanding)

	mustSettle(t, pending, 3)

	s, err := branch.Settle(10)
	if err != nil {
		t.Fatalf("branch.Settle after children resolved: %v", err)
	}
	// charged children = 12 + 3 = 15; own usage 10; refund = 60 - 25.
	if s.Refund != 35 {
		t.Fatalf("branch refund = %d, want 35", s.Refund)
	}
	if got := r.Charged(); got != 25 {
		t.Fatalf("root charged = %d, want 25", got)
	}
	assertAuditInvariants(t, r.Audit())
}

// TestSettleUsageExceedsReservation checks the usage boundary of a parent and
// of a leaf.
func TestSettleUsageExceedsReservation(t *testing.T) {
	r := mustRoot(t, 100, newFakeClock(baseTime).Now)
	branch := mustReserveRoot(t, r, 20)
	child := mustReserveChild(t, branch, 15)

	_, err := child.Settle(16)
	assertErrIs(t, err, budget.ErrUsageExceedsReservation)
	mustSettle(t, child, 15)

	_, err = branch.Settle(6)
	assertErrIs(t, err, budget.ErrUsageExceedsReservation)
	_, err = branch.Settle(-1)
	assertErrIs(t, err, budget.ErrInvalidAmount)

	s := mustSettle(t, branch, 5)
	if s.Refund != 0 {
		t.Fatalf("refund = %d, want 0", s.Refund)
	}
	if got := r.Charged(); got != 20 {
		t.Fatalf("root charged = %d, want 20", got)
	}
	assertAuditInvariants(t, r.Audit())
}

// TestTimeoutKeepsQuotaHeldUntilValidSettlement covers the required recovery
// path: a timed-out branch reports unsettled, its quota stays held, and only a
// valid actual usage returns the balance.
func TestTimeoutKeepsQuotaHeldUntilValidSettlement(t *testing.T) {
	clock := newFakeClock(baseTime)
	r := mustRoot(t, 100, clock.Now)

	branch := mustReserveRoot(t, r, 50)
	if err := branch.Timeout(); err != nil {
		t.Fatalf("Timeout: %v", err)
	}
	if branch.State() != budget.StateUnsettled {
		t.Fatalf("state = %s, want unsettled", branch.State())
	}
	if got := r.Held(); got != 50 {
		t.Fatalf("held after timeout = %d, want 50", got)
	}
	if got := r.Available(); got != 50 {
		t.Fatalf("root available after timeout = %d, want 50", got)
	}

	// No new actions from an unsettled branch, including retries.
	_, err := branch.Reserve(1)
	assertErrIs(t, err, budget.ErrInvalidState)
	_, err = branch.Retry()
	assertErrIs(t, err, budget.ErrInvalidState)
	err = branch.Timeout()
	assertErrIs(t, err, budget.ErrInvalidState)

	// A usage larger than the reservation is not a valid settlement.
	_, err = branch.Settle(51)
	assertErrIs(t, err, budget.ErrUsageExceedsReservation)
	if branch.State() != budget.StateUnsettled {
		t.Fatalf("state after rejected settle = %s, want unsettled", branch.State())
	}

	s := mustSettle(t, branch, 20)
	if s.Refund != 30 {
		t.Fatalf("refund = %d, want 30", s.Refund)
	}
	if got := r.Charged(); got != 20 {
		t.Fatalf("root charged = %d, want 20", got)
	}
	if got := r.Available(); got != 80 {
		t.Fatalf("root available = %d, want 80", got)
	}
	assertAuditInvariants(t, r.Audit())
}

// TestLateSettlementAppliedOnce checks duplicate and late settlements have no
// second effect.
func TestLateSettlementAppliedOnce(t *testing.T) {
	clock := newFakeClock(baseTime)
	r := mustRoot(t, 100, clock.Now)
	branch := mustReserveRoot(t, r, 40)

	first := mustSettle(t, branch, 10)
	clock.Advance(20 * time.Second) // well past the deadline

	_, err := branch.Settle(40)
	assertErrIs(t, err, budget.ErrAlreadySettled)
	_, err = branch.Settle(0)
	assertErrIs(t, err, budget.ErrAlreadySettled)

	gotS, ok := branch.Settlement()
	if !ok || gotS != first {
		t.Fatalf("stored settlement = %+v (ok=%v), want the first one %+v", gotS, ok, first)
	}
	if got := r.Charged(); got != 10 {
		t.Fatalf("root charged = %d, want 10", got)
	}
	if got := r.Available(); got != 90 {
		t.Fatalf("root available = %d, want 90", got)
	}
	assertAuditInvariants(t, r.Audit())
}

// TestCancelDoesNotBorrowSiblingReservation checks cancellation refunds only
// the cancelled branch and leaves sibling holds (and their spendable quota)
// untouched.
func TestCancelDoesNotBorrowSiblingReservation(t *testing.T) {
	r := mustRoot(t, 100, newFakeClock(baseTime).Now)

	a := mustReserveRoot(t, r, 30)
	b := mustReserveRoot(t, r, 30)
	if got := r.Available(); got != 40 {
		t.Fatalf("root available = %d, want 40", got)
	}

	refund, err := a.Cancel()
	if err != nil {
		t.Fatalf("a.Cancel: %v", err)
	}
	if refund != 30 {
		t.Fatalf("cancel refund = %d, want 30", refund)
	}
	if got := r.Available(); got != 70 {
		t.Fatalf("root available after cancel = %d, want 70", got)
	}
	if got := b.Available(); got != 30 {
		t.Fatalf("sibling available changed to %d, want 30", got)
	}

	// The sibling can still allocate from its own reservation.
	b1 := mustReserveChild(t, b, 30)
	if b1.Limit() != 30 {
		t.Fatalf("b1 limit = %d, want 30", b1.Limit())
	}

	// The new root budget is exactly 70; 71 must not fit.
	_, err = r.Reserve(71)
	assertErrIs(t, err, budget.ErrInsufficientBudget)
	c := mustReserveRoot(t, r, 70)

	// A second cancel on the same reservation is rejected and must not double
	// refund.
	_, err = a.Cancel()
	assertErrIs(t, err, budget.ErrInvalidState)

	mustSettle(t, b1, 30)
	mustSettle(t, b, 0) // own usage 0; children already charged 30
	mustSettle(t, c, 70)
	assertAuditInvariants(t, r.Audit())
	if got := r.Available(); got != 0 {
		t.Fatalf("root available = %d, want 0", got)
	}
}

// TestCancelBlockedByOutstandingChildren checks a cancelled branch cannot
// strand or steal child reservations.
func TestCancelBlockedByOutstandingChildren(t *testing.T) {
	r := mustRoot(t, 100, newFakeClock(baseTime).Now)
	branch := mustReserveRoot(t, r, 40)
	child := mustReserveChild(t, branch, 10)

	_, err := branch.Cancel()
	assertErrIs(t, err, budget.ErrChildrenOutstanding)
	if branch.State() != budget.StateActive {
		t.Fatalf("branch state = %s, want active", branch.State())
	}
	if got := branch.Available(); got != 30 {
		t.Fatalf("branch available = %d, want 30", got)
	}

	// Child timeout still counts as outstanding.
	if err := child.Timeout(); err != nil {
		t.Fatalf("child.Timeout: %v", err)
	}
	_, err = branch.Cancel()
	assertErrIs(t, err, budget.ErrChildrenOutstanding)

	mustSettle(t, child, 4)
	refund, err := branch.Cancel()
	if err != nil {
		t.Fatalf("branch.Cancel after child settled: %v", err)
	}
	if refund != 36 {
		t.Fatalf("branch cancel refund = %d, want 36", refund)
	}
	if got := r.Charged(); got != 4 {
		t.Fatalf("root charged = %d, want 4", got)
	}
	assertAuditInvariants(t, r.Audit())
}

// TestDeadlineRejectsNewActionsButReconcilesExisting covers the absolute cutoff:
// every new action at or after the deadline is rejected, while reconciliation
// of already consumed quota keeps working.
func TestDeadlineRejectsNewActionsButReconcilesExisting(t *testing.T) {
	clock := newFakeClock(baseTime)
	r := mustRoot(t, 100, clock.Now)
	branch := mustReserveRoot(t, r, 40)
	child := mustReserveChild(t, branch, 15)
	pre := mustReserveRoot(t, r, 10) // reserved before the deadline, closed after

	// One nanosecond before the deadline a new action is still allowed.
	clock.Advance(testDeadline - time.Nanosecond)
	if _, err := branch.Retry(); err != nil {
		t.Fatalf("Retry just before deadline: %v", err)
	}
	probe, err := r.Reserve(5)
	if err != nil {
		t.Fatalf("Reserve just before deadline: %v", err)
	}
	if _, err := probe.Cancel(); err != nil {
		t.Fatalf("probe.Cancel: %v", err)
	}

	clock.Advance(time.Nanosecond) // exactly at the deadline
	_, err = r.Reserve(1)
	assertErrIs(t, err, budget.ErrDeadlineExceeded)
	_, err = branch.Reserve(1)
	assertErrIs(t, err, budget.ErrDeadlineExceeded)
	_, err = child.Reserve(1)
	assertErrIs(t, err, budget.ErrDeadlineExceeded)
	_, err = branch.Retry()
	assertErrIs(t, err, budget.ErrDeadlineExceeded)
	if _, err := r.Reserve(0); !errors.Is(err, budget.ErrDeadlineExceeded) {
		t.Fatalf("deadline must be checked before amount, got %v", err)
	}

	// Reconciliation still works after the deadline.
	if err := child.Timeout(); err != nil {
		t.Fatalf("child.Timeout after deadline: %v", err)
	}
	mustSettle(t, child, 10)
	s := mustSettle(t, branch, 5) // own 5 + child 10 = 15 charged of 40
	if s.Refund != 25 {
		t.Fatalf("refund after deadline = %d, want 25", s.Refund)
	}
	if got := r.Charged(); got != 15 {
		t.Fatalf("root charged = %d, want 15", got)
	}
	if got := r.Available(); got != 75 { // 100 - 15 charged - 10 held by pre
		t.Fatalf("root available = %d, want 75", got)
	}
	assertAuditInvariants(t, r.Audit())

	// A branch reserved before the deadline can still be cancelled after it.
	if refund, err := pre.Cancel(); err != nil || refund != 10 {
		t.Fatalf("Cancel after deadline = (%d, %v), want (10, nil)", refund, err)
	}
	if got := r.Available(); got != 85 {
		t.Fatalf("root available after late cancel = %d, want 85", got)
	}
	assertAuditInvariants(t, r.Audit())
}

// TestIdentifiersAreIndependent checks that reservation, retry and settlement
// identifiers are distinct and stable.
func TestIdentifiersAreIndependent(t *testing.T) {
	r := mustRoot(t, 100, newFakeClock(baseTime).Now)
	a := mustReserveRoot(t, r, 10)
	b := mustReserveRoot(t, r, 10)

	seen := map[string]string{}
	add := func(kind, id string) {
		t.Helper()
		if id == "" {
			t.Fatalf("%s id is empty", kind)
		}
		if prev, ok := seen[id]; ok {
			t.Fatalf("%s id %q collides with %s", kind, id, prev)
		}
		seen[id] = kind
	}
	add("reservation", a.ReserveID())
	add("reservation", b.ReserveID())

	attemptsA := a.Attempts()
	if len(attemptsA) != 1 || attemptsA[0].Seq != 1 {
		t.Fatalf("initial attempts = %+v, want one seq 1", attemptsA)
	}
	tr, err := a.Retry()
	if err != nil {
		t.Fatalf("Retry: %v", err)
	}
	if tr.Seq != 2 || tr.ReservationID != a.ReserveID() {
		t.Fatalf("retry attempt = %+v", tr)
	}
	add("attempt", attemptsA[0].ID)
	add("attempt", tr.ID)
	if tr.ID == attemptsA[0].ID {
		t.Fatal("retry reused the initial attempt id")
	}
	if got := len(a.Attempts()); got != 2 {
		t.Fatalf("attempt count = %d, want 2", got)
	}

	sa := mustSettle(t, a, 1)
	sb := mustSettle(t, b, 2)
	add("settlement", sa.ID)
	add("settlement", sb.ID)

	// Ids are per-root monotonic; a fresh root restarts numbering but the
	// reservation objects stay distinct.
	r2 := mustRoot(t, 5, newFakeClock(baseTime).Now)
	c := mustReserveRoot(t, r2, 5)
	if c.ReserveID() == a.ReserveID() {
		// Same identifier space across roots is acceptable only if the
		// reservations are still independent objects with separate budgets.
		if got := c.Limit(); got != 5 {
			t.Fatalf("cross-root reservation limit = %d, want 5", got)
		}
	}
}

// TestConcurrentChildrenConserveQuota hammers one reservation from many
// goroutines; the sum of granted reservations may never exceed its limit.
func TestConcurrentChildrenConserveQuota(t *testing.T) {
	const limit = 1000
	const chunk = 20
	const workers = 200

	fixed := baseTime
	r, err := budget.NewRoot("root-concurrent", limit, baseTime.Add(time.Hour), func() time.Time { return fixed })
	if err != nil {
		t.Fatalf("NewRoot: %v", err)
	}
	branch := mustReserveRoot(t, r, limit)

	var wg sync.WaitGroup
	var granted int64
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := branch.Reserve(chunk); err == nil {
				atomic.AddInt64(&granted, chunk)
			} else if !errors.Is(err, budget.ErrInsufficientBudget) {
				t.Errorf("unexpected reserve error: %v", err)
			}
		}()
	}
	wg.Wait()

	if granted != limit {
		t.Fatalf("granted %d of %d quota, want exact limit", granted, limit)
	}
	if got := branch.Available(); got != 0 {
		t.Fatalf("branch available = %d, want 0", got)
	}
	assertAuditInvariants(t, r.Audit())
}

// TestConcurrentSettleAppliesOnce races settlements of the same reservation
// and checks exactly one is accepted.
func TestConcurrentSettleAppliesOnce(t *testing.T) {
	fixed := baseTime
	r, err := budget.NewRoot("root-race-settle", 100, baseTime.Add(time.Hour), func() time.Time { return fixed })
	if err != nil {
		t.Fatalf("NewRoot: %v", err)
	}
	branch := mustReserveRoot(t, r, 100)

	const workers = 50
	var wg sync.WaitGroup
	var accepted int64
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := branch.Settle(10); err == nil {
				atomic.AddInt64(&accepted, 1)
			} else if !errors.Is(err, budget.ErrAlreadySettled) {
				t.Errorf("unexpected settle error: %v", err)
			}
		}()
	}
	wg.Wait()

	if accepted != 1 {
		t.Fatalf("accepted settlements = %d, want exactly 1", accepted)
	}
	if got := r.Charged(); got != 10 {
		t.Fatalf("root charged = %d, want 10", got)
	}
	if got := r.Available(); got != 90 {
		t.Fatalf("root available = %d, want 90", got)
	}
	assertAuditInvariants(t, r.Audit())
}

// TestInterleavedNestedBranchesWithPartialSettlement is the required scenario:
// two nested branch trees finish interleaved, one branch has only received a
// partial settlement (a child timed out and is still unsettled), and the test
// then verifies which next call may be issued and that the whole tree
// conserves quota exactly.
func TestInterleavedNestedBranchesWithPartialSettlement(t *testing.T) {
	clock := newFakeClock(baseTime)
	r := mustRoot(t, 100, clock.Now)

	// Branch A: limit 40, children A1 (20) and A2 (10).
	a := mustReserveRoot(t, r, 40)
	a1 := mustReserveChild(t, a, 20)
	a2 := mustReserveChild(t, a, 10)

	// Branch B: limit 50, child B1 (30).
	b := mustReserveRoot(t, r, 50)
	b1 := mustReserveChild(t, b, 30)

	if got := r.Available(); got != 10 {
		t.Fatalf("initial root available = %d, want 10", got)
	}

	// (1) A1 times out: reported unsettled, its 20 stay held.
	if err := a1.Timeout(); err != nil {
		t.Fatalf("a1.Timeout: %v", err)
	}
	if a1.State() != budget.StateUnsettled {
		t.Fatalf("a1 state = %s, want unsettled", a1.State())
	}
	if got := a.Available(); got != 10 {
		t.Fatalf("a available after a1 timeout = %d, want 10", got)
	}

	// (2) B1 settles first and refunds 18 to B: interleaved branch order.
	sb1 := mustSettle(t, b1, 12)
	if sb1.Refund != 18 {
		t.Fatalf("b1 refund = %d, want 18", sb1.Refund)
	}
	// B itself is still open: 12 charged, 38 still spendable by B.
	if got := b.Available(); got != 38 {
		t.Fatalf("b available after b1 settle = %d, want 38", got)
	}
	if got := b.Charged(); got != 12 {
		t.Fatalf("b charged after b1 settle = %d, want 12", got)
	}

	// (3) A2 settles while A1 is still unsettled: A receives a partial
	// settlement (its unused 5 come back), but A itself must not settle yet.
	sa2 := mustSettle(t, a2, 5)
	if sa2.Refund != 5 {
		t.Fatalf("a2 refund = %d, want 5", sa2.Refund)
	}
	if got := a.Available(); got != 15 { // 40 - charged 5 - held 20
		t.Fatalf("a available after a2 settle = %d, want 15", got)
	}
	_, err := a.Settle(8)
	assertErrIs(t, err, budget.ErrChildrenUnsettled)

	// A1's held 20 is still off-limits: A may hand out at most its 15 free
	// quota, so a 16 reservation must not fit.
	_, err = a.Reserve(16)
	assertErrIs(t, err, budget.ErrInsufficientBudget)

	// (4) B completes; its refund reaches the root.
	sb := mustSettle(t, b, 9) // charged 12 + 9 = 21 of 50
	if sb.Refund != 29 {
		t.Fatalf("b refund = %d, want 29", sb.Refund)
	}
	if got := r.Charged(); got != 21 {
		t.Fatalf("root charged after B = %d, want 21", got)
	}
	if got := r.Held(); got != 40 {
		t.Fatalf("root held after B = %d, want 40", got)
	}
	// Next call that may be issued at root: exactly 39 of 100.
	if got := r.Available(); got != 39 {
		t.Fatalf("root available mid-tree = %d, want 39", got)
	}
	_, err = r.Reserve(40)
	assertErrIs(t, err, budget.ErrInsufficientBudget)
	probe := mustReserveRoot(t, r, 39)
	if probe.ReserveID() == a.ReserveID() || probe.ReserveID() == b.ReserveID() {
		t.Fatalf("probe reservation id %q overlaps an existing id", probe.ReserveID())
	}
	if got := r.Available(); got != 0 {
		t.Fatalf("root available with probe held = %d, want 0", got)
	}
	refund, err := probe.Cancel()
	if err != nil || refund != 39 {
		t.Fatalf("probe.Cancel = (%d, %v), want (39, nil)", refund, err)
	}
	if got := r.Available(); got != 39 {
		t.Fatalf("root available after probe cancel = %d, want 39", got)
	}

	// (5) A1 finally reports a valid actual usage; only then can A settle.
	sA1 := mustSettle(t, a1, 20)
	if sA1.Refund != 0 {
		t.Fatalf("a1 refund = %d, want 0", sA1.Refund)
	}
	if got := a.Charged(); got != 25 {
		t.Fatalf("a charged = %d, want 25", got)
	}
	if got := a.Available(); got != 15 {
		t.Fatalf("a available = %d, want 15", got)
	}

	// Duplicate of the late child settlement has no effect.
	_, err = a1.Settle(1)
	assertErrIs(t, err, budget.ErrAlreadySettled)

	sa := mustSettle(t, a, 6) // charged 25 + 6 = 31 of 40
	if sa.Refund != 9 {
		t.Fatalf("a refund = %d, want 9", sa.Refund)
	}

	// Whole-tree conservation: 21 + 31 charged, nothing held, 48 available.
	if got := r.Charged(); got != 52 {
		t.Fatalf("final root charged = %d, want 52", got)
	}
	if got := r.Held(); got != 0 {
		t.Fatalf("final root held = %d, want 0", got)
	}
	if got := r.Available(); got != 48 {
		t.Fatalf("final root available = %d, want 48", got)
	}
	audit := r.Audit()
	assertAuditInvariants(t, audit)
	if got := audit.Charged + audit.Held + audit.Available; got != 100 {
		t.Fatalf("tree total = %d, want 100", got)
	}
	// Explicit expected shape of the final tree. The cancelled 39-quota probe
	// is included: it consumed nothing and refunded everything.
	wantTree := []struct {
		id     string
		st     budget.State
		ch     int64
		refund int64
	}{
		{"res-1", budget.StateSettled, 31, 9},   // A
		{"res-2", budget.StateSettled, 20, 0},   // A1
		{"res-3", budget.StateSettled, 5, 5},    // A2
		{"res-4", budget.StateSettled, 21, 29},  // B
		{"res-5", budget.StateSettled, 12, 18},  // B1
		{"res-6", budget.StateCancelled, 0, 39}, // probe
	}
	flat := flatten(audit)
	if len(flat) != len(wantTree) {
		t.Fatalf("audit nodes = %d, want %d: %+v", len(flat), len(wantTree), flat)
	}
	for i, w := range wantTree {
		if flat[i].ReserveID != w.id || flat[i].State != w.st || flat[i].Charged != w.ch || flat[i].Refunded != w.refund {
			t.Fatalf("node %d = {%s %s charged=%d refunded=%d}, want {%s %s charged=%d refunded=%d}",
				i, flat[i].ReserveID, flat[i].State, flat[i].Charged, flat[i].Refunded,
				w.id, w.st, w.ch, w.refund)
		}
	}
}

func flatten(a budget.Audit) []budget.NodeAudit {
	var out []budget.NodeAudit
	var walk func(n budget.NodeAudit)
	walk = func(n budget.NodeAudit) {
		out = append(out, n)
		for _, c := range n.Children {
			walk(c)
		}
	}
	for _, b := range a.Branches {
		walk(b)
	}
	return out
}
