package blockcw

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func newTestStore(t *testing.T) *Store {
	t.Helper()
	path := filepath.Join(t.TempDir(), "durable", "blockcw.json")
	s, err := Open(path)
	if err != nil {
		t.Fatalf("Open(%q) error: %v", path, err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func mustCreateFourBlockVolume(t *testing.T, s *Store, id string, count int) {
	t.Helper()
	if count <= 0 {
		count = 4
	}
	_, err := s.CreateVolume(context.Background(), id, VolumeConfig{BlockSize: 4, BlockCount: count})
	if err != nil {
		t.Fatalf("CreateVolume(%q) error: %v", id, err)
	}
}

func seedFourByteBlocks(t *testing.T, s *Store, volumeID string, blocks ...string) {
	t.Helper()
	writes := make([]BlockWrite, 0, len(blocks))
	for i, b := range blocks {
		if len(b) != 4 {
			t.Fatalf("test block %q must be exactly four bytes", b)
		}
		writes = append(writes, BlockWrite{Index: i, Data: []byte(b)})
	}
	if err := s.WriteBlocks(context.Background(), volumeID, writes); err != nil {
		t.Fatalf("WriteBlocks seed error: %v", err)
	}
}

func assertBytesEqual(t *testing.T, name string, got, want []byte) {
	t.Helper()
	if string(got) != string(want) {
		t.Fatalf("%s = %q, want %q", name, got, want)
	}
}

func readAllStrings(t *testing.T, s *Store, volumeID string) []string {
	t.Helper()
	blocks, err := s.ReadVolume(context.Background(), volumeID)
	if err != nil {
		t.Fatalf("ReadVolume(%q) error: %v", volumeID, err)
	}
	out := make([]string, len(blocks))
	for i, b := range blocks {
		out[i] = string(b)
	}
	return out
}

func TestCompareAndWriteCommitsAllBlocksAtomically(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	mustCreateFourBlockVolume(t, s, "vol", 3)
	seedFourByteBlocks(t, s, "vol", "aaaa", "bbbb", "cccc")

	req := Request{
		VolumeID:  "vol",
		RequestID: "req-commit",
		Operations: []Operation{
			{Index: 1, Expected: []byte("bbbb"), Replacement: []byte("BBBB")},
			{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("AAAA")},
			{Index: 2, Expected: []byte("cccc"), Replacement: []byte("CCCC")},
		},
	}

	res, err := s.CompareAndWrite(ctx, req)
	if err != nil {
		t.Fatalf("CompareAndWrite first call error: %v", err)
	}
	if !res.Committed() || res.Replayed {
		t.Fatalf("first result = %+v, want original committed decision", res)
	}
	if len(res.Mismatches) != 0 {
		t.Fatalf("committed result has mismatches: %+v", res.Mismatches)
	}

	if got := readAllStrings(t, s, "vol"); strings.Join(got, ",") != "AAAA,BBBB,CCCC" {
		t.Fatalf("blocks after commit = %v, want [AAAA BBBB CCCC]", got)
	}

	retry, err := s.CompareAndWrite(ctx, req)
	if err != nil {
		t.Fatalf("CompareAndWrite retry error: %v", err)
	}
	if !retry.Committed() || !retry.Replayed {
		t.Fatalf("retry result = %+v, want replayed committed decision", retry)
	}
	if !retry.CompletedAt.Equal(res.CompletedAt) {
		t.Fatalf("retry completed_at = %v, want original %v", retry.CompletedAt, res.CompletedAt)
	}
	if got := readAllStrings(t, s, "vol"); strings.Join(got, ",") != "AAAA,BBBB,CCCC" {
		t.Fatalf("blocks after retry = %v, want unchanged [AAAA BBBB CCCC]", got)
	}
}

func TestCompareAndWriteReturnsEveryMismatchAndPreservesOriginalDecision(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	mustCreateFourBlockVolume(t, s, "vol", 3)
	seedFourByteBlocks(t, s, "vol", "0000", "1111", "2222")

	req := Request{
		VolumeID:  "vol",
		RequestID: "req-conflict",
		Operations: []Operation{
			{Index: 0, Expected: []byte("9999"), Replacement: []byte("XXXX")},
			{Index: 1, Expected: []byte("1111"), Replacement: []byte("YYYY")},
			{Index: 2, Expected: []byte("8888"), Replacement: []byte("ZZZZ")},
		},
	}

	res, err := s.CompareAndWrite(ctx, req)
	if err != nil {
		t.Fatalf("CompareAndWrite conflict error: %v", err)
	}
	if res.Decision != DecisionConflict || res.Replayed {
		t.Fatalf("result = %+v, want original conflict", res)
	}
	if len(res.Mismatches) != 2 {
		t.Fatalf("len(Mismatches) = %d (%+v), want 2", len(res.Mismatches), res.Mismatches)
	}
	if res.Mismatches[0].Index != 0 || res.Mismatches[1].Index != 2 {
		t.Fatalf("mismatch indices = %d,%d; want 0,2", res.Mismatches[0].Index, res.Mismatches[1].Index)
	}
	assertBytesEqual(t, "mismatch[0].actual", res.Mismatches[0].Actual, []byte("0000"))
	assertBytesEqual(t, "mismatch[1].actual", res.Mismatches[1].Actual, []byte("2222"))

	if got := readAllStrings(t, s, "vol"); strings.Join(got, ",") != "0000,1111,2222" {
		t.Fatalf("blocks after conflict = %v, want unchanged", got)
	}

	// A later ordinary write changes one actual block.
	if err := s.WriteBlock(ctx, "vol", 0, []byte("7777")); err != nil {
		t.Fatalf("WriteBlock after conflict error: %v", err)
	}

	retry, err := s.CompareAndWrite(ctx, req)
	if err != nil {
		t.Fatalf("retry conflict error: %v", err)
	}
	if retry.Decision != DecisionConflict || !retry.Replayed {
		t.Fatalf("retry = %+v, want replayed conflict", retry)
	}
	if len(retry.Mismatches) != 2 {
		t.Fatalf("retry mismatches = %+v, want two stored mismatches", retry.Mismatches)
	}
	// The retry must return the bytes from the original decision, not the
	// current 7777 value.
	assertBytesEqual(t, "retry original actual for block 0", retry.Mismatches[0].Actual, []byte("0000"))
	assertBytesEqual(t, "retry original actual for block 2", retry.Mismatches[1].Actual, []byte("2222"))

	cur, err := s.ReadBlock(ctx, "vol", 0)
	if err != nil {
		t.Fatal(err)
	}
	assertBytesEqual(t, "current block after replay", cur, []byte("7777"))
}

func TestRequestIDReuseWithChangedContentIsRejected(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	mustCreateFourBlockVolume(t, s, "vol", 2)
	seedFourByteBlocks(t, s, "vol", "aaaa", "bbbb")

	base := Request{
		VolumeID:  "vol",
		RequestID: "req-reuse",
		Operations: []Operation{
			{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("AAAA")},
			{Index: 1, Expected: []byte("bbbb"), Replacement: []byte("BBBB")},
		},
	}
	if res, err := s.CompareAndWrite(ctx, base); err != nil || !res.Committed() {
		t.Fatalf("base CompareAndWrite: res=%+v err=%v", res, err)
	}

	changedReplacement := base
	changedReplacement.Operations = append([]Operation(nil), base.Operations...)
	changedReplacement.Operations[0].Replacement = []byte("DIFF")
	_, err := s.CompareAndWrite(ctx, changedReplacement)
	assertRequestReuse(t, err, "changed replacement")

	changedExpected := base
	changedExpected.Operations = append([]Operation(nil), base.Operations...)
	changedExpected.Operations[1].Expected = []byte("????")
	_, err = s.CompareAndWrite(ctx, changedExpected)
	assertRequestReuse(t, err, "changed expected bytes")

	changedVolume := base
	changedVolume.VolumeID = "other-volume"
	_, err = s.CompareAndWrite(ctx, changedVolume)
	assertRequestReuse(t, err, "changed volume id")

	changedSet := base
	changedSet.Operations = []Operation{base.Operations[0]}
	_, err = s.CompareAndWrite(ctx, changedSet)
	assertRequestReuse(t, err, "removed operation")

	// Reordering the same canonical group is a retry, not content reuse.
	reordered := base
	reordered.Operations = []Operation{base.Operations[1], base.Operations[0]}
	res, err := s.CompareAndWrite(ctx, reordered)
	if err != nil {
		t.Fatalf("reordered identical request error: %v", err)
	}
	if !res.Committed() || !res.Replayed {
		t.Fatalf("reordered result = %+v, want replayed commit", res)
	}

	if got := readAllStrings(t, s, "vol"); strings.Join(got, ",") != "AAAA,BBBB" {
		t.Fatalf("blocks after rejected reuse = %v, want AAAA,BBBB", got)
	}
}

func assertRequestReuse(t *testing.T, err error, caseName string) {
	t.Helper()
	if err == nil {
		t.Fatalf("%s: expected request-ID reuse error, got nil", caseName)
	}
	if !errors.Is(err, ErrRequestIDReused) {
		t.Fatalf("%s: error %v does not wrap ErrRequestIDReused", caseName, err)
	}
	var reuseErr *RequestReuseError
	if !errors.As(err, &reuseErr) {
		t.Fatalf("%s: error %T is not *RequestReuseError", caseName, err)
	}
	if reuseErr.RequestID != "req-reuse" {
		t.Fatalf("%s: RequestID = %q", caseName, reuseErr.RequestID)
	}
	if reuseErr.ExistingFingerprint == "" || reuseErr.ProvidedFingerprint == "" {
		t.Fatalf("%s: fingerprints should not be empty", caseName)
	}
	if reuseErr.ExistingFingerprint == reuseErr.ProvidedFingerprint {
		t.Fatalf("%s: changed request should have a different fingerprint", caseName)
	}
}

func TestOtherWritesAndNewCompareAndWritesProceedAfterSuccess(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	mustCreateFourBlockVolume(t, s, "vol", 2)
	seedFourByteBlocks(t, s, "vol", "aaaa", "bbbb")

	original := Request{
		VolumeID:  "vol",
		RequestID: "req-original",
		Operations: []Operation{
			{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("AAAA")},
			{Index: 1, Expected: []byte("bbbb"), Replacement: []byte("BBBB")},
		},
	}
	if res, err := s.CompareAndWrite(ctx, original); err != nil || !res.Committed() {
		t.Fatalf("original commit: res=%+v err=%v", res, err)
	}

	// Ordinary write after successful idempotent commit.
	if err := s.WriteBlock(ctx, "vol", 0, []byte("dddd")); err != nil {
		t.Fatalf("ordinary WriteBlock error: %v", err)
	}

	// A new request based on the new current bytes succeeds.
	next := Request{
		VolumeID:  "vol",
		RequestID: "req-next",
		Operations: []Operation{
			{Index: 0, Expected: []byte("dddd"), Replacement: []byte("EEEE")},
			{Index: 1, Expected: []byte("BBBB"), Replacement: []byte("FFFF")},
		},
	}
	if res, err := s.CompareAndWrite(ctx, next); err != nil || !res.Committed() {
		t.Fatalf("next commit: res=%+v err=%v", res, err)
	}

	// Retrying the old successful request must return its old decision but
	// must not roll back the ordinary write or the newer committed group.
	replay, err := s.CompareAndWrite(ctx, original)
	if err != nil {
		t.Fatalf("original replay error: %v", err)
	}
	if !replay.Committed() || !replay.Replayed {
		t.Fatalf("original replay = %+v", replay)
	}
	if got := readAllStrings(t, s, "vol"); strings.Join(got, ",") != "EEEE,FFFF" {
		t.Fatalf("blocks after old replay = %v, want newer EEEE,FFFF", got)
	}
}

func TestInvalidRequestsAreRejectedAndNotRecorded(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	mustCreateFourBlockVolume(t, s, "vol", 2)
	seedFourByteBlocks(t, s, "vol", "aaaa", "bbbb")

	validOps := []Operation{{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("AAAA")}}
	tests := []struct {
		name    string
		req     Request
		wantErr error
	}{
		{
			name:    "empty volume",
			req:     Request{VolumeID: "   ", RequestID: "bad-empty-volume", Operations: validOps},
			wantErr: ErrInvalidRequest,
		},
		{
			name:    "empty request id",
			req:     Request{VolumeID: "vol", RequestID: "", Operations: validOps},
			wantErr: ErrInvalidRequest,
		},
		{
			name:    "no operations",
			req:     Request{VolumeID: "vol", RequestID: "bad-no-ops"},
			wantErr: ErrInvalidRequest,
		},
		{
			name: "negative index",
			req: Request{VolumeID: "vol", RequestID: "bad-negative", Operations: []Operation{
				{Index: -1, Expected: []byte("aaaa"), Replacement: []byte("AAAA")},
			}},
			wantErr: ErrInvalidRequest,
		},
		{
			name: "duplicate block",
			req: Request{VolumeID: "vol", RequestID: "bad-duplicate", Operations: []Operation{
				{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("AAAA")},
				{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("BBBB")},
			}},
			wantErr: ErrInvalidRequest,
		},
		{
			name: "expected replacement length disagreement",
			req: Request{VolumeID: "vol", RequestID: "bad-length-disagree", Operations: []Operation{
				{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("aaa")},
			}},
			wantErr: ErrInvalidRequest,
		},
		{
			name: "length does not match block size",
			req: Request{VolumeID: "vol", RequestID: "bad-block-length", Operations: []Operation{
				{Index: 0, Expected: []byte("aaa"), Replacement: []byte("AAA")},
			}},
			wantErr: ErrInvalidRequest,
		},
		{
			name: "index out of range",
			req: Request{VolumeID: "vol", RequestID: "bad-range", Operations: []Operation{
				{Index: 9, Expected: []byte("aaaa"), Replacement: []byte("AAAA")},
			}},
			wantErr: ErrBlockOutOfRange,
		},
		{
			name:    "unknown volume",
			req:     Request{VolumeID: "missing", RequestID: "bad-volume", Operations: validOps},
			wantErr: ErrVolumeNotFound,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := s.CompareAndWrite(ctx, tc.req)
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("error = %v, want errors.Is %v", err, tc.wantErr)
			}
			if strings.TrimSpace(tc.req.RequestID) == "" {
				return
			}
			st, err := loadState(s.path)
			if err != nil {
				t.Fatalf("loadState: %v", err)
			}
			if _, recorded := st.Requests[tc.req.RequestID]; recorded {
				t.Fatalf("invalid request %q was recorded as a decision", tc.req.RequestID)
			}
		})
	}
}

func TestReopenReplaysCommitAndConflictDecisions(t *testing.T) {
	path := filepath.Join(t.TempDir(), "restart", "blockcw.json")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	mustCreateFourBlockVolume(t, s, "vol", 2)
	seedFourByteBlocks(t, s, "vol", "aaaa", "bbbb")

	commit := Request{
		VolumeID: "vol", RequestID: "commit-after-restart",
		Operations: []Operation{{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("AAAA")}},
	}
	if res, err := s.CompareAndWrite(ctx, commit); err != nil || !res.Committed() {
		t.Fatalf("commit res=%+v err=%v", res, err)
	}

	conflict := Request{
		VolumeID: "vol", RequestID: "conflict-after-restart",
		Operations: []Operation{{Index: 1, Expected: []byte("xxxx"), Replacement: []byte("BBBB")}},
	}
	if res, err := s.CompareAndWrite(ctx, conflict); err != nil || res.Decision != DecisionConflict {
		t.Fatalf("conflict res=%+v err=%v", res, err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := Open(path)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer reopened.Close()

	commitRetry, err := reopened.CompareAndWrite(ctx, commit)
	if err != nil || !commitRetry.Committed() || !commitRetry.Replayed {
		t.Fatalf("commit retry=%+v err=%v", commitRetry, err)
	}
	conflictRetry, err := reopened.CompareAndWrite(ctx, conflict)
	if err != nil || conflictRetry.Decision != DecisionConflict || !conflictRetry.Replayed {
		t.Fatalf("conflict retry=%+v err=%v", conflictRetry, err)
	}

	changed := commit
	changed.Operations = []Operation{{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("ZZZZ")}}
	_, err = reopened.CompareAndWrite(ctx, changed)
	if !errors.Is(err, ErrRequestIDReused) {
		t.Fatalf("changed after reopen error=%v, want ErrRequestIDReused", err)
	}
}

func TestConcurrentIdenticalRequestsShareOneDecision(t *testing.T) {
	path := filepath.Join(t.TempDir(), "concurrent", "blockcw.json")
	primary, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer primary.Close()
	ctx := context.Background()
	mustCreateFourBlockVolume(t, primary, "vol", 4)
	seedFourByteBlocks(t, primary, "vol", "orig", "orig", "orig", "orig")

	const handlesCount = 4
	handles := make([]*Store, handlesCount)
	for i := range handles {
		h, err := Open(path)
		if err != nil {
			t.Fatalf("Open handle %d: %v", i, err)
		}
		handles[i] = h
		t.Cleanup(func() { _ = h.Close() })
	}

	req := Request{VolumeID: "vol", RequestID: "same-concurrent-request"}
	for i := 0; i < 4; i++ {
		req.Operations = append(req.Operations, Operation{
			Index:       i,
			Expected:    []byte("orig"),
			Replacement: []byte("done"),
		})
	}

	const callers = 24
	start := make(chan struct{})
	var wg sync.WaitGroup
	var mu sync.Mutex
	var results []*Result
	var errs []error

	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			res, err := handles[i%handlesCount].CompareAndWrite(ctx, req)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				errs = append(errs, err)
				return
			}
			results = append(results, res)
		}(i)
	}
	close(start)
	wg.Wait()

	if len(errs) != 0 {
		t.Fatalf("concurrent identical request errors: %v", errs)
	}
	if len(results) != callers {
		t.Fatalf("got %d results, want %d", len(results), callers)
	}
	original, replayed := 0, 0
	for _, res := range results {
		if !res.Committed() {
			t.Fatalf("result %+v is not committed", res)
		}
		if res.Replayed {
			replayed++
		} else {
			original++
		}
	}
	if original != 1 || replayed != callers-1 {
		t.Fatalf("original=%d replayed=%d, want 1 original and %d replays", original, replayed, callers-1)
	}
	if got := readAllStrings(t, primary, "vol"); strings.Join(got, ",") != "done,done,done,done" {
		t.Fatalf("final blocks = %v", got)
	}
}

func TestConcurrentDifferentGroupWritesNeverExposeHalfUpdate(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	const blockCount = 8
	mustCreateFourBlockVolume(t, s, "atomic", blockCount)
	seed := make([]string, blockCount)
	for i := range seed {
		seed[i] = "orig"
	}
	seedFourByteBlocks(t, s, "atomic", seed...)

	const writers = 24
	start := make(chan struct{})
	var wg sync.WaitGroup
	var mu sync.Mutex
	var results []*Result
	var errs []error
	var readerViolations []string

	// Readers repeatedly take whole-volume snapshots. Every valid published
	// state has all eight blocks equal: orig before the winner, or winner-nnn
	// after the single winning group.
	for r := 0; r < 8; r++ {
		wg.Add(1)
		go func(reader int) {
			defer wg.Done()
			<-start
			for n := 0; n < 40; n++ {
				blocks, err := s.ReadVolume(ctx, "atomic")
				if err != nil {
					mu.Lock()
					readerViolations = append(readerViolations, err.Error())
					mu.Unlock()
					return
				}
				first := string(blocks[0])
				for i, b := range blocks {
					if string(b) != first {
						mu.Lock()
						readerViolations = append(readerViolations,
							fmt.Sprintf("reader %d saw mixed block %d=%q after block0=%q", reader, i, b, first))
						mu.Unlock()
						return
					}
				}
			}
		}(r)
	}

	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			<-start
			replacement := []byte(fmt.Sprintf("w%03d", w))
			req := Request{VolumeID: "atomic", RequestID: fmt.Sprintf("unique-write-%02d", w)}
			for i := 0; i < blockCount; i++ {
				req.Operations = append(req.Operations, Operation{
					Index: i, Expected: []byte("orig"), Replacement: replacement,
				})
			}
			res, err := s.CompareAndWrite(ctx, req)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				errs = append(errs, err)
				return
			}
			results = append(results, res)
		}(w)
	}

	close(start)
	wg.Wait()

	if len(readerViolations) != 0 {
		t.Fatalf("reader observed non-atomic state: %s", strings.Join(readerViolations, "; "))
	}
	if len(errs) != 0 {
		t.Fatalf("writer errors: %v", errs)
	}
	committed, conflict := 0, 0
	var winner string
	for _, res := range results {
		switch res.Decision {
		case DecisionCommitted:
			committed++
			if len(res.Mismatches) != 0 {
				t.Fatalf("committed result has mismatches: %+v", res.Mismatches)
			}
			blocks, err := s.ReadBlocks(ctx, "atomic", 0)
			if err != nil {
				t.Fatal(err)
			}
			winner = string(blocks[0])
		case DecisionConflict:
			conflict++
			if len(res.Mismatches) != blockCount {
				t.Fatalf("conflict %s mismatch count=%d, want %d", res.RequestID, len(res.Mismatches), blockCount)
			}
		default:
			t.Fatalf("unknown decision %q", res.Decision)
		}
	}
	if committed != 1 || conflict != writers-1 {
		t.Fatalf("committed=%d conflict=%d, want 1 and %d", committed, conflict, writers-1)
	}

	got := readAllStrings(t, s, "atomic")
	for i, b := range got {
		if b != winner {
			t.Fatalf("final block %d=%q, want all blocks %q", i, b, winner)
		}
	}
}

func TestCompareAndWriteContextTimeoutLeavesStoreRetryable(t *testing.T) {
	s := newTestStore(t)
	ctx := context.Background()
	mustCreateFourBlockVolume(t, s, "vol", 1)
	seedFourByteBlocks(t, s, "vol", "aaaa")

	req := Request{
		VolumeID: "vol", RequestID: "timeout-retry",
		Operations: []Operation{{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("AAAA")}},
	}

	release, err := acquireFileLock(context.Background(), s.lockPath, true)
	if err != nil {
		t.Fatal(err)
	}
	defer release()

	lockCtx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	began := time.Now()
	_, err = s.CompareAndWrite(lockCtx, req)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("timeout call error=%v, want context.DeadlineExceeded", err)
	}
	if elapsed := time.Since(began); elapsed > 2*time.Second {
		t.Fatalf("lock wait took %v, expected prompt context timeout", elapsed)
	}

	// The timed-out caller did not record a decision because it never entered
	// the critical section. Release the simulated stuck caller and retry.
	release()
	res, err := s.CompareAndWrite(ctx, req)
	if err != nil {
		t.Fatalf("retry after timeout error: %v", err)
	}
	if !res.Committed() || res.Replayed {
		t.Fatalf("retry=%+v, want first successful commit after timeout", res)
	}
	retry, err := s.CompareAndWrite(ctx, req)
	if err != nil || !retry.Committed() || !retry.Replayed {
		t.Fatalf("idempotent retry res=%+v err=%v", retry, err)
	}
}
