package fecrx

import (
	"bytes"
	"errors"
	"fmt"
	"testing"
)

// ---------- test helpers ----------

type event struct {
	kind string // "deliver" or "gap"
	a, b uint64
	data []byte
}

type recorder struct{ events []event }

func (rec *recorder) hooks() Hooks {
	return Hooks{
		OnDeliver: func(id uint64, d []byte) {
			rec.events = append(rec.events, event{
				kind: "deliver", a: id, data: append([]byte(nil), d...),
			})
		},
		OnGap: func(from, to uint64) {
			rec.events = append(rec.events, event{kind: "gap", a: from, b: to})
		},
	}
}

func (rec *recorder) deliveredIDs() []uint64 {
	var out []uint64
	for _, e := range rec.events {
		if e.kind == "deliver" {
			out = append(out, e.a)
		}
	}
	return out
}

func (rec *recorder) deliveredData() map[uint64][]byte {
	out := map[uint64][]byte{}
	for _, e := range rec.events {
		if e.kind == "deliver" {
			out[e.a] = e.data
		}
	}
	return out
}

func (rec *recorder) gaps() [][2]uint64 {
	var out [][2]uint64
	for _, e := range rec.events {
		if e.kind == "gap" {
			out = append(out, [2]uint64{e.a, e.b})
		}
	}
	return out
}

func xorb(a, b []byte) []byte { return xorBytes(a, b) }

func packets(n int, fill byte) [][]byte {
	out := make([][]byte, n)
	for i := range out {
		out[i] = []byte(fmt.Sprintf("pkt-%d-%c", i, fill))
	}
	return out
}

func requireDelivered(t *testing.T, rec *recorder, want map[uint64][]byte) {
	t.Helper()
	got := rec.deliveredData()
	if len(got) != len(want) {
		t.Fatalf("delivered %d packets %v, want %d packets %v; events=%+v",
			len(got), rec.deliveredIDs(), len(want), idsOf(want), rec.events)
	}
	for id, w := range want {
		g, ok := got[id]
		if !ok {
			t.Fatalf("packet %d not delivered; delivered=%v", id, rec.deliveredIDs())
		}
		if !bytes.Equal(g, w) {
			t.Fatalf("packet %d bytes mismatch:\n got  %x (%q)\n want %x (%q)", id, g, g, w, w)
		}
	}
}

func idsOf(m map[uint64][]byte) []uint64 {
	var out []uint64
	for k := range m {
		out = append(out, k)
	}
	return out
}

// eq is a synthetic equation descriptor.
type eq struct {
	src   uint64
	rev   uint64
	cover []uint64
	data  []byte
}

func addEq(t *testing.T, r *Receiver, e eq) {
	t.Helper()
	if e.rev == 0 {
		e.rev = 1
	}
	if err := r.AddEquation(e.src, e.rev, e.cover, e.data); err != nil {
		t.Fatalf("AddEquation src=%d cover=%v: unexpected error: %v", e.src, e.cover, err)
	}
}

// ---------- 1. normal path: in-order exactly-once delivery ----------

func TestBasicRecoveryInOrderOnce(t *testing.T) {
	rec := &recorder{}
	r := New(0, 8, rec.hooks())
	P := packets(3, 'A')

	addEq(t, r, eq{src: 1, cover: []uint64{0}, data: P[0]})
	addEq(t, r, eq{src: 2, cover: []uint64{0, 1}, data: xorb(P[0], P[1])})
	addEq(t, r, eq{src: 3, cover: []uint64{1, 2}, data: xorb(P[1], P[2])})

	requireDelivered(t, rec, map[uint64][]byte{0: P[0], 1: P[1], 2: P[2]})
	if ids := rec.deliveredIDs(); len(ids) != 3 || ids[0] != 0 || ids[1] != 1 || ids[2] != 2 {
		t.Fatalf("delivery order = %v, want [0 1 2]", ids)
	}
	// No duplicate delivery on further no-op mutations.
	if err := r.SlideTo(0); err != nil {
		t.Fatal(err)
	}
	if len(rec.deliveredIDs()) != 3 {
		t.Fatalf("re-delivered after no-op slide: %v", rec.deliveredIDs())
	}
}

// ---------- 2. unique solution before the window is full rank ----------

func TestEarlyUniqueSolveDoesNotWaitForFullWindow(t *testing.T) {
	rec := &recorder{}
	r := New(0, 16, rec.hooks()) // window far larger than 3 packets
	P := packets(3, 'B')

	// Three GF(2)-independent covers: 110, 011, 111.
	addEq(t, r, eq{src: 1, cover: []uint64{0, 1}, data: xorb(P[0], P[1])})
	addEq(t, r, eq{src: 2, cover: []uint64{1, 2}, data: xorb(P[1], P[2])})
	addEq(t, r, eq{src: 3, cover: []uint64{0, 1, 2}, data: func() []byte {
		return xorb(xorb(P[0], P[1]), P[2])
	}()})

	requireDelivered(t, rec, map[uint64][]byte{0: P[0], 1: P[1], 2: P[2]})
	if rank := len(r.rows); rank != 3 {
		t.Fatalf("basis rank = %d, want 3 (window size 16, must not wait for full rank)", rank)
	}
}

// ---------- 3. solved-but-undelivered packet participates ----------

func TestSolvedBehindGapParticipatesInElimination(t *testing.T) {
	rec := &recorder{}
	r := New(0, 8, rec.hooks())
	P := packets(4, 'C')

	// {1,2} and {2}: packet 1 is uniquely solved but cannot be
	// delivered because packet 0 is missing entirely.
	addEq(t, r, eq{src: 1, cover: []uint64{1, 2}, data: xorb(P[1], P[2])})
	addEq(t, r, eq{src: 2, cover: []uint64{2}, data: P[2]})

	if v, ok := r.Solved(1); !ok || !bytes.Equal(v, P[1]) {
		t.Fatalf("packet 1 should be solved behind gap, ok=%v", ok)
	}
	if ids := rec.deliveredIDs(); len(ids) != 0 {
		t.Fatalf("packet 1 delivered out of contiguous order: %v", ids)
	}

	// New equation {1,3} is reduced with the solved row for 1,
	// immediately recovering 3 even though delivery stays blocked.
	addEq(t, r, eq{src: 3, cover: []uint64{1, 3}, data: xorb(P[1], P[3])})
	if v, ok := r.Solved(3); !ok || !bytes.Equal(v, P[3]) {
		t.Fatalf("packet 3 should be solved via packet 1 row, ok=%v", ok)
	}
	if ids := rec.deliveredIDs(); len(ids) != 0 {
		t.Fatalf("delivery should still be blocked at 0: %v", ids)
	}

	// Finally 0 arrives: contiguous delivery 0,1,2,3 and 1 is only
	// delivered once.
	addEq(t, r, eq{src: 4, cover: []uint64{0}, data: P[0]})
	requireDelivered(t, rec, map[uint64][]byte{0: P[0], 1: P[1], 2: P[2], 3: P[3]})
	count1 := 0
	for _, id := range rec.deliveredIDs() {
		if id == 1 {
			count1++
		}
	}
	if count1 != 1 {
		t.Fatalf("packet 1 delivered %d times, want exactly once", count1)
	}
}

// ---------- 4. sliding old column keeps derived B xor C ----------

func TestSlideKeepsDerivedEquation(t *testing.T) {
	rec := &recorder{}
	r := New(0, 3, rec.hooks())
	P := [][]byte{{0x01, 0x02}, {0x03, 0x04}, {0x05, 0x06}}

	// Only A xor B and A xor C.
	addEq(t, r, eq{src: 1, cover: []uint64{0, 1}, data: xorb(P[0], P[1])})
	addEq(t, r, eq{src: 2, cover: []uint64{0, 2}, data: xorb(P[0], P[2])})

	if err := r.SlideTo(1); err != nil {
		t.Fatal(err)
	}
	// Exactly one derived row must survive: B xor C = P1 xor P2.
	found := false
	for _, rw := range r.rows {
		if len(rw.coeffs) == 2 && rw.coeffs.has(1) && rw.coeffs.has(2) &&
			bytes.Equal(rw.rhs, xorb(P[1], P[2])) {
			found = true
		}
	}
	if !found {
		t.Fatalf("derived B xor C equation missing after slide; rows=%+v", r.rows)
	}

	// B arrives, so C is recovered through the retained equation.
	addEq(t, r, eq{src: 3, cover: []uint64{1}, data: P[1]})
	requireDelivered(t, rec, map[uint64][]byte{1: P[1], 2: P[2]})

	// Discarded bodies must not be re-received.
	err := r.AddEquation(9, 1, []uint64{0}, P[0])
	if !errors.Is(err, ErrVarDiscarded) {
		t.Fatalf("insert covering discarded var: err=%v, want ErrVarDiscarded", err)
	}
}

// ---------- 5. duplicates do not increase rank ----------

func TestDuplicateDoesNotIncreaseRank(t *testing.T) {
	rec := &recorder{}
	r := New(0, 4, rec.hooks())
	P := packets(2, 'D')

	addEq(t, r, eq{src: 1, cover: []uint64{0, 1}, data: xorb(P[0], P[1])})
	addEq(t, r, eq{src: 2, cover: []uint64{0, 1}, data: xorb(P[0], P[1])}) // exact dup
	if len(r.rows) != 1 {
		t.Fatalf("rank after duplicate = %d, want 1", len(r.rows))
	}
	if _, ok := r.Solved(0); ok {
		t.Fatal("packet 0 should not be solved by one XOR equation + duplicate")
	}

	addEq(t, r, eq{src: 3, cover: []uint64{1}, data: P[1]})
	requireDelivered(t, rec, map[uint64][]byte{0: P[0], 1: P[1]})
}

// ---------- 6. contradiction returns sources and fails atomically ----------

func TestContradictionReportsSourcesAndIsAtomic(t *testing.T) {
	rec := &recorder{}
	r := New(0, 4, rec.hooks())
	P0 := []byte("original-zero")
	P0bad := []byte("tampered-zero!")

	addEq(t, r, eq{src: 1, cover: []uint64{0}, data: P0})
	err := r.AddEquation(2, 1, []uint64{0}, P0bad)
	var ce *ErrContradiction
	if !errors.As(err, &ce) {
		t.Fatalf("expected ErrContradiction, got %v", err)
	}
	if fmt.Sprint(ce.Sources) != "[1 2]" {
		t.Fatalf("contradiction sources = %v, want [1 2]", ce.Sources)
	}
	// Atomic: original value still solved and delivered.
	requireDelivered(t, rec, map[uint64][]byte{0: P0})
	if v, _ := r.Solved(0); !bytes.Equal(v, P0) {
		t.Fatalf("state mutated by failed insert: packet0=%x", v)
	}

	// Multi-source contradiction through elimination.
	r2 := New(0, 4, (&recorder{}).hooks())
	addEq(t, r2, eq{src: 1, cover: []uint64{0, 1}, data: xorb(P0, []byte("pp"))})
	addEq(t, r2, eq{src: 2, cover: []uint64{1}, data: []byte("pp")})
	err = r2.AddEquation(3, 1, []uint64{0}, []byte("wrong-value!!"))
	var ce2 *ErrContradiction
	if !errors.As(err, &ce2) {
		t.Fatalf("expected multi-source ErrContradiction, got %v", err)
	}
	if fmt.Sprint(ce2.Sources) != "[1 2 3]" {
		t.Fatalf("contradiction sources = %v, want [1 2 3]", ce2.Sources)
	}
}

// ---------- 7. variable payload lengths: pad zero and truncate ----------

func TestVariablePayloadLengths(t *testing.T) {
	rec := &recorder{}
	r := New(0, 8, rec.hooks())
	r.SetMeta(0, 3) // short original
	r.SetMeta(1, 5) // longer original
	r.SetMeta(2, 6) // payload shorter than metadata -> zero tail

	P0 := []byte("abc")
	P1 := []byte("HELLO")
	padded0 := align(P0, 5)

	addEq(t, r, eq{src: 1, cover: []uint64{0, 1}, data: xorb(padded0, P1)})
	addEq(t, r, eq{src: 2, cover: []uint64{1}, data: P1})
	addEq(t, r, eq{src: 3, cover: []uint64{2}, data: []byte{0x10, 0x20, 0x30, 0x40}})

	want2 := []byte{0x10, 0x20, 0x30, 0x40, 0x00, 0x00}
	requireDelivered(t, rec, map[uint64][]byte{
		0: P0, // truncated from 5 to 3
		1: P1,
		2: want2, // padded from 4 to 6
	})
}

// ---------- 8. checkpoint + reversed arrival order determinism ----------

func TestCheckpointReverseOrderSameOutcome(t *testing.T) {
	P := packets(3, 'E')
	e1 := eq{src: 1, cover: []uint64{0, 1}, data: xorb(P[0], P[1])}
	e2 := eq{src: 2, cover: []uint64{1, 2}, data: xorb(P[1], P[2])}
	e3 := eq{src: 3, cover: []uint64{2}, data: P[2]}
	e4 := eq{src: 4, cover: []uint64{0, 2}, data: xorb(P[0], P[2])}
	e5 := eq{src: 5, cover: []uint64{1}, data: P[1]}

	// Run A: e1,e2 then checkpoint, then e3,e4,e5 in order.
	recA := &recorder{}
	rA := New(0, 8, recA.hooks())
	addEq(t, rA, e1)
	addEq(t, rA, e2)
	cp, err := rA.Checkpoint()
	if err != nil {
		t.Fatal(err)
	}
	addEq(t, rA, e3)
	addEq(t, rA, e4)
	addEq(t, rA, e5)

	// Run B: restore checkpoint, remaining equations arrive reversed.
	recB := &recorder{}
	rB, err := Restore(cp, recB.hooks())
	if err != nil {
		t.Fatal(err)
	}
	addEq(t, rB, e5)
	addEq(t, rB, e4)
	addEq(t, rB, e3)

	if fmt.Sprint(recA.events) != fmt.Sprint(recB.events) {
		t.Fatalf("event sequences differ under reversed arrival order:\nA=%+v\nB=%+v",
			recA.events, recB.events)
	}
	requireDelivered(t, recB, map[uint64][]byte{0: P[0], 1: P[1], 2: P[2]})

	// Run C: checkpoint round-trip again must keep working.
	cp2, err := rB.Checkpoint()
	if err != nil {
		t.Fatal(err)
	}
	rC, err := Restore(cp2, (&recorder{}).hooks())
	if err != nil {
		t.Fatal(err)
	}
	if v, ok := rC.Solved(0); !ok || !bytes.Equal(v, P[0]) {
		t.Fatalf("restored solved(0) mismatch ok=%v", ok)
	}
}

// ---------- 9. retraction sustained by duplicate equation ----------

func TestRetractionSustainedByOtherEquation(t *testing.T) {
	rec := &recorder{}
	r := New(0, 4, rec.hooks())
	P0 := []byte("same-value!!")

	addEq(t, r, eq{src: 10, cover: []uint64{0}, data: P0})
	addEq(t, r, eq{src: 11, cover: []uint64{0}, data: P0}) // duplicate

	if err := r.Retract(10); err != nil {
		t.Fatalf("retract redundant source: %v", err)
	}
	if r.Frozen() {
		t.Fatal("receiver should not be frozen; duplicate sustains solution")
	}
	if v, ok := r.Solved(0); !ok || !bytes.Equal(v, P0) {
		t.Fatalf("packet 0 not sustained after retraction, ok=%v", ok)
	}
	requireDelivered(t, rec, map[uint64][]byte{0: P0})
}

// ---------- 10. retraction makes unique solution uncertain again ----------

func TestRetractionUnsolvesUnconfirmedPacket(t *testing.T) {
	rec := &recorder{}
	r := New(0, 8, rec.hooks())
	P := packets(2, 'F')

	// Packet 0 is never covered -> contiguous prefix blocked at 0.
	addEq(t, r, eq{src: 10, cover: []uint64{1, 2}, data: xorb(P[1], P[1])}) // {1,2}=0
	addEq(t, r, eq{src: 11, cover: []uint64{2}, data: P[1]})

	if v, ok := r.Solved(1); !ok || !bytes.Equal(v, P[1]) {
		t.Fatalf("packet1 should be solved behind gap, ok=%v", ok)
	}
	if len(rec.deliveredIDs()) != 0 {
		t.Fatalf("nothing should be delivered while 0 missing: %v", rec.deliveredIDs())
	}

	// Retract the equation anchoring packet 2: packet 1 becomes
	// uncertain again; no confirmation exists so no conflict.
	if err := r.Retract(11); err != nil {
		t.Fatalf("retract unconfirmed anchor: %v", err)
	}
	if _, ok := r.Solved(1); ok {
		t.Fatal("packet1 should be unsolved after retraction")
	}

	// Re-anchor with a fresh source: solved again.
	addEq(t, r, eq{src: 13, cover: []uint64{2}, data: P[1]})
	if v, ok := r.Solved(1); !ok || !bytes.Equal(v, P[1]) {
		t.Fatalf("packet1 should be solved again, ok=%v", ok)
	}

	// Explicitly abandon the missing packet 0: one gap notification,
	// then 1 and 2 are delivered.
	r.Abandon(1)
	if gaps := rec.gaps(); len(gaps) != 1 || gaps[0] != [2]uint64{0, 1} {
		t.Fatalf("gaps = %v, want [[0 1]]", gaps)
	}
	requireDelivered(t, rec, map[uint64][]byte{1: P[1], 2: P[1]})
}

// ---------- 11. confirmed bytes conflict -> minimal sources + freeze ----------

func TestConfirmedConflictReturnsSourcesAndFreezes(t *testing.T) {
	rec := &recorder{}
	r := New(0, 4, rec.hooks())
	P0 := []byte("confirmed!!")

	addEq(t, r, eq{src: 10, cover: []uint64{0}, data: P0})
	requireDelivered(t, rec, map[uint64][]byte{0: P0})

	// Slide 0 out: its proof becomes a w fact.
	if err := r.SlideTo(1); err != nil {
		t.Fatal(err)
	}

	err := r.Retract(10)
	var ec *ErrConflict
	if !errors.As(err, &ec) {
		t.Fatalf("expected ErrConflict, got %v", err)
	}
	if ec.Packet != 0 || fmt.Sprint(ec.Sources) != "[10]" {
		t.Fatalf("conflict = packet %d sources %v, want packet 0 sources [10]", ec.Packet, ec.Sources)
	}
	if !r.Frozen() {
		t.Fatal("generation should be frozen")
	}
	if !errors.Is(r.AddEquation(12, 1, []uint64{1}, []byte("x")), ErrFrozen) {
		t.Fatal("insert after freeze must return ErrFrozen")
	}
	if !errors.Is(r.Retract(11), ErrFrozen) {
		t.Fatal("retract after freeze must return ErrFrozen")
	}
	// Pre-conflict state is retained: checkpoint still decodes.
	if _, err := r.Checkpoint(); err != nil {
		t.Fatal(err)
	}
}

func TestConfirmedConflictSustainedThenFrozen(t *testing.T) {
	rec := &recorder{}
	r := New(0, 4, rec.hooks())
	P0 := []byte("redundant!!")

	addEq(t, r, eq{src: 10, cover: []uint64{0}, data: P0})
	addEq(t, r, eq{src: 11, cover: []uint64{0}, data: P0})
	requireDelivered(t, rec, map[uint64][]byte{0: P0})
	if err := r.SlideTo(1); err != nil {
		t.Fatal(err)
	}

	// Retracting 10 is sustained by 11 via the z dependency.
	if err := r.Retract(10); err != nil {
		t.Fatalf("retract 10 should be sustained: %v", err)
	}
	if r.Frozen() {
		t.Fatal("should not freeze while another source proves the byte")
	}
	// Retracting 11 now destroys the only proof -> conflict/freeze.
	err := r.Retract(11)
	var ec *ErrConflict
	if !errors.As(err, &ec) {
		t.Fatalf("expected ErrConflict on last proof, got %v", err)
	}
	if fmt.Sprint(ec.Sources) != "[11]" {
		t.Fatalf("sources = %v, want [11]", ec.Sources)
	}
	if !r.Frozen() {
		t.Fatal("expected frozen")
	}
}

// ---------- 12. interleave new equations/retractions after slide +
//              checkpoint/restore; discarded bodies not re-received ----------

func TestInterleavedRetractInsertCheckpoint(t *testing.T) {
	P0 := []byte{0xA0, 0x01}
	P1 := []byte{0xB1, 0x02}
	P2 := []byte{0xC2, 0x03}

	buildCheckpoint := func() []byte {
		rec := &recorder{}
		r := New(0, 4, rec.hooks())
		r.SetMeta(0, 2)
		r.SetMeta(1, 2)
		r.SetMeta(2, 2)
		r.SetMeta(3, 2)

		addEq(t, r, eq{src: 1, cover: []uint64{0, 1}, data: xorb(P0, P1)})
		addEq(t, r, eq{src: 2, cover: []uint64{0}, data: P0})
		// 1 is now recovered and delivered as P1.
		requireDelivered(t, rec, map[uint64][]byte{0: P0, 1: P1})

		// Consistent retransmission of 1: creates z dependency
		// {1,2,3}.
		addEq(t, r, eq{src: 3, cover: []uint64{1}, data: P1})

		if err := r.SlideTo(2); err != nil {
			t.Fatal(err)
		}
		cp, err := r.Checkpoint()
		if err != nil {
			t.Fatal(err)
		}
		return cp
	}

	// Branch A: retract old-window source 1 (redundant via z), then
	// insert new-window equation, then retract source 2 which kills
	// confirmed packet 0's proof.
	t.Run("retract-redundant-then-conflict", func(t *testing.T) {
		cp := buildCheckpoint()
		r, err := Restore(cp, (&recorder{}).hooks())
		if err != nil {
			t.Fatal(err)
		}

		// Discarded body cannot be re-received after restore.
		if err := r.AddEquation(99, 1, []uint64{0}, P0); !errors.Is(err, ErrVarDiscarded) {
			t.Fatalf("want ErrVarDiscarded, got %v", err)
		}

		if err := r.Retract(1); err != nil {
			t.Fatalf("retract redundant old-window source: %v", err)
		}
		if r.Frozen() {
			t.Fatal("redundant retraction must not freeze")
		}

		rec2 := &recorder{}
		r.hooks = rec2.hooks()
		addEq(t, r, eq{src: 4, cover: []uint64{2}, data: P2})
		requireDelivered(t, rec2, map[uint64][]byte{2: P2})

		err = r.Retract(2)
		var ec *ErrConflict
		if !errors.As(err, &ec) {
			t.Fatalf("want ErrConflict, got %v", err)
		}
		if ec.Packet != 0 {
			t.Fatalf("conflict packet = %d, want 0", ec.Packet)
		}
		if !contains(ec.Sources, 2) {
			t.Fatalf("conflict sources %v must contain retracted source 2", ec.Sources)
		}
		if !r.Frozen() {
			t.Fatal("expected freeze")
		}
	})

	// Branch B: retract source 2 first (z rewrites proofs so both
	// confirmed bytes survive through other sources), then recover
	// new window, then retract source 1 and hit the conflict whose
	// minimal proof is [1,3].
	t.Run("z-rewrite-sustains-then-proof-conflict", func(t *testing.T) {
		cp := buildCheckpoint()
		r, err := Restore(cp, (&recorder{}).hooks())
		if err != nil {
			t.Fatal(err)
		}

		if err := r.Retract(2); err != nil {
			t.Fatalf("z rewrite should sustain confirmed bytes: %v", err)
		}
		if r.Frozen() {
			t.Fatal("should stay generation-consistent after z rewrite")
		}

		rec2 := &recorder{}
		r.hooks = rec2.hooks()
		addEq(t, r, eq{src: 4, cover: []uint64{2}, data: P2})
		requireDelivered(t, rec2, map[uint64][]byte{2: P2})

		err = r.Retract(1)
		var ec *ErrConflict
		if !errors.As(err, &ec) {
			t.Fatalf("want ErrConflict on proof [1,3], got %v", err)
		}
		if fmt.Sprint(ec.Sources) != "[1 3]" {
			t.Fatalf("minimal conflict sources = %v, want [1 3]", ec.Sources)
		}
		if ec.Packet != 0 {
			t.Fatalf("conflict packet = %d, want 0", ec.Packet)
		}
	})
}

func contains(xs []uint64, x uint64) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// ---------- 13. gap notifications: explicit abandon and slide-over ----------

func TestGapNotifications(t *testing.T) {
	rec := &recorder{}
	r := New(0, 4, rec.hooks())
	P1 := []byte("one!")

	addEq(t, r, eq{src: 1, cover: []uint64{1}, data: P1})
	r.Abandon(1)
	requireDelivered(t, rec, map[uint64][]byte{1: P1})

	// Abandoning again up to same point is a no-op (no duplicate gap).
	r.Abandon(1)

	// Slide crosses unsolved packet 2: exactly one more gap.
	if err := r.SlideTo(3); err != nil {
		t.Fatal(err)
	}

	gaps := rec.gaps()
	if len(gaps) != 2 {
		t.Fatalf("gaps = %v, want two notifications", gaps)
	}
	if gaps[0] != [2]uint64{0, 1} {
		t.Fatalf("first gap = %v, want [0 1]", gaps[0])
	}
	if gaps[1] != [2]uint64{2, 3} {
		t.Fatalf("second gap = %v, want [2 3]", gaps[1])
	}
}

// ---------- 14. revisions and error edges ----------

func TestRevisionAndErrorEdges(t *testing.T) {
	rec := &recorder{}
	r := New(0, 4, rec.hooks())
	P0 := []byte("rev1-data!!")
	P1 := []byte("rev2-other")

	addEq(t, r, eq{src: 7, rev: 1, cover: []uint64{1, 2}, data: xorb(P1, P1)})
	if err := r.AddEquation(7, 0, []uint64{1}, P1); !errors.Is(err, ErrStaleRevision) {
		t.Fatalf("old revision err=%v, want ErrStaleRevision", err)
	}
	// Same revision with a rank-increasing different equation.
	if err := r.AddEquation(7, 1, []uint64{1}, P1); !errors.Is(err, ErrRevisionConflict) {
		t.Fatalf("same rev different eq err=%v, want ErrRevisionConflict", err)
	}
	// New revision replaces old equation.
	if err := r.AddEquation(7, 2, []uint64{1}, P1); err != nil {
		t.Fatalf("newer revision replace: %v", err)
	}
	if v, ok := r.Solved(1); !ok || !bytes.Equal(v, P1) {
		t.Fatalf("after revision replace packet1=%x ok=%v", v, ok)
	}

	if err := r.Retract(404); !errors.Is(err, ErrUnknownSource) {
		t.Fatalf("retract unknown err=%v, want ErrUnknownSource", err)
	}
	if err := r.AddEquation(8, 1, []uint64{9}, P0); !errors.Is(err, ErrOutOfWindow) {
		t.Fatalf("out of window err=%v, want ErrOutOfWindow", err)
	}
}

// ---------- 16. checkpoint + reversed order with prior gap & slide ----------

func TestCheckpointReverseOrderWithGapAndSlide(t *testing.T) {
	P := packets(8, 'G')

	build := func(h Hooks) *Receiver {
		r := New(0, 8, h)
		addEq(t, r, eq{src: 1, cover: []uint64{1, 2}, data: xorb(P[1], P[2])})
		addEq(t, r, eq{src: 2, cover: []uint64{3}, data: P[3]})
		r.Abandon(1) // gap [0,1), nothing deliverable yet
		addEq(t, r, eq{src: 3, cover: []uint64{2}, data: P[2]})
		addEq(t, r, eq{src: 4, cover: []uint64{4, 5}, data: xorb(P[4], P[5])})
		addEq(t, r, eq{src: 5, cover: []uint64{5}, data: P[5]})
		if err := r.SlideTo(4); err != nil {
			t.Fatal(err)
		}
		return r
	}

	base := build((&recorder{}).hooks())
	cp, err := base.Checkpoint()
	if err != nil {
		t.Fatal(err)
	}

	f1 := eq{src: 6, cover: []uint64{6, 7}, data: xorb(P[6], P[7])}
	f2 := eq{src: 7, cover: []uint64{7}, data: P[7]}
	f3 := eq{src: 8, cover: []uint64{4}, data: P[4]}
	f4 := eq{src: 9, cover: []uint64{4, 6}, data: xorb(P[4], P[6])}

	recA := &recorder{}
	rA, err := Restore(cp, recA.hooks())
	if err != nil {
		t.Fatal(err)
	}
	addEq(t, rA, f1)
	addEq(t, rA, f2)
	addEq(t, rA, f3)
	addEq(t, rA, f4)
	rA.Abandon(9)

	recB := &recorder{}
	rB, err := Restore(cp, recB.hooks())
	if err != nil {
		t.Fatal(err)
	}
	// Same window, equation arrival fully reversed.
	addEq(t, rB, f4)
	addEq(t, rB, f3)
	addEq(t, rB, f2)
	addEq(t, rB, f1)
	rB.Abandon(9)

	if fmt.Sprint(recA.events) != fmt.Sprint(recB.events) {
		t.Fatalf("continuation event streams differ:\nA=%+v\nB=%+v", recA.events, recB.events)
	}
	// Restored recorders only observe post-checkpoint deliveries;
	// the 1..5 prefix is frozen inside the checkpoint and is not
	// replayed (covered by TestCheckpointPreservesGapAndPrefix).
	wantPost := map[uint64][]byte{6: P[6], 7: P[7]}
	requireDelivered(t, recA, wantPost)
	requireDelivered(t, recB, wantPost)
	if gs := recA.gaps(); len(gs) != 1 || gs[0] != [2]uint64{8, 9} {
		t.Fatalf("continuation gaps = %v, want [[8 9]]", gs)
	}
	if gs := recB.gaps(); len(gs) != 1 || gs[0] != [2]uint64{8, 9} {
		t.Fatalf("reversed continuation gaps = %v, want [[8 9]]", gs)
	}

	// Solved state over the live window must be identical too.
	for id := uint64(4); id < 12; id++ {
		va, oa := rA.Solved(id)
		vb, ob := rB.Solved(id)
		if oa != ob || (oa && !bytes.Equal(va, vb)) {
			t.Fatalf("solved(%d) differs after reverse order: (%x,%v) vs (%x,%v)", id, va, oa, vb, ob)
		}
	}
}

// ---------- 17. source id reuse after retraction + empty cover edges ----------

func TestRetractThenReuseSourceID(t *testing.T) {
	rec := &recorder{}
	r := New(0, 4, rec.hooks())
	P0 := []byte("zero-data!")
	P1 := []byte("one--data!")

	addEq(t, r, eq{src: 5, cover: []uint64{0, 1}, data: xorb(P0, P1)})
	addEq(t, r, eq{src: 6, cover: []uint64{1}, data: P1})
	requireDelivered(t, rec, map[uint64][]byte{0: P0, 1: P1})

	// Retract the anchor: confirmed prefix conflicts -> freeze.
	err := r.Retract(6)
	var ec *ErrConflict
	if !errors.As(err, &ec) {
		t.Fatalf("expected conflict retracting confirmed anchor, got %v", err)
	}
	if !r.Frozen() {
		t.Fatal("expected freeze")
	}
}

func TestRetractUnconfirmedThenReuseSourceID(t *testing.T) {
	rec := &recorder{}
	r := New(0, 4, rec.hooks())
	P2 := []byte("two-data!!")

	// Packet 0 blocks delivery; 2 is solved behind the gap.
	addEq(t, r, eq{src: 5, cover: []uint64{2}, data: P2})
	if _, ok := r.Solved(2); !ok {
		t.Fatal("packet 2 should be solved")
	}
	if err := r.Retract(5); err != nil {
		t.Fatalf("retract unconfirmed sole source: %v", err)
	}
	if _, ok := r.Solved(2); ok {
		t.Fatal("packet 2 should be unsolved after retraction")
	}
	if err := r.AddEquation(5, 7, []uint64{2}, P2); err != nil {
		t.Fatalf("reuse source id with new revision after retract: %v", err)
	}
	if v, ok := r.Solved(2); !ok || !bytes.Equal(v, P2) {
		t.Fatalf("packet2 not recovered after source reuse, ok=%v", ok)
	}
	r.Abandon(2)
	requireDelivered(t, rec, map[uint64][]byte{2: P2})
}

func TestEmptyCoverContradiction(t *testing.T) {
	r := New(0, 4, (&recorder{}).hooks())
	// Empty cover with zero payload is a trivial zero dependency.
	if err := r.AddEquation(1, 1, nil, nil); err != nil {
		t.Fatalf("zero equation should be accepted as duplicate: %v", err)
	}
	// Empty cover with non-zero payload is an immediate contradiction
	// whose source set is exactly the new source.
	err := r.AddEquation(2, 1, nil, []byte{0x01})
	var ce *ErrContradiction
	if !errors.As(err, &ce) {
		t.Fatalf("want ErrContradiction, got %v", err)
	}
	if fmt.Sprint(ce.Sources) != "[2]" {
		t.Fatalf("sources = %v, want [2]", ce.Sources)
	}
	if err := r.Retract(2); !errors.Is(err, ErrUnknownSource) {
		t.Fatalf("failed insert must not register source, retract err=%v", err)
	}
}

// ---------- 15. checkpoint/restore preserves gap + committed prefix ----------

func TestCheckpointPreservesGapAndPrefix(t *testing.T) {
	rec := &recorder{}
	r := New(5, 8, rec.hooks()) // window [5,13)
	P6 := []byte("six-six")

	addEq(t, r, eq{src: 1, cover: []uint64{6}, data: P6})
	r.Abandon(6) // gap [5,6), then deliver 6
	if gaps := rec.gaps(); len(gaps) != 1 {
		t.Fatalf("gaps before checkpoint: %v", gaps)
	}

	cp, err := r.Checkpoint()
	if err != nil {
		t.Fatal(err)
	}
	rec2 := &recorder{}
	r2, err := Restore(cp, rec2.hooks())
	if err != nil {
		t.Fatal(err)
	}
	if r2.DeliveredUpTo() != 7 {
		t.Fatalf("deliverPtr after restore = %d, want 7", r2.DeliveredUpTo())
	}
	// No callbacks replayed on restore.
	if len(rec2.events) != 0 {
		t.Fatalf("restore must not replay callbacks: %+v", rec2.events)
	}
	// State continues consistently.
	if v, ok := r2.Solved(6); !ok || !bytes.Equal(v, P6) {
		t.Fatalf("solved(6) after restore ok=%v", ok)
	}
	r2.Abandon(7) // no-op relative to deliverPtr? deliverPtr=7 -> no gap
	if len(rec2.gaps()) != 0 {
		t.Fatalf("unexpected gap after restore: %v", rec2.gaps())
	}
}
