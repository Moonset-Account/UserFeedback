package recordlayer

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"sync"
	"testing"
	"time"
)

// snifferTransport records every complete frame that leaves an endpoint,
// reconstructing them even when the underlying transport performs short
// writes of arbitrary length.
type snifferTransport struct {
	inner  Transport
	mu     sync.Mutex
	buf    []byte
	frames []Header
}

func newSniffer(inner Transport) *snifferTransport {
	return &snifferTransport{inner: inner}
}

func (s *snifferTransport) Write(p []byte) (int, error) {
	n, err := s.inner.Write(p)
	if n > 0 {
		s.mu.Lock()
		s.buf = append(s.buf, p[:n]...)
		for len(s.buf) >= HeaderSize {
			hdr, derr := DecodeHeader(s.buf)
			if derr != nil {
				s.mu.Unlock()
				return n, derr
			}
			total := HeaderSize + int(hdr.PayloadLen)
			if len(s.buf) < total {
				break
			}
			s.frames = append(s.frames, hdr)
			s.buf = s.buf[total:]
		}
		s.mu.Unlock()
	}
	return n, err
}

func (s *snifferTransport) Read(p []byte) (int, error) { return s.inner.Read(p) }

func (s *snifferTransport) Close() error { return s.inner.Close() }

func (s *snifferTransport) snapshot() []Header {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Header, len(s.frames))
	copy(out, s.frames)
	return out
}

// hookTransport invokes onWrite on every successful non-empty write.
type hookTransport struct {
	inner   Transport
	onWrite func(n int)
}

func (h *hookTransport) Write(p []byte) (int, error) {
	n, err := h.inner.Write(p)
	if n > 0 && h.onWrite != nil {
		h.onWrite(n)
	}
	return n, err
}

func (h *hookTransport) Read(p []byte) (int, error) { return h.inner.Read(p) }

func (h *hookTransport) Close() error { return h.inner.Close() }

type testEnd struct {
	name     string
	conn     *Conn
	cipher   *CountingCipher
	sniffer  *snifferTransport
	serveWG  sync.WaitGroup
	serveMu  sync.Mutex
	serveErr error
}

func newPair(t *testing.T, bufferLimit int, key []byte) (a, b *testEnd) {
	t.Helper()
	return newPairWithHooks(t, bufferLimit, key, nil, nil)
}

// newPairWithHooks optionally wraps each endpoint's transport before the
// connection is created, so no transport field is swapped under a running
// Serve goroutine.
func newPairWithHooks(t *testing.T, bufferLimit int, key []byte, hookA, hookB func(Transport) Transport) (a, b *testEnd) {
	t.Helper()
	trA, trB := NewPipePair(bufferLimit)
	snA, snB := newSniffer(trA), newSniffer(trB)
	ca, err := NewDemoCipher(key)
	if err != nil {
		t.Fatalf("cipher A: %v", err)
	}
	cb, err := NewDemoCipher(key)
	if err != nil {
		t.Fatalf("cipher B: %v", err)
	}
	cca, ccb := NewCountingCipher(ca), NewCountingCipher(cb)
	outA, outB := Transport(snA), Transport(snB)
	if hookA != nil {
		outA = hookA(outA)
	}
	if hookB != nil {
		outB = hookB(outB)
	}
	a = &testEnd{name: "A", conn: NewConn(outA, cca), cipher: cca, sniffer: snA}
	b = &testEnd{name: "B", conn: NewConn(outB, ccb), cipher: ccb, sniffer: snB}
	return a, b
}

func (e *testEnd) startServe() {
	e.serveWG.Add(1)
	go func() {
		defer e.serveWG.Done()
		err := e.conn.Serve()
		e.serveMu.Lock()
		e.serveErr = err
		e.serveMu.Unlock()
	}()
}

func (e *testEnd) waitServe(t *testing.T) {
	t.Helper()
	e.serveWG.Wait()
	e.serveMu.Lock()
	err := e.serveErr
	e.serveMu.Unlock()
	if err != nil {
		t.Fatalf("%s Serve: %v", e.name, err)
	}
}

func shutdown(t *testing.T, ends ...*testEnd) {
	t.Helper()
	for _, e := range ends {
		if err := e.conn.Close(); err != nil {
			t.Errorf("%s close: %v", e.name, err)
		}
	}
	for _, e := range ends {
		e.waitServe(t)
	}
}

func waitFor(t *testing.T, timeout time.Duration, cond func() bool, what string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func assertFrames(t *testing.T, e *testEnd, want []Header) {
	t.Helper()
	got := e.sniffer.snapshot()
	if len(got) != len(want) {
		t.Fatalf("%s frame count: got %d want %d (%+v)", e.name, len(got), len(want), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("%s frame %d: got %+v want %+v", e.name, i, got[i], want[i])
		}
	}
}

func frame(typ RecordType, gen, seq uint64, payloadLen int) Header {
	return Header{Type: typ, Generation: gen, Sequence: seq, PayloadLen: uint32(payloadLen)}
}

func sealedLen(plaintextLen int) int { return plaintextLen + demoTagLen }

// TestApplicationRoundTrip covers the normal path over a short-writing
// transport, including an empty application record.
func TestApplicationRoundTrip(t *testing.T) {
	a, b := newPair(t, 64, []byte("shared-demo-secret"))
	a.startServe()
	b.startServe()

	msgs := [][]byte{nil, {}, []byte("hello record layer"), bytes.Repeat([]byte("x"), 200)}
	for i, msg := range msgs {
		if err := a.conn.WriteApplication(msg); err != nil {
			t.Fatalf("write %d: %v", i, err)
		}
		got, err := b.conn.ReadApplication()
		if err != nil {
			t.Fatalf("read %d: %v", i, err)
		}
		if !bytes.Equal(got, msg) {
			t.Fatalf("read %d mismatch: got %q want %q", i, got, msg)
		}
	}
	// Each application record is opened exactly once.
	if b.cipher.OpenCount(0) != len(msgs) {
		t.Fatalf("B opens on gen 0: got %d want %d", b.cipher.OpenCount(0), len(msgs))
	}
	if a.cipher.SealCount(0) != len(msgs) {
		t.Fatalf("A seals on gen 0: got %d want %d", a.cipher.SealCount(0), len(msgs))
	}
	assertFrames(t, a, []Header{
		frame(TypeApplication, 0, 0, sealedLen(0)),
		frame(TypeApplication, 0, 1, sealedLen(0)),
		frame(TypeApplication, 0, 2, sealedLen(len("hello record layer"))),
		frame(TypeApplication, 0, 3, sealedLen(200)),
	})
	shutdown(t, a, b)
}

// TestUnilateralRotation verifies that a rotation request is protected by the
// old generation, the first record after it uses the new generation at
// sequence zero, and the peer flushes its response before its next
// application record without changing its own write generation.
func TestUnilateralRotation(t *testing.T) {
	a, b := newPair(t, 64, []byte("shared-demo-secret"))
	a.startServe()
	b.startServe()

	if err := a.conn.WriteApplication([]byte("before")); err != nil {
		t.Fatal(err)
	}
	if _, err := b.conn.ReadApplication(); err != nil {
		t.Fatal(err)
	}
	if err := a.conn.Rotate(); err != nil {
		t.Fatal(err)
	}
	waitFor(t, time.Second, func() bool {
		s := b.conn.Snapshot()
		return s.ReadGeneration == 1 && s.PendingResponse
	}, "B to observe A's rotation request")

	if err := b.conn.WriteApplication([]byte("peer-data")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, time.Second, func() bool {
		return len(a.sniffer.snapshot()) == 2
	}, "A to receive B's response")

	if err := a.conn.WriteApplication([]byte("after")); err != nil {
		t.Fatal(err)
	}
	if got, err := b.conn.ReadApplication(); err != nil || !bytes.Equal(got, []byte("after")) {
		t.Fatalf("B new-gen read: %q, %v", got, err)
	}
	if got, err := a.conn.ReadApplication(); err != nil || !bytes.Equal(got, []byte("peer-data")) {
		t.Fatalf("A peer read: %q, %v", got, err)
	}

	assertFrames(t, a, []Header{
		frame(TypeApplication, 0, 0, sealedLen(len("before"))),
		frame(TypeRotateRequest, 0, 1, sealedLen(0)),
		frame(TypeApplication, 1, 0, sealedLen(len("after"))),
	})
	assertFrames(t, b, []Header{
		// Response stays on B's unchanged write generation and consumes seq 0.
		frame(TypeRotateResponse, 0, 0, sealedLen(0)),
		frame(TypeApplication, 0, 1, sealedLen(len("peer-data"))),
	})
	if bs := b.conn.Snapshot(); bs.WriteGeneration != 0 {
		t.Fatalf("B write generation advanced unexpectedly: %d", bs.WriteGeneration)
	}
	shutdown(t, a, b)
}

// TestSimultaneousRotation is the headline scenario: both endpoints request a
// rotation at once. Application data must be opened exactly once, sequence
// numbers stay continuous across the generation boundary, and each response is
// a plain confirmation that never triggers another response.
func TestSimultaneousRotation(t *testing.T) {
	a, b := newPair(t, 64, []byte("shared-demo-secret"))
	a.startServe()
	b.startServe()

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); _ = a.conn.Rotate() }()
	go func() { defer wg.Done(); _ = b.conn.Rotate() }()
	wg.Wait()

	waitFor(t, time.Second, func() bool {
		sa, sb := a.conn.Snapshot(), b.conn.Snapshot()
		return sa.ReadGeneration == 1 && sb.ReadGeneration == 1 &&
			sa.PendingResponse && sb.PendingResponse
	}, "both endpoints to observe each other's request")

	// Both now write application records; the pending responses are flushed
	// first, inside WriteApplication.
	if err := a.conn.WriteApplication([]byte("alpha")); err != nil {
		t.Fatal(err)
	}
	if err := b.conn.WriteApplication([]byte("beta")); err != nil {
		t.Fatal(err)
	}
	if got, err := b.conn.ReadApplication(); err != nil || !bytes.Equal(got, []byte("alpha")) {
		t.Fatalf("B read alpha: %q, %v", got, err)
	}
	if got, err := a.conn.ReadApplication(); err != nil || !bytes.Equal(got, []byte("beta")) {
		t.Fatalf("A read beta: %q, %v", got, err)
	}

	assertFrames(t, a, []Header{
		frame(TypeRotateRequest, 0, 0, sealedLen(0)),
		frame(TypeRotateResponse, 1, 0, sealedLen(0)),
		frame(TypeApplication, 1, 1, sealedLen(len("alpha"))),
	})
	assertFrames(t, b, []Header{
		frame(TypeRotateRequest, 0, 0, sealedLen(0)),
		frame(TypeRotateResponse, 1, 0, sealedLen(0)),
		frame(TypeApplication, 1, 1, sealedLen(len("beta"))),
	})

	// Exact-once opens, and no application record is opened under gen 0 after
	// the boundary.
	for _, e := range []*testEnd{a, b} {
		if e.cipher.OpenCount(0) != 1 {
			t.Fatalf("%s gen-0 opens: got %d want 1 (only the request)", e.name, e.cipher.OpenCount(0))
		}
		if e.cipher.OpenCount(1) != 2 {
			t.Fatalf("%s gen-1 opens: got %d want 2 (response + application)", e.name, e.cipher.OpenCount(1))
		}
	}

	// Responses must not trigger responses: after draining, nothing else moves.
	time.Sleep(50 * time.Millisecond)
	if len(a.sniffer.snapshot()) != 3 || len(b.sniffer.snapshot()) != 3 {
		t.Fatalf("unexpected extra traffic: A=%d B=%d frames", len(a.sniffer.snapshot()), len(b.sniffer.snapshot()))
	}
	if sa, sb := a.conn.Snapshot(), b.conn.Snapshot(); sa.PendingResponse || sb.PendingResponse {
		t.Fatalf("pending response flag not cleared: A=%v B=%v", sa.PendingResponse, sb.PendingResponse)
	}
	shutdown(t, a, b)
}

// TestInterleavedRotationDuringSegmentedWrite proves that a rotation request
// arriving while an application record is already being segmented out cannot
// jump ahead: the in-progress record is finished first, and the response only
// precedes the next record that had not started sealing.
func TestInterleavedRotationDuringSegmentedWrite(t *testing.T) {
	// An 8-byte buffer forces every frame into many short writes.
	fired := make(chan struct{})
	var once sync.Once
	// Wrap B's outbound transport (outside its sniffer): fire when the first
	// segment of a segmented write has gone out. The wrapper is installed
	// before Serve starts so there is no concurrent transport swap.
	a, b := newPairWithHooks(t, 8, []byte("shared-demo-secret"), nil, func(tr Transport) Transport {
		return &hookTransport{
			inner: tr,
			onWrite: func(n int) {
				once.Do(func() { close(fired) })
			},
		}
	})

	// White-box gate: park B's receive loop after A's request frame has been
	// fully read but before the coordinator lock is taken.
	requestAtGate := make(chan struct{})
	release := make(chan struct{})
	var gateOnce sync.Once
	b.conn.testReceiveGate = func(hdr Header) {
		if hdr.Type == TypeRotateRequest {
			gateOnce.Do(func() { close(requestAtGate) })
			<-release
		}
	}
	a.startServe()
	b.startServe()

	big := bytes.Repeat([]byte("Z"), 64)
	writeDone := make(chan error, 1)
	go func() { writeDone <- b.conn.WriteApplication(big) }()

	// Wait until B has started (but cannot finish) segmenting its record.
	select {
	case <-fired:
	case <-time.After(time.Second):
		t.Fatal("B never started its segmented write")
	}

	// A requests a rotation while B holds the write lock. B's receive loop
	// reads the frame and stops at the gate.
	if err := a.conn.Rotate(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-requestAtGate:
	case <-time.After(time.Second):
		t.Fatal("B's receive loop did not reach the gate")
	}
	// The request is read but not applied while the segmented write is in
	// flight.
	if sb := b.conn.Snapshot(); sb.ReadGeneration != 0 || sb.PendingResponse {
		t.Fatalf("B processed rotation before finishing its record: %+v", sb)
	}

	if err := <-writeDone; err != nil {
		t.Fatalf("segmented write: %v", err)
	}
	// Let the receive loop apply the request now that the record is complete.
	close(release)
	waitFor(t, time.Second, func() bool {
		s := b.conn.Snapshot()
		return s.ReadGeneration == 1 && s.PendingResponse
	}, "B to apply A's request after the segmented record finished")

	// B's next application record must be preceded by the response; B never
	// rotates its own write generation, so both frames stay on gen 0.
	if err := b.conn.WriteApplication([]byte("next")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, time.Second, func() bool {
		return len(b.sniffer.snapshot()) == 3
	}, "B to emit response and follow-up record")

	assertFrames(t, b, []Header{
		frame(TypeApplication, 0, 0, sealedLen(len(big))),
		frame(TypeRotateResponse, 0, 1, sealedLen(0)),
		frame(TypeApplication, 0, 2, sealedLen(len("next"))),
	})

	if got, err := a.conn.ReadApplication(); err != nil || !bytes.Equal(got, big) {
		t.Fatalf("A read big: %d bytes, %v", len(got), err)
	}
	if got, err := a.conn.ReadApplication(); err != nil || !bytes.Equal(got, []byte("next")) {
		t.Fatalf("A read next: %q, %v", got, err)
	}
	// A's own frames: request on the old generation, then nothing else.
	assertFrames(t, a, []Header{
		frame(TypeRotateRequest, 0, 0, sealedLen(0)),
	})
	shutdown(t, a, b)
}

// TestReadEOF verifies queued application data drains before ReadApplication
// reports end of stream.
func TestReadEOF(t *testing.T) {
	trA, trB := NewPipePair(0)
	cipher, _ := NewDemoCipher([]byte("k"))
	a := NewConn(trA, NewCountingCipher(cipher))
	other := NewConn(trB, NewCountingCipher(mustCipher(t, "k")))
	aWG := startServe(t, a)

	if err := other.WriteApplication([]byte("last")); err != nil {
		t.Fatal(err)
	}
	if got, err := a.ReadApplication(); err != nil || !bytes.Equal(got, []byte("last")) {
		t.Fatalf("drain read: %q, %v", got, err)
	}
	if err := other.Close(); err != nil {
		t.Fatal(err)
	}
	aWG.Wait()
	if _, err := a.ReadApplication(); !errors.Is(err, io.EOF) {
		t.Fatalf("after EOF: got %v want io.EOF", err)
	}
	if err := a.Close(); err != nil {
		t.Fatal(err)
	}
}

func mustCipher(t *testing.T, key string) *DemoCipher {
	t.Helper()
	c, err := NewDemoCipher([]byte(key))
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func startServe(t *testing.T, c *Conn) *sync.WaitGroup {
	t.Helper()
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		if err := c.Serve(); err != nil {
			t.Errorf("Serve: %v", err)
		}
	}()
	return &wg
}

func startServeCollect(c *Conn) chan error {
	errc := make(chan error, 1)
	go func() {
		errc <- c.Serve()
	}()
	return errc
}

// TestWrongGenerationRejected ensures the coordinator rejects a record whose
// header does not match the expected read generation before accepting data.
func TestWrongGenerationRejected(t *testing.T) {
	trA, trB := NewPipePair(0)
	cipher := NewCountingCipher(mustCipher(t, "k"))
	a := NewConn(trA, cipher)
	serveErr := startServeCollect(a)

	sealed, err := cipher.Seal(3, 0, TypeApplication, []byte("future"))
	if err != nil {
		t.Fatal(err)
	}
	wire := EncodeHeader(nil, Header{
		Type: TypeApplication, Generation: 3, Sequence: 0, PayloadLen: uint32(len(sealed)),
	})
	wire = append(wire, sealed...)
	if err := writeAll(trB, wire); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-serveErr:
		if err == nil {
			t.Fatal("Serve accepted a wrong-generation record")
		}
	case <-time.After(time.Second):
		t.Fatal("Serve did not stop on the bad record")
	}
	if got := cipher.OpenCount(3); got != 0 {
		t.Fatalf("cipher invoked for wrong-generation record: %d opens", got)
	}
	if q := a.Snapshot().AppQueueLength; q != 0 {
		t.Fatalf("application data enqueued despite bad generation: %d", q)
	}
	if err := a.Close(); err != nil {
		t.Fatal(err)
	}
}

// TestGenerationChangeHook observes both direction transitions.
func TestGenerationChangeHook(t *testing.T) {
	a, b := newPair(t, 64, []byte("shared-demo-secret"))
	var mu sync.Mutex
	var events []string
	a.conn.SetGenerationChangeHook(func(direction string, oldGen, newGen uint64) {
		mu.Lock()
		events = append(events, fmt.Sprintf("%s:%d->%d", direction, oldGen, newGen))
		mu.Unlock()
	})
	a.startServe()
	b.startServe()

	if err := a.conn.Rotate(); err != nil {
		t.Fatal(err)
	}
	if err := b.conn.Rotate(); err != nil {
		t.Fatal(err)
	}
	waitFor(t, time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(events) == 2
	}, "both generation transitions on A")

	want := map[string]bool{"write:0->1": false, "read:0->1": false}
	mu.Lock()
	for _, ev := range events {
		if _, ok := want[ev]; ok {
			want[ev] = true
		}
	}
	mu.Unlock()
	for ev, seen := range want {
		if !seen {
			t.Fatalf("missing hook event %q, got %v", ev, events)
		}
	}
	shutdown(t, a, b)
}
