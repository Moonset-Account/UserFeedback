package fecrx

import (
	"bytes"
	"errors"
	"fmt"
	"math/rand"
	"strings"
	"testing"
)

// ---------- brute-force reference over ALL variables ----------

type refEq struct {
	mask uint64 // bit i == coefficient of variable i
	rhs  []byte
}

type refRow struct {
	mask uint64
	rhs  []byte
}

// bruteRREF returns the RREF of all live equations over the full
// variable universe (no columns are ever discarded).
func bruteRREF(live map[uint64]refEq, nvars, length int) []refRow {
	var out []refRow
	pivotAt := map[int]int{}

	insert := func(mask uint64, rhs []byte) {
		for c := 0; c < nvars; c++ {
			if (mask>>uint(c))&1 == 1 {
				if idx, ok := pivotAt[c]; ok {
					mask ^= out[idx].mask
					rhs = xorBytes(rhs, out[idx].rhs)
				}
			}
		}
		if mask == 0 {
			return
		}
		p := firstTrailing(mask)
		for i := range out {
			if (out[i].mask>>uint(p))&1 == 1 {
				out[i].mask ^= mask
				out[i].rhs = xorBytes(out[i].rhs, rhs)
			}
		}
		pivotAt[p] = len(out)
		out = append(out, refRow{mask: mask, rhs: append([]byte(nil), rhs...)})
	}

	for _, e := range live {
		insert(e.mask, append([]byte(nil), e.rhs...))
	}
	return out
}

func firstTrailing(m uint64) int {
	for i := 0; i < 64; i++ {
		if (m>>uint(i))&1 == 1 {
			return i
		}
	}
	return 64
}

func bruteUnique(rows []refRow, id, length int) ([]byte, bool) {
	wantMask := uint64(1) << uint(id)
	for _, r := range rows {
		if r.mask == wantMask {
			return align(r.rhs, length), true
		}
	}
	return nil, false
}

// bruteConflict returns the smallest delivered id that the post-
// retraction system no longer proves with its delivered value.
func bruteConflict(live map[uint64]refEq, delivered map[uint64][]byte, nvars, length int) (uint64, bool) {
	rows := bruteRREF(live, nvars, length)
	for _, id := range sortedKeys(delivered) {
		v, ok := bruteUnique(rows, int(id), length)
		if !ok || !bytes.Equal(v, delivered[id]) {
			return id, true
		}
	}
	return 0, false
}

// xorSources computes XOR of the named reference equations.
func xorSources(live map[uint64]refEq, srcs []uint64) (uint64, []byte) {
	var mask uint64
	var rhs []byte
	for _, s := range srcs {
		e, ok := live[s]
		if !ok {
			// Could be a source retracted earlier in the run; tests
			// only verify conflict sets against pre-retraction live.
			return 0, nil
		}
		mask ^= e.mask
		rhs = xorBytes(rhs, e.rhs)
	}
	return mask, rhs
}

// ---------- randomized differential test ----------

func TestDifferentialRandomizedSlidingRetractions(t *testing.T) {
	const nvars = 10
	const length = 5
	const windowSize = uint64(4)

	for seed := int64(0); seed < 300; seed++ {
		rng := rand.New(rand.NewSource(seed))

		P := make([][]byte, nvars)
		for i := range P {
			P[i] = make([]byte, length)
			rng.Read(P[i])
		}

		rec := &recorder{}
		base := uint64(0)
		r := New(base, windowSize, rec.hooks())
		for i := 0; i < nvars; i++ {
			r.SetMeta(uint64(i), length)
		}

		live := map[uint64]refEq{}
		revs := map[uint64]uint64{}
		var liveSrcs []uint64
		nextSrc := uint64(1)
		frozen := false
		var hist []string

		// check compares library solved-state with brute force.
		check := func(stage string) {
			t.Helper()
			rows := bruteRREF(live, nvars, length)
			for id := base; id < base+windowSize; id++ {
				bv, bok := bruteUnique(rows, int(id), length)
				lv, lok := r.Solved(id)
				if bok != lok {
					t.Fatalf("seed=%d %s id=%d solved mismatch: lib=%v brute=%v (live=%d)\nHISTORY:\n%s",
						seed, stage, id, lok, bok, len(live), strings.Join(hist, "\n"))
				}
				if bok && !bytes.Equal(bv, lv) {
					t.Fatalf("seed=%d %s id=%d value mismatch: lib=%x brute=%x",
						seed, stage, id, lv, bv)
				}
				if lok && !bytes.Equal(lv, P[id]) {
					t.Fatalf("seed=%d %s id=%d lib solved to wrong ground-truth packet: %x vs %x",
						seed, stage, id, lv, P[id])
				}
			}
			// Every delivered byte must be the true packet, and
			// deliveries exactly once / strictly increasing.
			seen := map[uint64]int{}
			var last int64 = -1
			for _, e := range rec.events {
				if e.kind != "deliver" {
					continue
				}
				seen[e.a]++
				if int64(e.a) <= last {
					t.Fatalf("seed=%d %s delivery order violation at %d (last=%d)", seed, stage, e.a, last)
				}
				last = int64(e.a)
				if seen[e.a] > 1 {
					t.Fatalf("seed=%d %s duplicate delivery of %d", seed, stage, e.a)
				}
				if !bytes.Equal(e.data, P[e.a]) {
					t.Fatalf("seed=%d %s delivered wrong bytes for %d", seed, stage, e.a)
				}
			}
		}

		// checkpoint occasionally; restore into a fresh receiver.
		maybeCheckpoint := func() {
			if rng.Intn(4) != 0 || frozen {
				return
			}
			cp, err := r.Checkpoint()
			if err != nil {
				t.Fatalf("seed=%d checkpoint: %v", seed, err)
			}
			r2, err := Restore(cp, rec.hooks())
			if err != nil {
				t.Fatalf("seed=%d restore: %v", seed, err)
			}
			r = r2
			hist = append(hist, "CHECKPOINT-RESTORE")
		}

	Loop:
		for step := 0; step < 80 && !frozen; step++ {
			op := rng.Intn(10)
			switch {
			case op <= 4 || len(liveSrcs) == 0:
				// insert a new truthful equation over a random
				// non-empty subset of the current window.
				lo, hi := base, base+windowSize
				var cover []uint64
				mask := uint64(0)
				for id := lo; id < hi; id++ {
					if rng.Intn(2) == 0 {
						cover = append(cover, id)
						mask |= uint64(1) << id
					}
				}
				if len(cover) == 0 {
					id := lo + uint64(rng.Intn(int(windowSize)))
					cover = []uint64{id}
					mask = uint64(1) << id
				}
				rhs := []byte{}
				for _, id := range cover {
					rhs = xorBytes(rhs, P[id])
				}
				src := nextSrc
				nextSrc++
				if err := r.AddEquation(src, 1, cover, rhs); err != nil {
					t.Fatalf("seed=%d step=%d truthful insert src=%d cover=%v: %v",
						seed, step, src, cover, err)
				}
				live[src] = refEq{mask: mask, rhs: append([]byte(nil), rhs...)}
				revs[src] = 1
				liveSrcs = append(liveSrcs, src)
				hist = append(hist, fmt.Sprintf("INSERT src=%d cover=%v mask=%010b", src, cover, mask))

			case op == 5 && base+1 <= uint64(nvars)-windowSize:
				// slide one variable
				if err := r.SlideTo(base + 1); err != nil {
					t.Fatalf("seed=%d slide: %v", seed, err)
				}
				hist = append(hist, fmt.Sprintf("SLIDE %d->%d", base, base+1))
				base++

			case op == 6:
				// explicit abandon inside window
				dp := r.DeliveredUpTo()
				limit := base + windowSize
				if dp < limit {
					upTo := dp + 1 + uint64(rng.Intn(int(limit-dp)))
					if upTo > limit {
						upTo = limit
					}
					r.Abandon(upTo)
					hist = append(hist, fmt.Sprintf("ABANDON ->%d", upTo))
				}

			case op == 7 && len(liveSrcs) > 0:
				// retract a random live source; compare conflict
				// semantics against brute force.
				idx := rng.Intn(len(liveSrcs))
				src := liveSrcs[idx]

				removed := live[src]
				delete(live, src)
				wantBad, wantConflict := bruteConflict(live, rec.deliveredData(), nvars, length)

				err := r.Retract(src)
				var ec *ErrConflict
				gotConflict := errors.As(err, &ec)
				if wantConflict != gotConflict {
					t.Fatalf("seed=%d retract %d conflict mismatch: lib=%v err=%v, bruteBad=%v",
						seed, src, gotConflict, err, wantBad)
				}
				if wantConflict {
					if ec.Packet != wantBad {
						t.Fatalf("seed=%d conflict packet=%d, brute smallest bad=%d",
							seed, ec.Packet, wantBad)
					}
					if !r.Frozen() {
						t.Fatalf("seed=%d expected frozen after conflict", seed)
					}
					// The returned minimal source set must XOR to
					// exactly {packet} = delivered value in the
					// PRE-retraction reference system.
					pre := map[uint64]refEq{}
					for k, v := range live {
						pre[k] = v
					}
					pre[src] = removed
					mask, rhs := xorSources(pre, ec.Sources)
					wantMask := uint64(1) << ec.Packet
					if mask != wantMask {
						t.Fatalf("seed=%d conflict sources %v XOR mask=%b, want singleton %d (%b)",
							seed, ec.Sources, mask, ec.Packet, wantMask)
					}
					if !bytes.Equal(align(rhs, length), rec.deliveredData()[ec.Packet]) {
						t.Fatalf("seed=%d conflict sources proof value mismatch: %x vs delivered %x",
							seed, align(rhs, length), rec.deliveredData()[ec.Packet])
					}
					frozen = true
					liveSrcs = append(liveSrcs[:idx], liveSrcs[idx+1:]...)
					break Loop
				}
				if err != nil {
					t.Fatalf("seed=%d unexpected retract error: %v", seed, err)
				}
				hist = append(hist, fmt.Sprintf("RETRACT src=%d", src))
				liveSrcs = append(liveSrcs[:idx], liveSrcs[idx+1:]...)

			case op == 8 && len(liveSrcs) > 0:
				// revision replacement of a random live source
				idx := rng.Intn(len(liveSrcs))
				src := liveSrcs[idx]
				newRev := revs[src] + 1
				id := base + uint64(rng.Intn(int(windowSize)))
				cover := []uint64{id}
				rhs := P[id]
				mask := uint64(1) << id

				trialLive := map[uint64]refEq{}
				for k, v := range live {
					trialLive[k] = v
				}
				delete(trialLive, src)
				wantBad, wantConflict := bruteConflict(trialLive, rec.deliveredData(), nvars, length)

				err := r.AddEquation(src, newRev, cover, rhs)
				var ec *ErrConflict
				gotConflict := errors.As(err, &ec)
				if wantConflict != gotConflict {
					t.Fatalf("seed=%d replace %d conflict mismatch lib=%v err=%v bruteBad=%d",
						seed, src, gotConflict, err, wantBad)
				}
				if wantConflict {
					if ec.Packet != wantBad {
						t.Fatalf("seed=%d replace conflict packet=%d want %d", seed, ec.Packet, wantBad)
					}
					frozen = true
					break Loop
				}
				if err != nil {
					t.Fatalf("seed=%d truthful revision replace failed: %v", seed, err)
				}
				live[src] = refEq{mask: mask, rhs: append([]byte(nil), rhs...)}
				revs[src] = newRev
				hist = append(hist, fmt.Sprintf("REPLACE src=%d rev=%d cover=[%d]", src, newRev, id))
				maybeCheckpoint()
			}

			check("step")
			maybeCheckpoint()
		}

		// Final checkpoint must round-trip and preserve solved state.
		cp, err := r.Checkpoint()
		if err != nil {
			t.Fatalf("seed=%d final checkpoint: %v", seed, err)
		}
		r3, err := Restore(cp, (&recorder{}).hooks())
		if err != nil {
			t.Fatalf("seed=%d final restore: %v", seed, err)
		}
		for id := base; id < base+windowSize; id++ {
			v1, ok1 := r.Solved(id)
			v2, ok2 := r3.Solved(id)
			if ok1 != ok2 || (ok1 && !bytes.Equal(v1, v2)) {
				t.Fatalf("seed=%d restore changed solved(%d): (%x,%v) vs (%x,%v)",
					seed, id, v1, ok1, v2, ok2)
			}
		}
	}
}
