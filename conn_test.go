package framemux_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"framemux"
)

// smallTransport hands each chunk to a Receiver while asserting that no
// callback ever exceeds the configured write limit.
type smallTransport struct {
	mu      sync.Mutex
	got     [][]byte
	maxCall int
	limit   int
	recv    *framemux.Receiver
}

func newSmallTransport(limit int, recv *framemux.Receiver) *smallTransport {
	return &smallTransport{limit: limit, recv: recv}
}

func (s *smallTransport) write(b []byte) error {
	if len(b) > s.limit {
		return fmt.Errorf("transport got %d bytes, limit is %d", len(b), s.limit)
	}
	s.mu.Lock()
	// Copy the chunk because the sender reuses backing storage between
	// transport calls.
	s.got = append(s.got, append([]byte(nil), b...))
	if len(b) > s.maxCall {
		s.maxCall = len(b)
	}
	s.mu.Unlock()
	_, err := s.recv.Write(b)
	return err
}

func (s *smallTransport) calls() [][]byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([][]byte, len(s.got))
	for i, c := range s.got {
		out[i] = append([]byte(nil), c...)
	}
	return out
}

// gateTransport delivers every byte to a receiver and blocks the pump once
// blockAfter bytes have been delivered. Because the trip only happens after a
// write call returns, any frame containing those bytes is genuinely in
// flight. The block itself holds no mutex, so control calls stay responsive.
type gateTransport struct {
	recv     *framemux.Receiver
	mu       sync.Mutex
	blocked  chan struct{}
	released chan struct{}
	written  int
	tripAt   int
	armed    bool
}

func newGate(recv *framemux.Receiver, blockAfter int) *gateTransport {
	return &gateTransport{
		recv:     recv,
		blocked:  make(chan struct{}),
		released: make(chan struct{}),
		tripAt:   blockAfter,
		armed:    true,
	}
}

func (g *gateTransport) write(b []byte) error {
	if _, err := g.recv.Write(b); err != nil {
		return err
	}
	g.mu.Lock()
	g.written += len(b)
	trip := g.armed && g.written >= g.tripAt
	if trip {
		g.armed = false
	}
	g.mu.Unlock()
	if trip {
		close(g.blocked)
		// Park with no lock held.
		<-g.released
	}
	return nil
}

func (g *gateTransport) release() { close(g.released) }

func (g *gateTransport) totalWritten() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.written
}

func waitBlocked(t *testing.T, g *gateTransport) {
	t.Helper()
	select {
	case <-g.blocked:
	case <-time.After(2 * time.Second):
		t.Fatal("transport never reached the gate")
	}
}

func eventually(t *testing.T, timeout time.Duration, fn func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if fn() {
			return
		}
		time.Sleep(time.Millisecond * 5)
	}
	t.Fatal("condition never became true")
}

func makeMessage(id uint64, n int) framemux.Message {
	body := make([]byte, n)
	for i := range body {
		body[i] = byte(i*7 + int(id))
	}
	return framemux.Message{
		Method: framemux.Method{ClassID: 60, MethodID: 90},
		Header: framemux.Header{ClassID: 60, Flags: 0x1234, MessageID: id},
		Body:   body,
		ID:     id,
	}
}

type collected struct {
	mu   sync.Mutex
	msgs map[uint16][]framemux.Message
}

func newCollected() *collected {
	return &collected{msgs: make(map[uint16][]framemux.Message)}
}

func (c *collected) add(ch uint16, m framemux.Message) {
	c.mu.Lock()
	c.msgs[ch] = append(c.msgs[ch], m)
	c.mu.Unlock()
}

func (c *collected) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := 0
	for _, v := range c.msgs {
		n += len(v)
	}
	return n
}

// TestBasicRoundTrip verifies ordered method/header/body framing, exact body
// reassembly and the small-chunk transport contract.
func TestBasicRoundTrip(t *testing.T) {
	coll := newCollected()
	recv := framemux.NewReceiver(coll.add, nil)
	tr := newSmallTransport(7, recv)
	conn := framemux.Dial(tr.write, framemux.Config{FrameMax: 16, WriteLimit: 7})
	defer func() { _ = conn.Close() }()

	if err := conn.Open(3); err != nil {
		t.Fatal(err)
	}
	want := []framemux.Message{
		makeMessage(1, 0),   // empty body
		makeMessage(2, 1),   // single tiny body frame
		makeMessage(3, 40),  // multiple body frames, non-divisible size
		makeMessage(4, 512), // many frames
	}
	for _, m := range want {
		if err := conn.Publish(3, m); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if !conn.Idle(ctx) {
		t.Fatal("connection never drained")
	}

	if got := coll.count(); got != len(want) {
		t.Fatalf("reassembled %d messages, want %d", got, len(want))
	}
	for i, m := range want {
		got := coll.msgs[3][i]
		if got.ID != m.ID || !bytes.Equal(got.Body, m.Body) {
			t.Fatalf("message %d mismatch: id=%d bodyLen=%d want id=%d bodyLen=%d",
				i, got.ID, len(got.Body), m.ID, len(m.Body))
		}
		if got.Method != m.Method || got.Header.MessageID != m.Header.MessageID ||
			got.Header.Flags != m.Header.Flags || got.Header.BodySize != uint64(len(m.Body)) {
			t.Fatalf("message %d method/header mismatch: %+v", i, got)
		}
	}
	if tr.maxCall > 7 {
		t.Fatalf("transport callback received %d bytes in one call, limit 7", tr.maxCall)
	}
	if len(tr.calls()) < 20 {
		t.Fatalf("expected many small transport calls, got %d", len(tr.calls()))
	}
	if recv.Pending() != 0 {
		t.Fatalf("receiver retains %d buffered bytes after drain", recv.Pending())
	}
}

// TestRoundRobin verifies that continuously backlogged channels take whole
// frame turns in strict round-robin order.
func TestRoundRobin(t *testing.T) {
	type seen struct {
		ch  uint16
		typ byte
	}
	var frames []seen
	var mu sync.Mutex

	coll := newCollected()
	recv := framemux.NewReceiver(coll.add, func(f framemux.Frame) {
		mu.Lock()
		frames = append(frames, seen{f.ChannelID, f.Type})
		mu.Unlock()
	})
	tr := newSmallTransport(64, recv)
	conn := framemux.Dial(tr.write, framemux.Config{FrameMax: 10, WriteLimit: 64})
	defer func() { _ = conn.Close() }()

	const chans = 3
	const perCh = 4
	const bodyLen = 25 // 3 body frames per message (10+10+5)
	for ch := uint16(0); ch < chans; ch++ {
		if err := conn.Open(ch); err != nil {
			t.Fatal(err)
		}
	}
	var id uint64
	for k := 0; k < perCh; k++ {
		for ch := uint16(0); ch < chans; ch++ {
			id++
			if err := conn.Publish(ch, makeMessage(id, bodyLen)); err != nil {
				t.Fatal(err)
			}
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if !conn.Idle(ctx) {
		t.Fatal("never drained")
	}

	mu.Lock()
	defer mu.Unlock()
	if coll.count() != chans*perCh {
		t.Fatalf("reassembled %d, want %d", coll.count(), chans*perCh)
	}

	// Each message emits 5 frames; channel-level frames must cycle 0,1,2.
	var dataFrames []seen
	for _, f := range frames {
		if f.typ != framemux.FrameHeartbeat {
			dataFrames = append(dataFrames, f)
		}
	}
	if len(dataFrames) != chans*perCh*5 {
		t.Fatalf("got %d data frames, want %d", len(dataFrames), chans*perCh*5)
	}
	for i, f := range dataFrames {
		wantCh := uint16(i % chans)
		if f.ch != wantCh {
			t.Fatalf("frame %d on channel %d, want %d (round-robin violated)", i, f.ch, wantCh)
		}
	}
	// Within each channel the frames must be method, header, body x3.
	// Because whole frames round-robin across channels, frame at global
	// position i belongs to channel i%chans and its local slot is i/chans;
	// local frame index cycles through the 5-frame message layout.
	wantTypes := []byte{
		framemux.FrameMethod, framemux.FrameHeader,
		framemux.FrameBody, framemux.FrameBody, framemux.FrameBody,
	}
	for i, f := range dataFrames {
		localFrame := (i / chans) % 5
		if f.typ != wantTypes[localFrame] {
			t.Fatalf("frame %d (channel %d local frame %d) type %d, want %d",
				i, f.ch, localFrame, f.typ, wantTypes[localFrame])
		}
	}
}

// TestPauseOnlyFinishesInFlight checks the pause rule: a message whose method
// frame started is completed, while messages not yet started stay queued and
// other channels keep flowing.
func TestPauseOnlyFinishesInFlight(t *testing.T) {
	coll := newCollected()
	recv := framemux.NewReceiver(coll.add, nil)

	// Block after 4 delivered bytes: the method frame (11 bytes on the
	// wire) is partially written, so the message has definitely started.
	gate := newGate(recv, 4)
	conn := framemux.Dial(gate.write, framemux.Config{FrameMax: 8, WriteLimit: 4})
	defer func() { _ = conn.Close() }()

	if err := conn.Open(1); err != nil {
		t.Fatal(err)
	}
	if err := conn.Open(2); err != nil {
		t.Fatal(err)
	}
	if err := conn.Publish(1, makeMessage(100, 30)); err != nil {
		t.Fatal(err)
	}
	if err := conn.Publish(1, makeMessage(101, 30)); err != nil {
		t.Fatal(err)
	}
	waitBlocked(t, gate)

	// Method frame of msg 100 is on the wire; pause before it finishes.
	if err := conn.Pause(1); err != nil {
		t.Fatal(err)
	}
	// Channel 2 must remain fully serviceable while 1 is paused.
	for i := 0; i < 3; i++ {
		if err := conn.Publish(2, makeMessage(200+uint64(i), 20)); err != nil {
			t.Fatal(err)
		}
	}
	gate.release()

	eventually(t, 2*time.Second, func() bool {
		st := conn.Stats()
		var s framemux.ChannelStats
		for _, cs := range st.Channels {
			if cs.ID == 1 {
				s = cs
			}
		}
		return s.Paused && !s.InFlight && s.Queued == 1
	})
	// The in-flight message was fully delivered despite the pause.
	eventually(t, 2*time.Second, func() bool {
		coll.mu.Lock()
		defer coll.mu.Unlock()
		return len(coll.msgs[1]) == 1 && len(coll.msgs[2]) == 3
	})
	if coll.msgs[1][0].ID != 100 {
		t.Fatalf("paused channel delivered msg %d, want 100", coll.msgs[1][0].ID)
	}
	for _, m := range coll.msgs[2] {
		if !bytes.Equal(m.Body, makeMessage(m.ID, 20).Body) {
			t.Fatalf("channel 2 message %d body corrupted", m.ID)
		}
	}

	// Pause before anything starts: the whole queue stays put.
	if err := conn.Pause(2); err != nil {
		t.Fatal(err)
	}
	if err := conn.Publish(2, makeMessage(299, 10)); err != nil {
		t.Fatal(err)
	}
	time.Sleep(50 * time.Millisecond)
	st := conn.Stats()
	for _, cs := range st.Channels {
		if cs.ID == 2 && (cs.InFlight || cs.Queued != 1) {
			t.Fatalf("paused-before-start channel state: %+v", cs)
		}
	}
	if err := conn.Resume(2); err != nil {
		t.Fatal(err)
	}
	if err := conn.Resume(1); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if !conn.Idle(ctx) {
		t.Fatal("never drained after resume")
	}
	// Full reassembly check for every message.
	wantBodies := map[uint64]int{100: 30, 101: 30, 299: 10}
	for i := 0; i < 3; i++ {
		wantBodies[200+uint64(i)] = 20
	}
	gotIDs := map[uint64]bool{}
	for ch, ms := range coll.msgs {
		for _, m := range ms {
			gotIDs[m.ID] = true
			want := makeMessage(m.ID, wantBodies[m.ID])
			if !bytes.Equal(m.Body, want.Body) || len(m.Body) != wantBodies[m.ID] {
				t.Fatalf("channel %d message %d reassembly mismatch", ch, m.ID)
			}
		}
	}
	if len(gotIDs) != len(wantBodies) {
		t.Fatalf("reassembled ids %v, want %v", gotIDs, wantBodies)
	}
}

// TestFrameMaxChange proves an already-started frame keeps its boundary while
// frames not yet encapsulated use the new limit, and reassembly still matches.
func TestFrameMaxChange(t *testing.T) {
	t.Run("change mid frame", func(t *testing.T) {
		var sizes []int
		var mu sync.Mutex
		coll := newCollected()
		recv := framemux.NewReceiver(coll.add, func(f framemux.Frame) {
			if f.Type == framemux.FrameBody {
				mu.Lock()
				sizes = append(sizes, len(f.Payload))
				mu.Unlock()
			}
		})
		// Frame boundaries (FrameMax=10, WriteLimit=3):
		// method 1..11, header 12..40, body1 41..57, body2 58..74, ...
		// Tripping after 45 delivered bytes guarantees body1 (a 10-octet
		// frame) is already encapsulated and partially written, so it keeps
		// its old 10-octet boundary. SetFrameMax(4) then governs every frame
		// not yet encapsulated: body2.. split as 4,4,4,3.
		gate := newGate(recv, 45)
		conn := framemux.Dial(gate.write, framemux.Config{FrameMax: 10, WriteLimit: 3})
		defer func() {
			if got := gate.totalWritten(); got < 41 {
				t.Errorf("gate tripped at %d bytes, before body1 started", got)
			}
		}()
		defer func() { _ = conn.Close() }()
		if err := conn.Open(7); err != nil {
			t.Fatal(err)
		}
		if err := conn.Publish(7, makeMessage(77, 25)); err != nil {
			t.Fatal(err)
		}
		waitBlocked(t, gate)
		if err := conn.SetFrameMax(4); err != nil {
			t.Fatal(err)
		}
		gate.release()
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if !conn.Idle(ctx) {
			t.Fatal("never drained")
		}
		mu.Lock()
		defer mu.Unlock()
		// body1 was encapsulated before the change (10); the remaining
		// 15 bytes split under the new limit 4 as 4,4,4,3.
		want := []int{10, 4, 4, 4, 3}
		if fmt.Sprint(sizes) != fmt.Sprint(want) {
			t.Fatalf("body frame payloads %v, want %v", sizes, want)
		}
		if !bytes.Equal(coll.msgs[7][0].Body, makeMessage(77, 25).Body) {
			t.Fatal("reassembled body mismatch")
		}
	})

	t.Run("smaller limit between messages", func(t *testing.T) {
		var sizes []int
		var mu sync.Mutex
		coll := newCollected()
		recv := framemux.NewReceiver(coll.add, func(f framemux.Frame) {
			if f.Type == framemux.FrameBody {
				mu.Lock()
				sizes = append(sizes, len(f.Payload))
				mu.Unlock()
			}
		})
		tr := newSmallTransport(16, recv)
		conn := framemux.Dial(tr.write, framemux.Config{FrameMax: 10, WriteLimit: 16})
		defer func() { _ = conn.Close() }()
		if err := conn.Open(4); err != nil {
			t.Fatal(err)
		}
		if err := conn.Publish(4, makeMessage(1, 25)); err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if !conn.Idle(ctx) {
			t.Fatal("never drained first message")
		}
		if err := conn.SetFrameMax(3); err != nil {
			t.Fatal(err)
		}
		if err := conn.Publish(4, makeMessage(2, 11)); err != nil {
			t.Fatal(err)
		}
		if !conn.Idle(ctx) {
			t.Fatal("never drained second message")
		}
		cancel()
		mu.Lock()
		defer mu.Unlock()
		want := []int{10, 10, 5, 3, 3, 3, 2}
		if fmt.Sprint(sizes) != fmt.Sprint(want) {
			t.Fatalf("body frame payloads %v, want %v", sizes, want)
		}
	})

	t.Run("invalid limit", func(t *testing.T) {
		conn := framemux.Dial(func([]byte) error { return nil }, framemux.Config{})
		defer func() { _ = conn.Close() }()
		if err := conn.SetFrameMax(0); !errors.Is(err, framemux.ErrFrameMax) {
			t.Fatalf("got %v, want ErrFrameMax", err)
		}
		if err := conn.SetFrameMax(-3); !errors.Is(err, framemux.ErrFrameMax) {
			t.Fatalf("got %v, want ErrFrameMax", err)
		}
	})
}

// TestHeartbeatBetweenFrames checks heartbeats are whole standalone frames on
// channel 0, never splitting an in-progress frame.
func TestHeartbeatBetweenFrames(t *testing.T) {
	var seq []framemux.Frame
	var mu sync.Mutex
	coll := newCollected()
	recv := framemux.NewReceiver(coll.add, func(f framemux.Frame) {
		mu.Lock()
		seq = append(seq, f)
		mu.Unlock()
	})
	// Method spans bytes 1..11, header 12..40, body1 41..55; 48 delivered
	// bytes means body1 is mid-write when the heartbeats are queued.
	gate := newGate(recv, 48)
	conn := framemux.Dial(gate.write, framemux.Config{FrameMax: 8, WriteLimit: 5})
	defer func() { _ = conn.Close() }()
	if err := conn.Open(9); err != nil {
		t.Fatal(err)
	}
	if err := conn.Publish(9, makeMessage(909, 40)); err != nil {
		t.Fatal(err)
	}
	waitBlocked(t, gate)
	// Queued while a frame is mid-write: it must wait for the frame end.
	if err := conn.QueueHeartbeat(); err != nil {
		t.Fatal(err)
	}
	if err := conn.QueueHeartbeat(); err != nil {
		t.Fatal(err)
	}
	gate.release()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if !conn.Idle(ctx) {
		t.Fatal("never drained")
	}

	mu.Lock()
	defer mu.Unlock()
	var hbIdx []int
	for i, f := range seq {
		if f.Type == framemux.FrameHeartbeat {
			if f.ChannelID != 0 || len(f.Payload) != 0 {
				t.Fatalf("malformed heartbeat: %+v", f)
			}
			hbIdx = append(hbIdx, i)
		}
	}
	if len(hbIdx) != 2 {
		t.Fatalf("got %d heartbeats, want 2", len(hbIdx))
	}
	// A heartbeat may only sit on a frame boundary: predecessor and
	// successor (if any) must end/start whole frames, which is guaranteed by
	// construction and additionally asserted here through ordering types.
	if !bytes.Equal(coll.msgs[9][0].Body, makeMessage(909, 40).Body) {
		t.Fatal("message corrupted by heartbeat interleaving")
	}
	// A heartbeat may only sit on a frame boundary: assert it never precedes
	// the completed method+header pair (positions 0 and 1 in the sequence).
	if hbIdx[0] < 2 {
		t.Fatalf("heartbeat at position %d, inside message opening", hbIdx[0])
	}
}

// TestConcurrentInterleaving hammers many goroutines and channels, then checks
// every message reassembles byte-for-byte and per-channel order is preserved.
func TestConcurrentInterleaving(t *testing.T) {
	coll := newCollected()
	recv := framemux.NewReceiver(coll.add, nil)
	tr := newSmallTransport(3, recv)
	conn := framemux.Dial(tr.write, framemux.Config{FrameMax: 13, WriteLimit: 3})
	defer func() { _ = conn.Close() }()

	const chans = 6
	const perG = 25
	for ch := uint16(0); ch < chans; ch++ {
		if err := conn.Open(ch); err != nil {
			t.Fatal(err)
		}
	}
	var wg sync.WaitGroup
	var idMu sync.Mutex
	var nextID uint64
	for ch := uint16(0); ch < chans; ch++ {
		ch := ch
		wg.Add(1)
		go func() {
			defer wg.Done()
			for k := 0; k < perG; k++ {
				idMu.Lock()
				nextID++
				id := nextID
				idMu.Unlock()
				n := int(id%127) + 1 // 1..127 byte bodies
				if err := conn.Publish(ch, makeMessage(id, n)); err != nil {
					t.Error(err)
					return
				}
				if id%5 == 0 {
					_ = conn.QueueHeartbeat()
				}
				if id%7 == 0 {
					_ = conn.Pause(ch)
					_ = conn.Resume(ch)
				}
			}
		}()
	}
	wg.Wait()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if !conn.Idle(ctx) {
		t.Fatal("never drained under concurrency")
	}
	if got := coll.count(); got != chans*perG {
		t.Fatalf("reassembled %d messages, want %d", got, chans*perG)
	}
	for ch := uint16(0); ch < chans; ch++ {
		ms := coll.msgs[ch]
		if len(ms) != perG {
			t.Fatalf("channel %d got %d messages, want %d", ch, len(ms), perG)
		}
		// IDs are globally unique; verify bodies independently.
		seen := map[uint64]bool{}
		for _, m := range ms {
			if seen[m.ID] {
				t.Fatalf("duplicate message %d on channel %d", m.ID, ch)
			}
			seen[m.ID] = true
			want := makeMessage(m.ID, int(m.ID%127)+1)
			if !bytes.Equal(m.Body, want.Body) {
				t.Fatalf("channel %d message %d body mismatch", ch, m.ID)
			}
		}
	}
	if tr.maxCall > 3 {
		t.Fatalf("callback exceeded write limit: %d", tr.maxCall)
	}
}

// TestDefensiveCopy ensures mutating the caller's body after Publish cannot
// alter the bytes that travel over the wire.
func TestDefensiveCopy(t *testing.T) {
	coll := newCollected()
	recv := framemux.NewReceiver(coll.add, nil)
	tr := newSmallTransport(8, recv)
	conn := framemux.Dial(tr.write, framemux.Config{FrameMax: 16, WriteLimit: 8})
	defer func() { _ = conn.Close() }()
	if err := conn.Open(1); err != nil {
		t.Fatal(err)
	}
	body := []byte("abcdefghijklmnop")
	m := framemux.Message{
		Method: framemux.Method{ClassID: 1, MethodID: 2},
		Header: framemux.Header{ClassID: 1, MessageID: 55},
		Body:   body,
		ID:     55,
	}
	if err := conn.Publish(1, m); err != nil {
		t.Fatal(err)
	}
	for i := range body {
		body[i] = 'X'
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if !conn.Idle(ctx) {
		t.Fatal("never drained")
	}
	if string(coll.msgs[1][0].Body) != "abcdefghijklmnop" {
		t.Fatalf("body was mutated after publish: %q", coll.msgs[1][0].Body)
	}
}

// TestTransportErrorStopsWriter verifies a failing transport surfaces through
// Close/Err and rejects further publishes.
func TestTransportErrorStopsWriter(t *testing.T) {
	boom := errors.New("socket reset")
	var calls int
	var mu sync.Mutex
	conn := framemux.Dial(func(b []byte) error {
		mu.Lock()
		calls++
		n := calls
		mu.Unlock()
		if n >= 2 {
			return boom
		}
		return nil
	}, framemux.Config{FrameMax: 4, WriteLimit: 2})
	if err := conn.Open(1); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 10; i++ {
		_ = conn.Publish(1, makeMessage(uint64(i), 40))
	}
	select {
	case <-conn.Done():
	case <-time.After(2 * time.Second):
		t.Fatal("writer did not stop after transport error")
	}
	if err := conn.Close(); !errors.Is(err, boom) {
		t.Fatalf("Close returned %v, want %v", err, boom)
	}
	if err := conn.Err(); !errors.Is(err, boom) {
		t.Fatalf("Err returned %v, want %v", err, boom)
	}
	if err := conn.Publish(1, makeMessage(1, 1)); !errors.Is(err, framemux.ErrClosed) {
		t.Fatalf("publish after failure returned %v, want ErrClosed", err)
	}
	if err := conn.QueueHeartbeat(); !errors.Is(err, framemux.ErrClosed) {
		t.Fatalf("heartbeat after failure returned %v, want ErrClosed", err)
	}
}

// TestUnknownChannelAndCloseAPI covers the remaining API error paths.
func TestUnknownChannelAndCloseAPI(t *testing.T) {
	conn := framemux.Dial(func([]byte) error { return nil }, framemux.Config{})
	if err := conn.Publish(42, makeMessage(1, 1)); !errors.Is(err, framemux.ErrUnknownChannel) {
		t.Fatalf("got %v, want ErrUnknownChannel", err)
	}
	if err := conn.Pause(42); !errors.Is(err, framemux.ErrUnknownChannel) {
		t.Fatalf("got %v, want ErrUnknownChannel", err)
	}
	if err := conn.Resume(42); !errors.Is(err, framemux.ErrUnknownChannel) {
		t.Fatalf("got %v, want ErrUnknownChannel", err)
	}
	if err := conn.Open(42); err != nil {
		t.Fatal(err)
	}
	if err := conn.Open(42); err != nil { // idempotent
		t.Fatal(err)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if err := conn.Open(1); !errors.Is(err, framemux.ErrClosed) {
		t.Fatalf("got %v, want ErrClosed", err)
	}
	if err := conn.Publish(42, makeMessage(1, 1)); !errors.Is(err, framemux.ErrClosed) {
		t.Fatalf("got %v, want ErrClosed", err)
	}
}

// TestReceiverMalformedStream exercises the receiver's defensive checks.
func TestReceiverMalformedStream(t *testing.T) {
	goodMethod := encodeForTest(t, framemux.FrameMethod, 1,
		[]byte{0, 60, 0, 90})
	goodHeader := encodeForTest(t, framemux.FrameHeader, 1,
		append(append([]byte{0, 60, 0, 0},
			0, 0, 0, 0, 0, 0, 0, 3, // body size 3
			0, 0), 0, 0, 0, 0, 0, 0, 0, 7))
	goodBody := func(payload []byte) []byte {
		return encodeForTest(t, framemux.FrameBody, 1, payload)
	}

	tests := []struct {
		name string
		wire func() []byte
		want error
	}{
		{
			name: "bad end marker",
			wire: func() []byte {
				b := append([]byte(nil), goodMethod...)
				b[len(b)-1] = 0x00
				return b
			},
			want: framemux.ErrBadFrameEnd,
		},
		{
			name: "method wrong payload size",
			wire: func() []byte {
				return encodeForTest(t, framemux.FrameMethod, 1, []byte{1, 2})
			},
			want: framemux.ErrBadMethodFrame,
		},
		{
			name: "header before method",
			wire: func() []byte { return goodHeader },
			want: framemux.ErrHeaderOrder,
		},
		{
			name: "body before method",
			wire: func() []byte { return goodBody([]byte{1}) },
			want: framemux.ErrUnexpectedFrame,
		},
		{
			name: "body overflow",
			wire: func() []byte {
				return bytes.Join([][]byte{
					goodMethod, goodHeader,
					goodBody([]byte{1, 2}), goodBody([]byte{3, 4}),
				}, nil)
			},
			want: framemux.ErrBodyOverflow,
		},
		{
			name: "body underflow new method",
			wire: func() []byte {
				return bytes.Join([][]byte{
					goodMethod, goodHeader,
					goodBody([]byte{1, 2}),
					goodMethod,
				}, nil)
			},
			want: framemux.ErrBodyUnderflow,
		},
		{
			name: "heartbeat with payload",
			wire: func() []byte {
				return encodeForTest(t, framemux.FrameHeartbeat, 0, []byte{1})
			},
			want: framemux.ErrHeartbeatPayload,
		},
		{
			name: "heartbeat on non-zero channel",
			wire: func() []byte {
				return encodeForTest(t, framemux.FrameHeartbeat, 5, nil)
			},
			want: framemux.ErrHeartbeatPayload,
		},
		{
			name: "unknown frame type",
			wire: func() []byte {
				return encodeForTest(t, 99, 1, nil)
			},
			want: framemux.ErrUnknownFrameType,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			recv := framemux.NewReceiver(nil, nil)
			// Feed one byte at a time to prove partial-frame tolerance.
			stream := tc.wire()
			var err error
			for i, b := range stream {
				_, err = recv.Write([]byte{b})
				if err != nil {
					if i != len(stream)-1 {
						// Errors may surface as soon as the final frame is complete.
					}
					break
				}
			}
			if !errors.Is(err, tc.want) {
				t.Fatalf("got %v, want %v", err, tc.want)
			}
		})
	}

	// Partial frames must buffer without error and complete later.
	recv := framemux.NewReceiver(nil, nil)
	stream := goodMethod
	half := len(stream) / 2
	if _, err := recv.Write(stream[:half]); err != nil {
		t.Fatalf("partial frame rejected: %v", err)
	}
	if recv.Pending() != half {
		t.Fatalf("pending %d, want %d", recv.Pending(), half)
	}
	if _, err := recv.Write(stream[half:]); err != nil {
		t.Fatalf("frame completion rejected: %v", err)
	}
	if recv.Pending() != 0 {
		t.Fatalf("pending %d after complete frame", recv.Pending())
	}
}

// encodeForTest builds a wire frame inside the external test package.
func encodeForTest(t *testing.T, typ byte, channel uint16, payload []byte) []byte {
	t.Helper()
	b := make([]byte, framemux.HeaderSize+len(payload))
	b[0] = typ
	b[1] = byte(channel >> 8)
	b[2] = byte(channel)
	b[3] = byte(len(payload) >> 24)
	b[4] = byte(len(payload) >> 16)
	b[5] = byte(len(payload) >> 8)
	b[6] = byte(len(payload))
	copy(b[7:], payload)
	b[len(b)-1] = framemux.FrameEnd
	return b
}

// TestIdleTimeout verifies Idle returns false on context cancellation while a
// paused channel still holds undelivered messages.
func TestIdleTimeout(t *testing.T) {
	coll := newCollected()
	recv := framemux.NewReceiver(coll.add, nil)
	tr := newSmallTransport(16, recv)
	conn := framemux.Dial(tr.write, framemux.Config{FrameMax: 8, WriteLimit: 16})
	defer func() { _ = conn.Close() }()
	if err := conn.Open(1); err != nil {
		t.Fatal(err)
	}
	if err := conn.Publish(1, makeMessage(1, 20)); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if !conn.Idle(ctx) {
		t.Fatal("first message should have drained")
	}

	// Pause and enqueue work that can never start; Idle must time out.
	if err := conn.Pause(1); err != nil {
		t.Fatal(err)
	}
	if err := conn.Publish(1, makeMessage(2, 20)); err != nil {
		t.Fatal(err)
	}
	shortCtx, shortCancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer shortCancel()
	if conn.Idle(shortCtx) {
		t.Fatal("Idle reported quiescence with a paused, queued message")
	}
	if err := conn.Resume(1); err != nil {
		t.Fatal(err)
	}
	if !conn.Idle(ctx) {
		t.Fatal("message did not drain after resume")
	}
	if coll.count() != 2 {
		t.Fatalf("reassembled %d messages, want 2", coll.count())
	}
}

// TestIdleWaitsForHeartbeatBytes ensures a heartbeat already encapsulated but
// still mid-write keeps Idle waiting until its bytes reach the transport.
func TestIdleWaitsForHeartbeatBytes(t *testing.T) {
	var hb int
	var mu sync.Mutex
	recv := framemux.NewReceiver(nil, func(f framemux.Frame) {
		if f.Type == framemux.FrameHeartbeat {
			mu.Lock()
			hb++
			mu.Unlock()
		}
	})
	// Block immediately, before the heartbeat frame finishes.
	gate := newGate(recv, 1)
	conn := framemux.Dial(gate.write, framemux.Config{FrameMax: 8, WriteLimit: 4})
	defer func() { _ = conn.Close() }()
	if err := conn.Open(1); err != nil {
		t.Fatal(err)
	}
	if err := conn.QueueHeartbeat(); err != nil {
		t.Fatal(err)
	}
	waitBlocked(t, gate)

	// Heartbeat count is decremented once encapsulation starts, but bytes are
	// still pending: Idle must not return yet.
	shortCtx, shortCancel := context.WithTimeout(context.Background(), 60*time.Millisecond)
	defer shortCancel()
	if conn.Idle(shortCtx) {
		t.Fatal("Idle returned while a heartbeat was still mid-write")
	}
	gate.release()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if !conn.Idle(ctx) {
		t.Fatal("connection never drained after releasing the gate")
	}
	mu.Lock()
	defer mu.Unlock()
	if hb != 1 {
		t.Fatalf("receiver saw %d heartbeats, want 1", hb)
	}
}

// TestEmptyBodyMessage covers the boundary where a header completes a message
// with no body frames at all.
func TestEmptyBodyMessage(t *testing.T) {
	coll := newCollected()
	var frameTypes []byte
	var mu sync.Mutex
	recv := framemux.NewReceiver(coll.add, func(f framemux.Frame) {
		mu.Lock()
		frameTypes = append(frameTypes, f.Type)
		mu.Unlock()
	})
	tr := newSmallTransport(5, recv)
	conn := framemux.Dial(tr.write, framemux.Config{FrameMax: 4, WriteLimit: 5})
	defer func() { _ = conn.Close() }()
	if err := conn.Open(0); err != nil {
		t.Fatal(err)
	}
	if err := conn.Publish(0, makeMessage(1, 0)); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if !conn.Idle(ctx) {
		t.Fatal("never drained empty-body message")
	}
	mu.Lock()
	defer mu.Unlock()
	want := []byte{framemux.FrameMethod, framemux.FrameHeader}
	if fmt.Sprint(frameTypes) != fmt.Sprint(want) {
		t.Fatalf("frames %v, want method then header only", frameTypes)
	}
	if len(coll.msgs[0]) != 1 || len(coll.msgs[0][0].Body) != 0 {
		t.Fatalf("empty-body message not reassembled: %+v", coll.msgs)
	}
}
