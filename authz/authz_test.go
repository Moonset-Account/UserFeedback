package authz

import (
	"errors"
	"sync"
	"testing"
)

// fixture builds a catalog spanning two industries and three clients.
//
//	industry "fin":  finA1, finA2 (client A), finB1 (client B, body missing), finPub (public)
//	industry "med":  medB1 (client B), medC1 (client C, body missing)
func fixture(t *testing.T) (*Engine, map[MaterialID]string) {
	t.Helper()
	materials := []Material{
		{ID: "finA1", Industry: "fin", Client: "A", Title: "A finance 1", Confidential: true},
		{ID: "finA2", Industry: "fin", Client: "A", Title: "A finance 2", Confidential: true},
		{ID: "finB1", Industry: "fin", Client: "B", Title: "B finance 1", Confidential: true},
		{ID: "finPub", Industry: "fin", Client: "B", Title: "B finance public", Confidential: false},
		{ID: "medB1", Industry: "med", Client: "B", Title: "B medical 1", Confidential: true},
		{ID: "medC1", Industry: "med", Client: "C", Title: "C medical 1", Confidential: true},
	}
	// medC1 and finB1 are intentionally absent from the source: their
	// downloads fail.
	bodies := map[MaterialID]string{
		"finA1":  "body-finA1",
		"finA2":  "body-finA2",
		"finPub": "body-finPub",
		"medB1":  "body-medB1",
	}
	e, err := New(materials, MapSource(bodies))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return e, bodies
}

func mustAllow(t *testing.T, out Outcome, wantBody string) {
	t.Helper()
	if !out.Allowed {
		t.Fatalf("expected allowed, got denied: reason=%q limitedBy=%+v", out.Reason, out.LimitedBy)
	}
	if out.Reason != ReasonAllowed {
		t.Fatalf("allowed outcome must carry reason %q, got %q", ReasonAllowed, out.Reason)
	}
	if out.Body != wantBody {
		t.Fatalf("body = %q, want %q", out.Body, wantBody)
	}
	if out.LimitedBy != nil {
		t.Fatalf("allowed outcome must not carry LimitedBy, got %+v", out.LimitedBy)
	}
}

func mustDeny(t *testing.T, out Outcome, want Reason) {
	t.Helper()
	if out.Allowed {
		t.Fatalf("expected denial %q, got allowed with body %q", want, out.Body)
	}
	if out.Reason != want {
		t.Fatalf("reason = %q, want %q (limitedBy=%+v)", out.Reason, want, out.LimitedBy)
	}
	if out.Body != "" {
		t.Fatalf("denied outcome leaked body %q", out.Body)
	}
	if out.NewConstraint != nil {
		t.Fatalf("denied outcome must not report a new constraint: %+v", out.NewConstraint)
	}
}

// TestPublicMaterialsAlwaysReadableWithoutCommitment covers the public bypass
// and proves that public opens, previews, and failed confidential downloads
// never consume the consultant's choice.
func TestPublicMaterialsAlwaysReadableWithoutCommitment(t *testing.T) {
	e, _ := fixture(t)

	// Preview shows catalog metadata but never bodies, and does not lock.
	entries := e.Preview("c1")
	if len(entries) != 6 {
		t.Fatalf("preview entries = %d, want 6", len(entries))
	}
	if snap := e.Snapshot("c1"); len(snap.Locks) != 0 || len(snap.Reads) != 0 {
		t.Fatalf("preview changed state: %+v", snap)
	}

	// A public material is readable even though its client is not committed.
	out := e.Open("c1", "finPub")
	mustAllow(t, out, "body-finPub")
	if out.NewConstraint != nil {
		t.Fatalf("public open must not create a constraint: %+v", out.NewConstraint)
	}
	if snap := e.Snapshot("c1"); len(snap.Locks) != 0 || len(snap.Reads) != 0 {
		t.Fatalf("public open changed state: %+v", snap)
	}

	// A failed confidential download is a denial and must not commit.
	mustDeny(t, e.Open("c1", "finB1"), ReasonDownloadFailed)

	// Because the failure did not consume the choice, client A can still be
	// chosen first in "fin".
	out = e.Open("c1", "finA1")
	mustAllow(t, out, "body-finA1")
	if out.NewConstraint == nil {
		t.Fatal("first successful confidential open must report a new constraint")
	}
	if out.NewConstraint.Client != "A" || out.NewConstraint.Industry != "fin" {
		t.Fatalf("new constraint = %+v, want client A in fin", out.NewConstraint)
	}
	if snap := e.Snapshot("c1"); len(snap.Locks) != 1 || snap.Locks["fin"].Client != "A" {
		t.Fatalf("snapshot after commit = %+v", snap)
	}
}

// TestIndustryCommitmentRestrictsConfidentialReads covers the accept path for
// the committed client, the reject path for another client in the same
// industry, and the isolation of other industries and public materials.
func TestIndustryCommitmentRestrictsConfidentialReads(t *testing.T) {
	e, _ := fixture(t)
	first := e.Open("c1", "finA1")
	mustAllow(t, first, "body-finA1")
	grant := first.NewConstraint

	// Same client in the committed industry: allowed, but no new constraint.
	out := e.Open("c1", "finA2")
	mustAllow(t, out, "body-finA2")
	if out.NewConstraint != nil {
		t.Fatalf("reopening the committed client must not create a new constraint: %+v", out.NewConstraint)
	}

	// Another client's confidential material in the same industry is denied.
	// finB1 has no body, but the policy check must reject before downloading,
	// so the reason must be the commitment, not a download failure.
	denied := e.Open("c1", "finB1")
	mustDeny(t, denied, ReasonIndustryCommitted)
	if denied.LimitedBy == nil {
		t.Fatal("denial must identify the read that limited it")
	}
	if denied.LimitedBy.ID != grant.ID || denied.LimitedBy.Material != "finA1" {
		t.Fatalf("LimitedBy = %+v, want grant %d from finA1", denied.LimitedBy, grant.ID)
	}

	// Public materials are still readable in a committed industry.
	mustAllow(t, e.Open("c1", "finPub"), "body-finPub")

	// A different industry is not committed yet.
	med := e.Open("c1", "medB1")
	mustAllow(t, med, "body-medB1")
	if med.NewConstraint == nil {
		t.Fatal("first confidential open in a second industry must create a constraint")
	}
	if med.NewConstraint.Industry != "med" {
		t.Fatalf("second constraint = %+v, want industry med", med.NewConstraint)
	}
}

// TestConsultantIsolation is kept separate so a failure is unambiguous.
func TestConsultantIsolation(t *testing.T) {
	e, _ := fixture(t)
	mustAllow(t, e.Open("c1", "finA1"), "body-finA1")
	mustDeny(t, e.Open("c1", "finB1"), ReasonIndustryCommitted)

	// c2 never read anything: finB1's download fails, proving the engine
	// reached the download stage instead of being blocked by c1's commitment.
	mustDeny(t, e.Open("c2", "finB1"), ReasonDownloadFailed)
	if snap := e.Snapshot("c2"); len(snap.Locks) != 0 {
		t.Fatalf("c2 must be unaffected: %+v", snap)
	}
}

// TestWriteChecksAllReads covers the write rule: the target client must match
// every confidential material the consultant has read, denials name every
// conflicting read, and writes never create constraints.
func TestWriteChecksAllReads(t *testing.T) {
	e, _ := fixture(t)

	// No prior confidential reads: the write is unconstrained.
	mustAllowWrite(t, e.Write("c1", "A"))
	if out := e.Write("c1", "A"); out.NewConstraint != nil {
		t.Fatalf("write must never create a constraint: %+v", out.NewConstraint)
	}

	first := e.Open("c1", "finA1")
	g1 := first.NewConstraint
	if g1 == nil {
		t.Fatal("first confidential open must create a constraint")
	}
	if out := e.Open("c1", "finA2"); out.NewConstraint != nil {
		t.Fatalf("same-client reopen must not create a constraint: %+v", out.NewConstraint)
	}

	// With only the two client-A reads, a write to A is allowed.
	mustAllowWrite(t, e.Write("c1", "A"))

	if out := e.Open("c1", "medB1"); out.NewConstraint == nil {
		t.Fatal("first confidential open in a second industry must create a constraint")
	}

	// All three confidential reads are recorded, in order, with distinct IDs.
	reads := e.Snapshot("c1").Reads
	if len(reads) != 3 {
		t.Fatalf("reads = %+v, want 3", reads)
	}
	g2, g3 := reads[1], reads[2]
	if reads[0].ID != g1.ID || g2.ID == g1.ID || g3.ID == g1.ID || g3.ID == g2.ID {
		t.Fatalf("read IDs not monotonic: %+v", reads)
	}

	// Because the reads span clients A and B, no write target can satisfy all
	// of them: every write now conflicts with at least one read.
	out := e.Write("c1", "A")
	mustDenyWrite(t, out, ReasonWriteClientConflict)
	if out.LimitedBy == nil || out.LimitedBy.ID != g3.ID {
		t.Fatalf("LimitedBy = %+v, want the medical read %d", out.LimitedBy, g3.ID)
	}
	if len(out.Conflicts) != 1 || out.Conflicts[0].ID != g3.ID {
		t.Fatalf("Conflicts = %+v, want only the medical read %d", out.Conflicts, g3.ID)
	}

	// Writing to B conflicts with the two A reads.
	out = e.Write("c1", "B")
	mustDenyWrite(t, out, ReasonWriteClientConflict)
	if out.LimitedBy == nil || out.LimitedBy.ID != g1.ID {
		t.Fatalf("LimitedBy = %+v, want earliest conflicting read %d", out.LimitedBy, g1.ID)
	}
	if len(out.Conflicts) != 2 || out.Conflicts[0].ID != g1.ID || out.Conflicts[1].ID != g2.ID {
		t.Fatalf("Conflicts = %+v, want reads %d and %d", out.Conflicts, g1.ID, g2.ID)
	}

	// Writing to an untouched client conflicts with all three reads.
	out = e.Write("c1", "C")
	mustDenyWrite(t, out, ReasonWriteClientConflict)
	if len(out.Conflicts) != 3 {
		t.Fatalf("Conflicts = %+v, want all 3 reads", out.Conflicts)
	}

	// Writes did not alter the read history or the industry locks.
	snap := e.Snapshot("c1")
	if len(snap.Reads) != 3 || len(snap.Locks) != 2 {
		t.Fatalf("write changed state: %+v", snap)
	}
}

// TestWriteConflictsDedupRepeatedReads ensures re-reading the same material
// does not multiply the reported conflicts.
func TestWriteConflictsDedupRepeatedReads(t *testing.T) {
	e, _ := fixture(t)
	first := e.Open("c1", "finA1")
	mustAllow(t, first, "body-finA1")
	again := e.Open("c1", "finA1")
	mustAllow(t, again, "body-finA1")
	if again.NewConstraint != nil {
		t.Fatalf("re-read must not create a constraint: %+v", again.NewConstraint)
	}
	mustAllow(t, e.Open("c1", "medB1"), "body-medB1")

	out := e.Write("c1", "C")
	mustDenyWrite(t, out, ReasonWriteClientConflict)
	if len(out.Conflicts) != 2 {
		t.Fatalf("Conflicts = %+v, want finA1 once plus medB1", out.Conflicts)
	}
	if out.Conflicts[0].Material != "finA1" || out.Conflicts[0].ID != first.NewConstraint.ID {
		t.Fatalf("Conflicts[0] = %+v, want the earliest finA1 read", out.Conflicts[0])
	}
}

// TestWriteBoundaryAfterIndustryLock is the interleaved constraint test:
// reading in a second industry tightens the write rule even though it is
// unrelated to the first industry's lock.
func TestWriteBoundaryAfterIndustryLock(t *testing.T) {
	e, _ := fixture(t)
	mustAllow(t, e.Open("c1", "finA1"), "body-finA1")
	mustAllowWrite(t, e.Write("c1", "A"))

	// Commit a different client in a different industry.
	mustAllow(t, e.Open("c1", "medB1"), "body-medB1")

	// The target now differs from the medical read, so writing to A is denied.
	out := e.Write("c1", "A")
	mustDenyWrite(t, out, ReasonWriteClientConflict)
	if out.LimitedBy == nil || out.LimitedBy.Material != "medB1" {
		t.Fatalf("LimitedBy = %+v, want the medB1 read", out.LimitedBy)
	}
	if len(out.Conflicts) != 1 {
		t.Fatalf("Conflicts = %+v, want exactly the medB1 read", out.Conflicts)
	}
}

// TestConcurrentFirstOpenDifferentClients guarantees that when two requests
// race to be first in one industry for different clients, at most one obtains
// a body and only that read constrains later writes.
func TestConcurrentFirstOpenDifferentClients(t *testing.T) {
	src := newCountingSource(map[MaterialID]string{
		"finA1": "body-finA1",
		"finB1": "body-finB1",
	})
	e := newEngineForRace(t, src)

	outs := runConcurrentOpens(t, e, "c1", []MaterialID{"finA1", "finB1"})

	allowed := 0
	constraints := 0
	for _, out := range outs {
		if out.Allowed {
			allowed++
			if out.Body == "" {
				t.Fatal("allowed open returned empty body")
			}
			if out.NewConstraint == nil {
				t.Fatalf("winning open must report the new constraint: %+v", out)
			}
		} else {
			mustDeny(t, out, ReasonIndustryCommitted)
			if out.LimitedBy == nil {
				t.Fatal("losing open must name the winning read")
			}
		}
		if out.NewConstraint != nil {
			constraints++
		}
	}
	if allowed != 1 {
		t.Fatalf("allowed opens = %d, want exactly 1 (outs=%+v)", allowed, outs)
	}
	if constraints != 1 {
		t.Fatalf("new constraints = %d, want exactly 1", constraints)
	}

	// The losing request is rejected before downloading, so only the winner's
	// confidential body is ever fetched.
	if got := src.downloadCount(); got != 1 {
		t.Fatalf("downloads = %d, want exactly 1", got)
	}

	// Only the winning read exists, so only its client is writeable.
	snap := e.Snapshot("c1")
	if len(snap.Reads) != 1 {
		t.Fatalf("reads = %+v, want only the winning read", snap.Reads)
	}
	if len(snap.Locks) != 1 || snap.Locks["fin"].ID != snap.Reads[0].ID {
		t.Fatalf("lock/read mismatch: %+v", snap)
	}
	winner := snap.Reads[0].Client
	mustAllowWrite(t, e.Write("c1", winner))
	loser := ClientID("A")
	if winner == "A" {
		loser = "B"
	}
	mustDenyWrite(t, e.Write("c1", loser), ReasonWriteClientConflict)
}

// TestConcurrentFirstOpenWithFailedDownload is the interleaving boundary: one
// racing first open succeeds and the other's download fails. Whichever order
// the race resolves in, the failed attempt must never produce an extra read,
// an extra lock, or a second body.
func TestConcurrentFirstOpenWithFailedDownload(t *testing.T) {
	src := newCountingSource(map[MaterialID]string{
		"finA1": "body-finA1",
		// finB1 has no body: its download fails.
	})
	e := newEngineForRace(t, src)

	outs := runConcurrentOpens(t, e, "c1", []MaterialID{"finA1", "finB1"})

	if !outs[0].Allowed || outs[0].Body != "body-finA1" {
		t.Fatalf("finA1 outcome = %+v, want allowed with its body", outs[0])
	}
	if outs[0].NewConstraint == nil || outs[0].NewConstraint.Client != "A" {
		t.Fatalf("finA1 must create the A constraint: %+v", outs[0].NewConstraint)
	}
	// finB1 is denied either by the failed download (if it ran first) or by
	// the commitment A just made. It must never be allowed.
	if outs[1].Allowed || outs[1].Body != "" {
		t.Fatalf("finB1 outcome = %+v, want a denial without a body", outs[1])
	}
	if outs[1].Reason != ReasonDownloadFailed && outs[1].Reason != ReasonIndustryCommitted {
		t.Fatalf("finB1 reason = %q, want %q or %q", outs[1].Reason, ReasonDownloadFailed, ReasonIndustryCommitted)
	}
	snap := e.Snapshot("c1")
	if len(snap.Reads) != 1 || snap.Reads[0].Client != "A" {
		t.Fatalf("reads = %+v, want only the successful A read", snap.Reads)
	}
	if len(snap.Locks) != 1 || snap.Locks["fin"].Client != "A" {
		t.Fatalf("locks = %+v, want only fin->A", snap.Locks)
	}
}

// TestConcurrentFirstOpenSameClient ensures the race guard does not reject
// legitimate concurrent opens of the committed client, and still reports the
// constraint exactly once.
func TestConcurrentFirstOpenSameClient(t *testing.T) {
	src := newCountingSource(map[MaterialID]string{
		"finA1": "body-finA1",
		"finA2": "body-finA2",
	})
	e := newEngineForRace(t, src)

	ids := []MaterialID{"finA1", "finA2"}
	outs := runConcurrentOpens(t, e, "c1", ids)
	constraints := 0
	for i, out := range outs {
		mustAllow(t, out, "body-"+string(ids[i]))
		if out.NewConstraint != nil {
			constraints++
			if out.NewConstraint.Client != "A" {
				t.Fatalf("constraint = %+v, want client A", out.NewConstraint)
			}
		}
	}
	if constraints != 1 {
		t.Fatalf("new constraints = %d, want exactly 1", constraints)
	}
	if got := len(e.Snapshot("c1").Reads); got != 2 {
		t.Fatalf("reads = %d, want 2 successful reads", got)
	}
}

// TestConcurrentFirstOpenDifferentIndustries ensures independent industries
// do not block each other.
func TestConcurrentFirstOpenDifferentIndustries(t *testing.T) {
	src := newCountingSource(map[MaterialID]string{
		"finA1": "body-finA1",
		"medB1": "body-medB1",
	})
	e := newEngineForRace(t, src)

	outs := runConcurrentOpens(t, e, "c1", []MaterialID{"finA1", "medB1"})
	for _, out := range outs {
		if !out.Allowed || out.NewConstraint == nil {
			t.Fatalf("independent industries must both commit: %+v", out)
		}
	}
}

func TestInvalidInputs(t *testing.T) {
	e, _ := fixture(t)
	mustDeny(t, e.Open("", "finA1"), ReasonInvalidConsultant)
	mustDeny(t, e.Open("c1", "missing"), ReasonMaterialNotFound)
	mustDeny(t, e.Write("", "A"), ReasonInvalidConsultant)
	mustDeny(t, e.Write("c1", ""), ReasonInvalidClient)

	if _, err := New(nil, nil); err == nil {
		t.Fatal("New with nil source must fail")
	}
	if _, err := New([]Material{{ID: "x", Industry: "i"}}, MapSource{}); err == nil {
		t.Fatal("New with empty client must fail")
	}
	if _, err := New([]Material{{ID: "x", Industry: "i", Client: "A"}, {ID: "x", Industry: "i", Client: "B"}}, MapSource{}); err == nil {
		t.Fatal("New with duplicate ID must fail")
	}
}

func TestDownloadErrorIsReported(t *testing.T) {
	src := errSource{err: errors.New("boom")}
	e, err := New([]Material{{ID: "m", Industry: "i", Client: "A", Confidential: true}}, src)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	mustDeny(t, e.Open("c1", "m"), ReasonDownloadFailed)
	if snap := e.Snapshot("c1"); len(snap.Locks) != 0 {
		t.Fatalf("failed download changed state: %+v", snap)
	}
}

func mustAllowWrite(t *testing.T, out Outcome) {
	t.Helper()
	if !out.Allowed || out.Reason != ReasonAllowed {
		t.Fatalf("expected write allowed, got reason=%q limitedBy=%+v", out.Reason, out.LimitedBy)
	}
	if out.LimitedBy != nil || out.Conflicts != nil || out.NewConstraint != nil {
		t.Fatalf("allowed write carried extra detail: %+v", out)
	}
}

func mustDenyWrite(t *testing.T, out Outcome, want Reason) {
	t.Helper()
	if out.Allowed {
		t.Fatal("expected write denial, got allowed")
	}
	if out.Reason != want {
		t.Fatalf("reason = %q, want %q", out.Reason, want)
	}
	if out.NewConstraint != nil {
		t.Fatalf("denied write must not create a constraint: %+v", out.NewConstraint)
	}
	if out.LimitedBy == nil {
		t.Fatal("denied write must identify the read that limited it")
	}
}

// countingSource counts downloads and never blocks. Same-industry opens are
// serialized inside the engine, so a source that waited for all downloads to
// start would deadlock; counting is enough to assert on the accept path.
type countingSource struct {
	bodies map[MaterialID]string

	mu sync.Mutex
	n  int
}

func newCountingSource(bodies map[MaterialID]string) *countingSource {
	return &countingSource{bodies: bodies}
}

func (s *countingSource) Download(id MaterialID) (string, error) {
	s.mu.Lock()
	s.n++
	s.mu.Unlock()
	body, ok := s.bodies[id]
	if !ok {
		return "", errors.New("missing body")
	}
	return body, nil
}

func (s *countingSource) downloadCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.n
}

type errSource struct{ err error }

func (s errSource) Download(MaterialID) (string, error) { return "", s.err }

func newEngineForRace(t *testing.T, src Source) *Engine {
	t.Helper()
	e, err := New([]Material{
		{ID: "finA1", Industry: "fin", Client: "A", Confidential: true},
		{ID: "finA2", Industry: "fin", Client: "A", Confidential: true},
		{ID: "finB1", Industry: "fin", Client: "B", Confidential: true},
		{ID: "medB1", Industry: "med", Client: "B", Confidential: true},
	}, src)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return e
}

// runConcurrentOpens starts one Open per id behind a shared start barrier so
// the requests genuinely race, then returns the outcomes in input order.
func runConcurrentOpens(t *testing.T, e *Engine, c ConsultantID, ids []MaterialID) []Outcome {
	t.Helper()
	var start sync.WaitGroup
	start.Add(1)

	outs := make([]Outcome, len(ids))
	var wg sync.WaitGroup
	wg.Add(len(ids))
	for i, id := range ids {
		go func(i int, id MaterialID) {
			defer wg.Done()
			start.Wait()
			outs[i] = e.Open(c, id)
		}(i, id)
	}
	start.Done()
	wg.Wait()
	return outs
}
