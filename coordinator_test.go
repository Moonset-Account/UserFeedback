package multipathwrite

import (
	"bytes"
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

// ---- test helpers -------------------------------------------------------

type writeOutcome struct {
	id  WriteID
	res WriteResult
	err error
}

func doWrite(c *Coordinator, ctx context.Context, offset int, data []byte) <-chan writeOutcome {
	ch := make(chan writeOutcome, 1)
	go func() {
		id, res, err := c.Write(ctx, offset, data)
		ch <- writeOutcome{id: id, res: res, err: err}
	}()
	return ch
}

// submitWrite spawns a write that blocks on start before entering
// Coordinator.Write. Closing each start in sequence makes stable ID
// assignment deterministic even though the writes then execute
// concurrently.
func submitWrite(c *Coordinator, ctx context.Context, offset int, data []byte, start <-chan struct{}) <-chan writeOutcome {
	ch := make(chan writeOutcome, 1)
	go func() {
		<-start
		id, res, err := c.Write(ctx, offset, data)
		ch <- writeOutcome{id: id, res: res, err: err}
	}()
	return ch
}

func waitFor(t *testing.T, cond func() bool, timeout time.Duration, msg string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for condition: %s", msg)
}

// assertCountStays verifies the send count of id does not change during
// the given window (a real blocking assertion, not a single snapshot).
func assertCountStays(t *testing.T, dev *SimDevice, id WriteID, want int, during time.Duration, msg string) {
	t.Helper()
	deadline := time.Now().Add(during)
	for time.Now().Before(deadline) {
		if got := dev.AttemptCount(id); got != want {
			t.Fatalf("%s: attempts for write %d changed to %d, want %d", msg, id, got, want)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

func testContext(t *testing.T) (context.Context, context.CancelFunc) {
	t.Helper()
	return context.WithTimeout(context.Background(), 3*time.Second)
}

func repeatByte(b byte, n int) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = b
	}
	return out
}

// ---- 1. normal path: write, read, flush carry device/path identity ------

func TestNormalWriteReadFlushAndPathIdentity(t *testing.T) {
	ctx, cancel := testContext(t)
	defer cancel()

	dev := NewSimDevice("vol-42", 16)
	p1 := dev.NewPath()
	coord := NewCoordinator([]Path{p1}, dev.NewPath)

	id, wres, err := coord.Write(ctx, 0, []byte("hello"))
	if err != nil {
		t.Fatalf("Write: %v", err)
	}
	if id != 1 {
		t.Errorf("write id = %d, want 1", id)
	}
	if wres.Path.DeviceID != "vol-42" || wres.Path.Generation != 1 {
		t.Errorf("write result path = %+v, want device vol-42 generation 1", wres.Path)
	}

	buf := make([]byte, 5)
	rres, err := coord.Read(ctx, 0, buf)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if string(rres.Data) != "hello" {
		t.Errorf("read back = %q, want %q", rres.Data, "hello")
	}
	if rres.Path.DeviceID != "vol-42" || rres.Path.Generation != 1 {
		t.Errorf("read result path = %+v, want gen 1 identity", rres.Path)
	}

	fres, err := coord.Flush(ctx)
	if err != nil {
		t.Fatalf("Flush: %v", err)
	}
	if fres.Path.DeviceID != "vol-42" || fres.Path.Generation != 1 {
		t.Errorf("flush result path = %+v, want gen 1 identity", fres.Path)
	}
	if got := dev.Snapshot()[:5]; string(got) != "hello" {
		t.Errorf("device bytes = %q, want hello", got)
	}
}

// ---- 2. boundary: adjacent (touching but non-overlapping) writes --------

func TestAdjacentWritesRunInParallel(t *testing.T) {
	ctx, cancel := testContext(t)
	defer cancel()

	dev := NewSimDevice("vol-8", 8)
	p1, p2 := dev.NewPath(), dev.NewPath()
	coord := NewCoordinator([]Path{p1, p2}, dev.NewPath)

	// Gates for both possible stable IDs; writes will receive 1 and 2.
	g1, g2 := dev.Gate(1), dev.Gate(2)

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); coord.Write(ctx, 0, repeatByte('A', 4)) }()
	go func() { defer wg.Done(); coord.Write(ctx, 4, repeatByte('B', 4)) }()

	// Both writes must reach the device at the same time: parallelism at
	// the half-range boundary.
	waitFor(t, func() bool {
		return dev.AttemptCount(1) == 1 && dev.AttemptCount(2) == 1
	}, time.Second, "both adjacent writes in flight")

	close(g1)
	close(g2)
	wg.Wait()

	got := dev.Snapshot()
	want := append(repeatByte('A', 4), repeatByte('B', 4)...)
	if !bytes.Equal(got, want) {
		t.Errorf("device content = %q, want %q", got, want)
	}
	order := dev.ApplyOrder()
	if len(order) != 2 || (order[0] != 1 && order[0] != 2) {
		t.Errorf("apply order = %v, want both writes applied once", order)
	}
}

// ---- 3. interleaving: overlapping writes keep submission order ---------

func TestOverlappingWritesKeepSubmissionOrder(t *testing.T) {
	ctx, cancel := testContext(t)
	defer cancel()

	dev := NewSimDevice("vol-16", 16)
	coord := NewCoordinator([]Path{dev.NewPath()}, dev.NewPath)

	g1, g2, g3 := dev.Gate(1), dev.Gate(2), dev.Gate(3)

	aCh := doWrite(coord, ctx, 0, repeatByte('A', 8)) // [0,8)  id 1
	waitFor(t, func() bool { return dev.AttemptCount(1) == 1 }, time.Second, "A applying")

	// B overlaps A; C overlaps B but is adjacent to A. Release them in
	// a fixed order so stable IDs 2 and 3 are assigned deterministically.
	startB, startC := make(chan struct{}), make(chan struct{})
	bCh := submitWrite(coord, ctx, 4, repeatByte('B', 8), startB) // [4,12) id 2
	cCh := submitWrite(coord, ctx, 8, repeatByte('C', 8), startC) // [8,16) id 3
	close(startB)
	waitFor(t, func() bool {
		coord.mu.Lock()
		n := len(coord.records)
		coord.mu.Unlock()
		return n == 2
	}, time.Second, "B receives stable ID 2")
	close(startC)

	// While A is undecided, B must not even be sent.
	assertCountStays(t, dev, 2, 0, 200*time.Millisecond, "B must wait for overlapping A")

	close(g1)
	if o := <-aCh; o.err != nil {
		t.Fatalf("A write: %v", o.err)
	}
	waitFor(t, func() bool { return dev.AttemptCount(2) == 1 }, time.Second, "B applying after A")

	// C overlaps B, so it must remain queued behind B.
	assertCountStays(t, dev, 3, 0, 200*time.Millisecond, "C must wait for overlapping B")

	close(g2)
	if o := <-bCh; o.err != nil {
		t.Fatalf("B write: %v", o.err)
	}
	waitFor(t, func() bool { return dev.AttemptCount(3) == 1 }, time.Second, "C applying after B")
	close(g3)
	if o := <-cCh; o.err != nil {
		t.Fatalf("C write: %v", o.err)
	}

	want := []byte("AAAABBBBCCCCCCCC")
	if got := dev.Snapshot(); !bytes.Equal(got, want) {
		t.Errorf("device content = %q, want %q", got, want)
	}
	if order := dev.ApplyOrder(); len(order) != 3 || order[0] != 1 || order[1] != 2 || order[2] != 3 {
		t.Errorf("apply order = %v, want strictly [1 2 3]", order)
	}
}

// ---- 4. headline acceptance: half-range write loses its receipt --------

func TestHalfRangeWriteLostReceiptThenFailoverReadFlushAndProgress(t *testing.T) {
	ctx, cancel := testContext(t)
	defer cancel()

	dev := NewSimDevice("vol-headline", 8)
	p1, p2 := dev.NewPath(), dev.NewPath()
	coord := NewCoordinator([]Path{p1, p2}, dev.NewPath)

	// The write covering the first half lands, then its receipt is lost.
	dev.LoseReceipt(1)
	id1, wres, err := coord.Write(ctx, 0, []byte("ABCD"))
	if err != nil {
		t.Fatalf("first-half Write: %v", err)
	}
	if id1 != 1 {
		t.Errorf("write id = %d, want stable id 1", id1)
	}
	if dev.AttemptCount(1) != 1 {
		t.Errorf("attempts = %d, want 1: an already-written write must not be re-sent",
			dev.AttemptCount(1))
	}
	if !p1.Broken() {
		t.Error("path 1 should be marked disconnected after the lost receipt")
	}
	// The coordinator must have switched to generation 2 of the same device.
	if wres.Path.DeviceID != "vol-headline" || wres.Path.Generation != 2 {
		t.Errorf("ack path = %+v, want same device on generation 2", wres.Path)
	}

	// After failover, reading the switched-to path returns the landed bytes.
	buf := make([]byte, 4)
	rres, err := coord.Read(ctx, 0, buf)
	if err != nil {
		t.Fatalf("Read after failover: %v", err)
	}
	if string(rres.Data) != "ABCD" {
		t.Errorf("read after failover = %q, want ABCD", rres.Data)
	}
	if rres.Path.Generation != 2 || rres.Path.DeviceID != "vol-headline" {
		t.Errorf("read path = %+v, want generation 2 of vol-headline", rres.Path)
	}

	// The disjoint second-half write must keep making progress.
	id2, wres2, err := coord.Write(ctx, 4, []byte("EFGH"))
	if err != nil {
		t.Fatalf("second-half Write: %v", err)
	}
	if id2 != 2 {
		t.Errorf("second write id = %d, want 2", id2)
	}
	if wres2.Path.Generation != 2 {
		t.Errorf("second write path gen = %d, want 2", wres2.Path.Generation)
	}

	// Flush confirms only once both preceding writes are durable.
	fres, err := coord.Flush(ctx)
	if err != nil {
		t.Fatalf("Flush: %v", err)
	}
	if fres.Path.Generation != 2 {
		t.Errorf("flush path gen = %d, want 2", fres.Path.Generation)
	}

	if got, want := dev.Snapshot(), []byte("ABCDEFGH"); !bytes.Equal(got, want) {
		t.Errorf("full device = %q, want %q", got, want)
	}
}

// ---- 5. not-started outcome: re-send with the original stable ID --------

func TestNotStartedWriteIsResentWithSameID(t *testing.T) {
	ctx, cancel := testContext(t)
	defer cancel()

	dev := NewSimDevice("vol-resend", 8)
	p1, p2 := dev.NewPath(), dev.NewPath()
	coord := NewCoordinator([]Path{p1, p2}, dev.NewPath)

	// First transport dies before the device sees anything.
	dev.FailFirstPath(1)
	id, res, err := coord.Write(ctx, 0, []byte("wxyz"))
	if err != nil {
		t.Fatalf("Write: %v", err)
	}
	if id != 1 {
		t.Errorf("write id = %d, want 1 reused on re-send", id)
	}
	if dev.AttemptCount(1) != 2 {
		t.Errorf("attempts = %d, want 2 (failed send + re-send with same ID)",
			dev.AttemptCount(1))
	}
	if !p1.Broken() {
		t.Error("path 1 should be broken after the injected failure")
	}
	if res.Path.Generation != 2 || res.Path.DeviceID != "vol-resend" {
		t.Errorf("ack path = %+v, want generation 2 of vol-resend", res.Path)
	}
	if got := dev.Snapshot()[:4]; string(got) != "wxyz" {
		t.Errorf("device bytes = %q, want wxyz", got)
	}
}

// ---- 6. unknown outcome blocks the range; disjoint writes continue ------

func TestUnknownBlocksOverlappingRangeButDisjointWriteProceeds(t *testing.T) {
	ctx, cancel := testContext(t)
	defer cancel()

	dev := NewSimDevice("vol-unknown", 16)
	p1 := dev.NewPath()
	coord := NewCoordinator([]Path{p1}, dev.NewPath)

	g1 := dev.Gate(1)
	dev.MarkUnknown(1)
	dev.LoseReceipt(1)

	// A: first half, lands but its receipt is lost and status is unknown.
	aCh := doWrite(coord, ctx, 0, repeatByte('A', 8))
	waitFor(t, func() bool { return dev.AttemptCount(1) == 1 }, time.Second, "A sent")
	close(g1)
	waitFor(t, func() bool { return p1.Broken() }, time.Second, "path 1 disconnects")

	// B overlaps A and must be blocked while A is unknown. C is disjoint
	// and must proceed. Release B first so it deterministically keeps
	// stable ID 2; C receives ID 3.
	startB, startC := make(chan struct{}), make(chan struct{})
	bCh := submitWrite(coord, ctx, 4, repeatByte('B', 8), startB)
	cCh := submitWrite(coord, ctx, 12, repeatByte('C', 4), startC)
	close(startB)
	waitFor(t, func() bool {
		coord.mu.Lock()
		n := len(coord.records)
		coord.mu.Unlock()
		return n == 2
	}, time.Second, "B receives stable ID 2")
	close(startC)

	co := <-cCh
	if co.err != nil {
		t.Fatalf("disjoint write C should proceed: %v", co.err)
	}
	if dev.AttemptCount(co.id) != 1 {
		t.Errorf("C attempts = %d, want 1", dev.AttemptCount(co.id))
	}
	if got := dev.Snapshot()[12:16]; !bytes.Equal(got, repeatByte('C', 4)) {
		t.Errorf("C bytes = %q, want CCCC", got)
	}

	// B must still be parked: never sent while the overlapping range is unknown.
	assertCountStays(t, dev, 2, 0, 200*time.Millisecond,
		"overlapping B must be blocked during unknown outcome")

	// The device recovers visibility: the write had in fact landed.
	dev.ResolveUnknown(1)
	ao := <-aCh
	if ao.err != nil {
		t.Fatalf("A should resolve as written: %v", ao.err)
	}
	if ao.res.Path.Generation != 2 {
		t.Errorf("A resolved on path gen %d, want replacement generation 2",
			ao.res.Path.Generation)
	}

	bo := <-bCh
	if bo.err != nil {
		t.Fatalf("blocked B should complete after resolution: %v", bo.err)
	}
	if dev.AttemptCount(2) != 1 {
		t.Errorf("B attempts = %d, want exactly 1 after unblocking", dev.AttemptCount(2))
	}

	if _, err := coord.Flush(ctx); err != nil {
		t.Fatalf("Flush after resolution: %v", err)
	}
	want := []byte("AAAABBBBBBBBCCCC")
	if got := dev.Snapshot(); !bytes.Equal(got, want) {
		t.Errorf("device content = %q, want %q", got, want)
	}
}

// ---- 7. flush barrier waits for prior writes; later writes still run -----

func TestFlushWaitsForPriorWritesWhileLaterWritesPrepare(t *testing.T) {
	ctx, cancel := testContext(t)
	defer cancel()

	dev := NewSimDevice("vol-flush", 8)
	coord := NewCoordinator([]Path{dev.NewPath()}, dev.NewPath)
	g1 := dev.Gate(1)

	aCh := doWrite(coord, ctx, 0, repeatByte('A', 4))
	waitFor(t, func() bool { return dev.AttemptCount(1) == 1 }, time.Second, "A in flight")

	type flushOut struct {
		res FlushResult
		err error
	}
	flushCh := make(chan flushOut, 1)
	go func() {
		res, err := coord.Flush(ctx)
		flushCh <- flushOut{res: res, err: err}
	}()

	// The flush cannot confirm while its preceding write is undecided.
	select {
	case fo := <-flushCh:
		t.Fatalf("flush confirmed early: %+v", fo)
	case <-time.After(150 * time.Millisecond):
	}

	// A write submitted after the flush call is not part of the snapshot
	// and may prepare/complete while the flush is still waiting.
	bCh := doWrite(coord, ctx, 4, repeatByte('B', 4))
	bo := <-bCh
	if bo.err != nil {
		t.Fatalf("later disjoint write should proceed during pending flush: %v", bo.err)
	}

	close(g1)
	ao := <-aCh
	if ao.err != nil {
		t.Fatalf("A write: %v", ao.err)
	}

	select {
	case fo := <-flushCh:
		if fo.err != nil {
			t.Fatalf("Flush: %v", fo.err)
		}
		if fo.res.Path.Generation != 1 {
			t.Errorf("flush path gen = %d, want 1", fo.res.Path.Generation)
		}
	case <-time.After(time.Second):
		t.Fatal("flush never confirmed after the preceding write became durable")
	}

	if got, want := dev.Snapshot(), []byte("AAAABBBB"); !bytes.Equal(got, want) {
		t.Errorf("device content = %q, want %q", got, want)
	}
}

// ---- 8. adapter query contract: not-started / written / unknown ---------

func TestPathQueryReportsAllThreeStatusesWithIdentity(t *testing.T) {
	ctx, cancel := testContext(t)
	defer cancel()

	dev := NewSimDevice("vol-query", 8)
	p1 := dev.NewPath()

	q, err := p1.Query(ctx, 99)
	if err != nil {
		t.Fatalf("query missing id: %v", err)
	}
	if q.Status != StatusNotStarted {
		t.Errorf("status = %s, want not-started", q.Status)
	}
	if q.Path.DeviceID != "vol-query" || q.Path.Generation != 1 {
		t.Errorf("query path = %+v, want gen 1 identity", q.Path)
	}

	dev.MarkUnknown(99)
	q, err = p1.Query(ctx, 99)
	if err != nil {
		t.Fatalf("unknown query: %v", err)
	}
	if q.Status != StatusUnknown {
		t.Errorf("status = %s, want unknown", q.Status)
	}

	dev.ResolveUnknown(99)
	q, _ = p1.Query(ctx, 99)
	if q.Status != StatusNotStarted {
		t.Errorf("status after resolve = %s, want not-started", q.Status)
	}

	if _, err := (p1.(*simPath)).Write(ctx, 1, 0, []byte("land")); err != nil {
		t.Fatalf("seed write: %v", err)
	}
	q, _ = p1.Query(ctx, 1)
	if q.Status != StatusWritten {
		t.Errorf("status = %s, want written", q.Status)
	}
	if q.ID != 1 {
		t.Errorf("query id = %d, want 1", q.ID)
	}
}

// ---- 9. parameter validation and exhausted paths ------------------------

func TestValidationAndExhaustedPaths(t *testing.T) {
	ctx, cancel := testContext(t)
	defer cancel()

	dev := NewSimDevice("vol-err", 8)
	coord := NewCoordinator([]Path{dev.NewPath()}, dev.NewPath)

	if _, _, err := coord.Write(ctx, 0, nil); err == nil {
		t.Error("empty write should fail")
	}
	if _, _, err := coord.Write(ctx, -1, []byte("x")); err == nil {
		t.Error("negative offset should fail")
	}
	if _, err := coord.Read(ctx, 0, nil); err == nil {
		t.Error("empty read should fail")
	}
	if _, _, err := coord.Write(ctx, 7, []byte("xy")); err == nil {
		t.Error("write exceeding device range should fail")
	}

	// All paths gone and no replacement provisioned: the context deadline
	// must surface instead of an indefinite hang.
	broken := dev.NewPath()
	coord2 := NewCoordinator([]Path{broken}, nil)
	broken.(*simPath).markBroken()
	shortCtx, shortCancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer shortCancel()
	_, _, err := coord2.Write(shortCtx, 0, []byte("z"))
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("err = %v, want context.DeadlineExceeded", err)
	}
	_ = ctx
	_ = cancel
}
