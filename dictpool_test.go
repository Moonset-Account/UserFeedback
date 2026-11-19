package dictpool

import (
	"errors"
	"sync"
	"testing"
)

const (
	customerA = "customer-a"
	customerB = "customer-b"
	wordAlpha = "alpha-dict"
	wordBeta  = "beta-dict"
	wordGamma = "gamma-dict"
)

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func mustErrIs(t *testing.T, err error, target error) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected error %v, got nil", target)
	}
	if !errors.Is(err, target) {
		t.Fatalf("expected error %v, got %v", target, err)
	}
}

func hit(t *testing.T, pool *Pool, handleID, text string) Entry {
	t.Helper()
	e, ok, err := pool.Lookup(handleID, text)
	if err != nil {
		t.Fatalf("unexpected lookup error: %v", err)
	}
	if !ok {
		t.Fatalf("expected dictionary hit for %q, got miss", text)
	}
	return e
}

func miss(t *testing.T, pool *Pool, handleID, text string) {
	t.Helper()
	_, ok, err := pool.Lookup(handleID, text)
	if err != nil {
		t.Fatalf("unexpected lookup error: %v", err)
	}
	if ok {
		t.Fatalf("expected dictionary miss for %q, got hit", text)
	}
}

// TestTwoCustomersSharePublicSnapshot is the headline acceptance flow:
// two customers start from the same public snapshot, one writes and returns
// its handle, the other still hits the original public dictionary, and their
// follow-up snapshots cannot be cross-used.
func TestTwoCustomersSharePublicSnapshot(t *testing.T) {
	pool := New()

	pub, err := pool.CreatePublicSnapshot([]string{wordAlpha})
	must(t, err)
	if pub.Scope != ScopePublic {
		t.Fatalf("public snapshot scope = %s, want public", pub.Scope)
	}

	ha, err := pool.Derive(customerA, pub.ID)
	must(t, err)
	hb, err := pool.Derive(customerB, pub.ID)
	must(t, err)

	// Before any private append both handles share the public branch.
	detached, err := pool.Detached(ha.ID)
	must(t, err)
	if detached {
		t.Fatal("fresh handle must not be detached before first append")
	}
	scope, err := pool.HandleScope(ha.ID)
	must(t, err)
	if scope != ScopePublic {
		t.Fatalf("fresh handle scope = %s, want public", scope)
	}

	// Customer A writes private data: copy-on-write separation happens here.
	must(t, pool.Append(ha.ID, customerA, wordBeta))
	detached, err = pool.Detached(ha.ID)
	must(t, err)
	if !detached {
		t.Fatal("handle must be detached after first private append")
	}
	scope, err = pool.HandleScope(ha.ID)
	must(t, err)
	if scope != ScopePrivate {
		t.Fatalf("handle scope after append = %s, want private", scope)
	}

	// A sees both the inherited public entry and its own private entry,
	// each carrying a source label describing its provenance.
	alphaInA := hit(t, pool, ha.ID, wordAlpha)
	if alphaInA.Label.Provenance != PublicData || alphaInA.Label.Scope != ScopePublic {
		t.Fatalf("public entry label in handle A = %+v", alphaInA.Label)
	}
	if alphaInA.Label.OriginSnapshotID != pub.ID {
		t.Fatalf("public entry origin = %q, want %q",
			alphaInA.Label.OriginSnapshotID, pub.ID)
	}
	betaInA := hit(t, pool, ha.ID, wordBeta)
	if betaInA.Label.Provenance != CustomerData ||
		betaInA.Label.Scope != ScopePrivate ||
		betaInA.Label.Customer != customerA {
		t.Fatalf("private entry label in handle A = %+v", betaInA.Label)
	}

	// A commits a private follow-up snapshot and returns its handle.
	privA, err := pool.SaveSnapshot(ha.ID)
	must(t, err)
	if privA.Scope != ScopePrivate || privA.Customer != customerA {
		t.Fatalf("private snapshot = scope %s customer %q", privA.Scope, privA.Customer)
	}
	must(t, pool.Return(ha.ID))

	// The returned handle is dead, but its committed snapshot is still usable.
	mustErrIs(t, pool.Return(ha.ID), ErrHandleReturned)
	_, _, err = pool.Lookup(ha.ID, wordAlpha)
	mustErrIs(t, err, ErrHandleReturned)

	// Customer B still hits the original public dictionary and never sees A's
	// private data, even though both started from the same snapshot.
	alphaInB := hit(t, pool, hb.ID, wordAlpha)
	if alphaInB.Label.Provenance != PublicData {
		t.Fatalf("customer B public hit label = %+v", alphaInB.Label)
	}
	miss(t, pool, hb.ID, wordBeta)
	detached, err = pool.Detached(hb.ID)
	must(t, err)
	if detached {
		t.Fatal("customer B handle must stay attached to the public branch")
	}

	// The public snapshot itself is untouched by A's private append.
	pubEntries, err := pool.SnapshotEntries(customerB, pub.ID)
	must(t, err)
	if len(pubEntries) != 1 || pubEntries[0].Text != wordAlpha {
		t.Fatalf("public snapshot mutated: %+v", pubEntries)
	}

	// B writes its own private data and commits its own follow-up snapshot.
	must(t, pool.Append(hb.ID, customerB, wordGamma))
	privB, err := pool.SaveSnapshot(hb.ID)
	must(t, err)
	if privB.Customer != customerB {
		t.Fatalf("customer B snapshot owner = %q, want %q", privB.Customer, customerB)
	}

	// The two follow-up snapshots must not be cross-usable.
	_, err = pool.Derive(customerB, privA.ID)
	mustErrIs(t, err, ErrNotAuthorized)
	_, err = pool.Derive(customerA, privB.ID)
	mustErrIs(t, err, ErrNotAuthorized)
	_, _, err = pool.LookupSnapshot(customerB, privA.ID, wordBeta)
	mustErrIs(t, err, ErrNotAuthorized)
	_, _, err = pool.LookupSnapshot(customerA, privB.ID, wordGamma)
	mustErrIs(t, err, ErrNotAuthorized)

	// Owners retain full access to their own branches and inherited labels.
	owned, err := pool.SnapshotEntries(customerA, privA.ID)
	must(t, err)
	if len(owned) != 2 {
		t.Fatalf("private snapshot A entry count = %d, want 2", len(owned))
	}
	var privateLabel SourceLabel
	for _, e := range owned {
		if e.Text == wordBeta {
			privateLabel = e.Label
		}
	}
	if privateLabel.OriginSnapshotID != privA.ID ||
		privateLabel.SnapshotID != privA.ID ||
		privateLabel.Customer != customerA {
		t.Fatalf("private entry label after commit = %+v", privateLabel)
	}
	_, ok, err := pool.LookupSnapshot(customerB, privB.ID, wordGamma)
	must(t, err)
	if !ok {
		t.Fatal("customer B must hit its own private snapshot")
	}
}

// TestCrossCustomerRejections covers results that must be blocked: deriving,
// reading or searching another customer's private snapshot is always denied.
func TestCrossCustomerRejections(t *testing.T) {
	pool := New()

	pub, err := pool.CreatePublicSnapshot([]string{wordAlpha})
	must(t, err)
	ha, err := pool.Derive(customerA, pub.ID)
	must(t, err)
	must(t, pool.Append(ha.ID, customerA, wordBeta))
	privA, err := pool.SaveSnapshot(ha.ID)
	must(t, err)

	// Deriving from another customer's private branch is rejected.
	if _, err := pool.Derive(customerB, privA.ID); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("cross-customer derive error = %v, want ErrNotAuthorized", err)
	}

	// Entry listing and exact lookup on the private snapshot are rejected.
	if _, err := pool.SnapshotEntries(customerB, privA.ID); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("cross-customer entries error = %v, want ErrNotAuthorized", err)
	}
	if _, _, err := pool.LookupSnapshot(customerB, privA.ID, wordBeta); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("cross-customer lookup error = %v, want ErrNotAuthorized", err)
	}

	// Appending through another customer's handle is rejected, and on rejection
	// no copy-on-write separation or state change has happened.
	if err := pool.Append(ha.ID, customerB, wordGamma); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("cross-customer append error = %v, want ErrNotAuthorized", err)
	}
	entries, err := pool.HandleEntries(ha.ID)
	must(t, err)
	if len(entries) != 2 {
		t.Fatalf("handle A entry count after rejected append = %d, want 2", len(entries))
	}
	for _, e := range entries {
		if e.Text == wordGamma {
			t.Fatal("rejected append must not add an entry")
		}
	}

	// The owner is still fully authorized.
	if _, err := pool.Derive(customerA, privA.ID); err != nil {
		t.Fatalf("owner derive from own private snapshot failed: %v", err)
	}
}

// TestCopyOnWriteBoundaryAndInterleaving exercises the sharing boundary and
// interleaved use: shared storage before the first append, strict separation
// afterwards, with a second handle derived in the middle of the flow.
func TestCopyOnWriteBoundaryAndInterleaving(t *testing.T) {
	pool := New()

	pub, err := pool.CreatePublicSnapshot([]string{wordAlpha})
	must(t, err)
	h1, err := pool.Derive(customerA, pub.ID)
	must(t, err)
	h2, err := pool.Derive(customerB, pub.ID)
	must(t, err)

	// Interleave: H1 appends while H2 remains attached to the public branch.
	must(t, pool.Append(h1.ID, customerA, wordBeta))
	miss(t, pool, h2.ID, wordBeta)
	hit(t, pool, h1.ID, wordBeta)

	// A third handle derived after the write still starts from the untouched
	// public snapshot and shares its (unmodified) branch.
	h3, err := pool.Derive(customerA, pub.ID)
	must(t, err)
	detached, err := pool.Detached(h3.ID)
	must(t, err)
	if detached {
		t.Fatal("newly derived handle should share the public branch")
	}
	miss(t, pool, h3.ID, wordBeta)
	alpha := hit(t, pool, h3.ID, wordAlpha)
	if alpha.Label.OriginSnapshotID != pub.ID {
		t.Fatalf("h3 public entry origin = %q, want %q",
			alpha.Label.OriginSnapshotID, pub.ID)
	}

	// H2's first append now detaches H2 without affecting H1 or the public one.
	must(t, pool.Append(h2.ID, customerB, wordGamma))
	miss(t, pool, h1.ID, wordGamma)
	miss(t, pool, h3.ID, wordGamma)
	hit(t, pool, h2.ID, wordGamma)

	// Repeated appends reuse the detached buffer instead of re-splitting.
	must(t, pool.Append(h2.ID, customerB, "delta-dict"))
	detached, err = pool.Detached(h2.ID)
	must(t, err)
	if !detached {
		t.Fatal("handle should remain detached after subsequent appends")
	}
	entries, err := pool.HandleEntries(h2.ID)
	must(t, err)
	if len(entries) != 3 {
		t.Fatalf("handle H2 entry count = %d, want 3", len(entries))
	}
	entries, err = pool.HandleEntries(h1.ID)
	must(t, err)
	if len(entries) != 2 {
		t.Fatalf("handle H1 entry count = %d, want 2", len(entries))
	}

	// Saving one handle's state never mutates another live handle.
	snap1, err := pool.SaveSnapshot(h1.ID)
	must(t, err)
	hit(t, pool, h2.ID, wordAlpha)
	miss(t, pool, h1.ID, wordGamma)
	if snap1.ID == pub.ID {
		t.Fatal("saved snapshot must be a new immutable object")
	}
}

// TestScopeInheritanceOnDerive checks that a derived copy always inherits
// the confidentiality scope of the source branch, including private entries
// carrying their original labels into the new handle.
func TestScopeInheritanceOnDerive(t *testing.T) {
	pool := New()

	pub, err := pool.CreatePublicSnapshot([]string{wordAlpha})
	must(t, err)
	h1, err := pool.Derive(customerA, pub.ID)
	must(t, err)
	must(t, pool.Append(h1.ID, customerA, wordBeta))
	privA, err := pool.SaveSnapshot(h1.ID)
	must(t, err)

	// A handle derived from a private branch starts private and already carries
	// the source snapshot's private entry with its full provenance.
	h2, err := pool.Derive(customerA, privA.ID)
	must(t, err)
	scope, err := pool.HandleScope(h2.ID)
	must(t, err)
	if scope != ScopePrivate {
		t.Fatalf("derived private handle scope = %s, want private", scope)
	}
	detached, err := pool.Detached(h2.ID)
	must(t, err)
	if detached {
		t.Fatal("derived handle should start attached to its source branch")
	}

	entries, err := pool.HandleEntries(h2.ID)
	must(t, err)
	if len(entries) != 2 {
		t.Fatalf("inherited entry count = %d, want 2", len(entries))
	}
	labels := map[string]SourceLabel{}
	for _, e := range entries {
		labels[e.Text] = e.Label
	}
	if labels[wordAlpha].Provenance != PublicData ||
		labels[wordAlpha].Scope != ScopePublic {
		t.Fatalf("inherited public label wrong: %+v", labels[wordAlpha])
	}
	if labels[wordBeta].Provenance != CustomerData ||
		labels[wordBeta].Scope != ScopePrivate ||
		labels[wordBeta].Customer != customerA ||
		labels[wordBeta].OriginSnapshotID != privA.ID {
		t.Fatalf("inherited private label wrong: %+v", labels[wordBeta])
	}

	// Appending to the private branch keeps everything inside customer A's scope.
	must(t, pool.Append(h2.ID, customerA, wordGamma))
	snap2, err := pool.SaveSnapshot(h2.ID)
	must(t, err)
	if snap2.Scope != ScopePrivate || snap2.Customer != customerA {
		t.Fatalf("second private snapshot = scope %s customer %q",
			snap2.Scope, snap2.Customer)
	}
	if _, err := pool.Derive(customerB, snap2.ID); !errors.Is(err, ErrNotAuthorized) {
		t.Fatalf("cross-customer derive from inherited branch error = %v", err)
	}
}

// TestInvalidInputsAndLifecycle covers boundary rejections: empty text,
// blank customer ids, unknown identifiers and returned-handle operations.
func TestInvalidInputsAndLifecycle(t *testing.T) {
	pool := New()

	if _, err := pool.CreatePublicSnapshot([]string{"", wordAlpha}); !errors.Is(err, ErrEmptyText) {
		t.Fatalf("empty public text error = %v, want ErrEmptyText", err)
	}
	pub, err := pool.CreatePublicSnapshot(nil)
	must(t, err)
	if got, err := pool.SnapshotScope(pub.ID); err != nil || got != ScopePublic {
		t.Fatalf("empty public snapshot scope = %v err = %v", got, err)
	}

	h, err := pool.Derive(customerA, pub.ID)
	must(t, err)
	mustErrIs(t, pool.Append(h.ID, customerA, ""), ErrEmptyText)
	mustErrIs(t, pool.Append(h.ID, "", wordBeta), ErrInvalidCustomer)
	if _, err := pool.Derive("", pub.ID); !errors.Is(err, ErrInvalidCustomer) {
		t.Fatalf("blank customer derive error = %v", err)
	}
	if _, err := pool.Derive(customerA, "snap-missing"); !errors.Is(err, ErrSnapshotNotFound) {
		t.Fatalf("unknown snapshot error = %v", err)
	}
	if err := pool.Return("handle-missing"); !errors.Is(err, ErrHandleNotFound) {
		t.Fatalf("unknown handle return error = %v", err)
	}
	if _, _, err := pool.Lookup("handle-missing", wordAlpha); !errors.Is(err, ErrHandleNotFound) {
		t.Fatalf("unknown handle lookup error = %v", err)
	}

	must(t, pool.Append(h.ID, customerA, wordBeta))
	snap, err := pool.SaveSnapshot(h.ID)
	must(t, err)
	must(t, pool.Return(h.ID))

	// Every mutating or reading operation on a returned handle is refused.
	mustErrIs(t, pool.Append(h.ID, customerA, wordGamma), ErrHandleReturned)
	mustErrIs(t, pool.Return(h.ID), ErrHandleReturned)
	_, err = pool.SaveSnapshot(h.ID)
	mustErrIs(t, err, ErrHandleReturned)
	_, _, err = pool.Lookup(h.ID, wordAlpha)
	mustErrIs(t, err, ErrHandleReturned)
	_, err = pool.HandleEntries(h.ID)
	mustErrIs(t, err, ErrHandleReturned)
	_, err = pool.Detached(h.ID)
	mustErrIs(t, err, ErrHandleReturned)
	_, err = pool.HandleScope(h.ID)
	mustErrIs(t, err, ErrHandleReturned)

	// Committed state outlives the handle, and a new handle can extend it.
	if _, _, err := pool.LookupSnapshot(customerA, snap.ID, wordBeta); err != nil {
		t.Fatalf("snapshot lookup after return failed: %v", err)
	}
	next, err := pool.Derive(customerA, snap.ID)
	must(t, err)
	must(t, pool.Append(next.ID, customerA, wordGamma))
}

// TestConcurrentAppends verifies the pool lock keeps interleaved appends
// consistent; run with -race for data-race detection.
func TestConcurrentAppends(t *testing.T) {
	pool := New()
	pub, err := pool.CreatePublicSnapshot([]string{wordAlpha})
	must(t, err)
	h, err := pool.Derive(customerA, pub.ID)
	must(t, err)

	const writers = 32
	var wg sync.WaitGroup
	wg.Add(writers)
	for i := 0; i < writers; i++ {
		i := i
		go func() {
			defer wg.Done()
			text := "private-" + itoa(uint64(i+1))
			if err := pool.Append(h.ID, customerA, text); err != nil {
				t.Errorf("concurrent append %d failed: %v", i, err)
			}
		}()
	}
	wg.Wait()

	entries, err := pool.HandleEntries(h.ID)
	must(t, err)
	if len(entries) != 1+writers {
		t.Fatalf("entry count after concurrent appends = %d, want %d",
			len(entries), 1+writers)
	}
	snap, err := pool.SaveSnapshot(h.ID)
	must(t, err)
	saved, err := pool.SnapshotEntries(customerA, snap.ID)
	must(t, err)
	if len(saved) != 1+writers {
		t.Fatalf("saved entry count = %d, want %d", len(saved), 1+writers)
	}
}
