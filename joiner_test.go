package temporaljoin

import (
	"fmt"
	"reflect"
	"sort"
	"sync"
	"testing"
)

// ---------------------------------------------------------------------------
// small assertion helpers (stdlib only)
// ---------------------------------------------------------------------------

func check(t *testing.T, cond bool, msg string, args ...interface{}) {
	t.Helper()
	if !cond {
		t.Fatalf("assertion failed: "+msg, args...)
	}
}

func checkEq(t *testing.T, got, want interface{}, msg string) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%s:\n got: %#v\nwant: %#v", msg, got, want)
	}
}

// rightID returns the version ID of a pairing, or "" for an empty pairing.
func rightID(p Pairing) string {
	if p.Right == nil {
		return ""
	}
	return p.Right.VersionID
}

// summary flattens results into easy-to-assert tuples: kind:leftID:rightID.
func summary(rs []Result) []string {
	out := make([]string, 0, len(rs))
	for _, r := range rs {
		k := "ADD"
		if r.Kind == ResultRetract {
			k = "RET"
		}
		out = append(out, fmt.Sprintf("%s:%s:%s", k, r.Pair.LeftID, rightID(r.Pair)))
	}
	return out
}

func sealedSummary(ss []Sealed) []string {
	out := make([]string, 0, len(ss))
	for _, s := range ss {
		out = append(out, fmt.Sprintf("%s:%s", s.Pair.LeftID, rightID(s.Pair)))
	}
	return out
}

func gcIDs(gs []GCEntry) []string {
	out := make([]string, 0, len(gs))
	for _, g := range gs {
		out = append(out, g.Key+"/"+g.VersionID+"/"+g.Reason)
	}
	sort.Strings(out)
	return out
}

func up(versionID, key string, eff, rev int64) RightRecord {
	return RightRecord{VersionID: versionID, Key: key, EffectiveTime: eff, Revision: rev, Op: RightUpsert, Value: versionID}
}

func ret(versionID, key string, eff int64) RightRecord {
	return RightRecord{VersionID: versionID, Key: key, EffectiveTime: eff, Op: RightRetract}
}

// ---------------------------------------------------------------------------
// Main scenario: one right version covers many left events, then a middle
// version is inserted, then the covering version is retracted; watermarks and
// sealing/GC are checked at every stage.
// ---------------------------------------------------------------------------

func TestMainScenarioCoverInsertRetractSealAndGC(t *testing.T) {
	j := NewJoiner()
	const k = "K1"

	// Step 1: right version R1 effective at t=10.
	j.PushRight(up("R1", k, 10, 1))
	checkEq(t, len(j.Results()), 0, "right change with no left events emits nothing")
	checkEq(t, j.RetainedRightVersions(k), []string{"R1"}, "retain R1")

	// Step 2: three left events. All join R1 immediately (early emission).
	j.PushLeft(LeftEvent{ID: "L1", Key: k, Time: 12})
	j.PushLeft(LeftEvent{ID: "L2", Key: k, Time: 15})
	j.PushLeft(LeftEvent{ID: "L3", Key: k, Time: 20})
	checkEq(t, summary(j.Results()), []string{
		"ADD:L1:R1", "ADD:L2:R1", "ADD:L3:R1",
	}, "all left events emit an immediate R1 pairing")

	// Step 3: late-but-legal insertion of R2 at an earlier effective time 14.
	// L1 (t=12) keeps R1; L2 (t=15) and L3 (t=20) switch to R2.
	j.PushRight(up("R2", k, 14, 1))
	checkEq(t, summary(j.Results()), []string{
		"ADD:L1:R1", "ADD:L2:R1", "ADD:L3:R1",
		"RET:L2:R1", "ADD:L2:R2",
		"RET:L3:R1", "ADD:L3:R2",
	}, "middle version replaces R1 for left events at/after t=14 only")
	checkEq(t, j.RetainedRightVersions(k), []string{"R1", "R2"}, "both versions retained")

	// Step 4: retract R1 by version ID. L1 loses its only valid version and
	// gets an empty pairing; L2/L3 are unaffected because R2 dominates.
	j.PushRight(ret("R1", k, 10))
	checkEq(t, summary(j.Results()), []string{
		"ADD:L1:R1", "ADD:L2:R1", "ADD:L3:R1",
		"RET:L2:R1", "ADD:L2:R2",
		"RET:L3:R1", "ADD:L3:R2",
		"RET:L1:R1", "ADD:L1:",
	}, "retracting R1 empties L1 and leaves L2/L3 on R2")

	// Retracted entries are retained as inactive until a sealing cycle runs GC.
	checkEq(t, j.RetainedRightVersions(k), []string{"R1(inactive)", "R2"},
		"retracted R1 kept as inactive tombstone before GC")

	// Step 5: left watermark alone cannot seal anything.
	j.AdvanceLeftWatermark(13)
	checkEq(t, len(j.Sealed()), 0, "left watermark alone must not seal")
	checkEq(t, j.PendingLeftIDs(), []string{"L1", "L2", "L3"}, "all left events still pending")

	// Step 6: right watermark to 13 (t == watermark is not behind it, so R1's
	// t=10 retraction above was on time). L1 (t=12) is now strictly behind
	// BOTH watermarks and seals immediately with its empty pairing.
	j.AdvanceRightWatermark(13)
	checkEq(t, sealedSummary(j.Sealed()), []string{"L1:"}, "L1 seals with its empty pairing")
	checkEq(t, j.PendingLeftIDs(), []string{"L2", "L3"}, "L1 removed from pending state")
	// GC cycle: inactive R1 collected; R2 still needed by pending L2/L3.
	check(t, contains(j.GCLog(), GCEntry{Key: k, VersionID: "R1", Reason: "retracted"}),
		"retracted R1 must be garbage collected after sealing, got %v", j.GCLog())
	checkEq(t, j.RetainedRightVersions(k), []string{"R2"}, "R2 remains for pending left events")

	// Step 7: late records behind the confirmed watermarks must be side-lined
	// and must not alter any sealed or unsealed result. First exercise an
	// on-time retract + re-upsert of R2 (right watermark is still 13, and R2
	// is effective at 14, so both records are on time). Each record changes
	// the pairing of L2 and L3, emitting retract+add per affected event.
	before := len(j.Results())
	j.PushRight(ret("R2", k, 14))
	j.PushRight(up("R2", k, 14, 1))
	checkEq(t, len(j.Results()), before+8,
		"on-time retract+reinsert emits retract/add for L2 and L3 (4 per record)")

	j.AdvanceRightWatermark(16) // now t<16 is closed on the right
	lateUpsert := up("R9", k, 15, 1)
	lateRetract := ret("R2", k, 14)
	j.PushRight(lateUpsert)
	j.PushRight(lateRetract)
	checkEq(t, len(j.Late()), 2, "both late right records go to the late side output")
	checkEq(t, j.Late()[0].Source, "right", "late record source")
	checkEq(t, j.Late()[1].Right.VersionID, "R2", "late retraction judged by version effective time")
	// Sealed L1 stays empty; pending L2/L3 stay on R2 despite the late R2 retract.
	checkEq(t, sealedSummary(j.Sealed()), []string{"L1:"}, "sealed results immutable")
	checkEq(t, summary(j.Results()), []string{
		"ADD:L1:R1", "ADD:L2:R1", "ADD:L3:R1",
		"RET:L2:R1", "ADD:L2:R2",
		"RET:L3:R1", "ADD:L3:R2",
		"RET:L1:R1", "ADD:L1:",
		// on-time retract of R2: L2/L3 go empty ...
		"RET:L2:R2", "ADD:L2:",
		"RET:L3:R2", "ADD:L3:",
		// ... followed by the on-time re-upsert restoring them to R2.
		"RET:L2:", "ADD:L2:R2",
		"RET:L3:", "ADD:L3:R2",
	}, "late right changes never appear in the result stream")

	// A late left event is side-lined as well.
	j.PushLeft(LeftEvent{ID: "LATE", Key: k, Time: 5})
	lateLeft := false
	for _, l := range j.Late() {
		if l.Source == "left" && l.Left.ID == "LATE" {
			lateLeft = true
		}
	}
	check(t, lateLeft, "left event behind left watermark must be side-lined")

	// Step 8: finish sealing. L2 (t=15) was already behind rightWm 16 and is
	// behind leftWm 13 too; L3 (t=20) needs both watermarks past 20.
	j.AdvanceLeftWatermark(16)
	checkEq(t, sealedSummary(j.Sealed()), []string{"L1:", "L2:R2"}, "L2 seals on R2")
	j.AdvanceRightWatermark(21)
	checkEq(t, sealedSummary(j.Sealed()), []string{"L1:", "L2:R2"}, "right wm alone cannot seal L3 at t=20")
	j.AdvanceLeftWatermark(21)
	checkEq(t, sealedSummary(j.Sealed()), []string{"L1:", "L2:R2", "L3:R2"},
		"sealing only happens once BOTH watermarks pass the left event")
	checkEq(t, len(j.PendingLeftIDs()), 0, "no pending left events remain")

	// End-of-input: remaining history that can be deleted.
	j.Finish()
	checkEq(t, j.RetainedRightVersions(k), []string{}, "all right history deleted at end-of-input")
	wantGC := []string{
		k + "/R1/retracted",
		k + "/R2/no-pending-left",
	}
	checkEq(t, gcIDs(j.GCLog()), wantGC, "GC log shows exactly what history was removed and why")
}

func contains(gs []GCEntry, want GCEntry) bool {
	for _, g := range gs {
		if g == want {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Boundary: empty pairing before any active version exists.
// ---------------------------------------------------------------------------

func TestEmptyPairingWhenNoActiveVersion(t *testing.T) {
	j := NewJoiner()
	const k = "K2"
	j.PushLeft(LeftEvent{ID: "E1", Key: k, Time: 100})
	checkEq(t, summary(j.Results()), []string{"ADD:E1:"}, "no active version -> immediate empty pairing")

	// A future-effective version must not backfill an earlier event.
	j.PushRight(up("F1", k, 150, 1))
	checkEq(t, summary(j.Results()), []string{"ADD:E1:"}, "future-effective version does not affect earlier event")

	// An on-time effective version now replaces the empty pairing.
	j.PushRight(up("P1", k, 90, 1))
	checkEq(t, summary(j.Results()), []string{
		"ADD:E1:", "RET:E1:", "ADD:E1:P1",
	}, "new valid version retracts empty pairing and adds replacement")
}

// ---------------------------------------------------------------------------
// Boundary: event time equal to watermark is NOT late / NOT sealed (strictly
// behind semantics).
// ---------------------------------------------------------------------------

func TestWatermarkBoundaryIsStrict(t *testing.T) {
	j := NewJoiner()
	const k = "K3"
	j.PushLeft(LeftEvent{ID: "B1", Key: k, Time: 10})
	j.AdvanceLeftWatermark(10)
	j.AdvanceRightWatermark(10)
	checkEq(t, len(j.Sealed()), 0, "event at t == watermark must not be sealed")
	checkEq(t, j.PendingLeftIDs(), []string{"B1"}, "event at watermark stays pending")

	// A right record effective exactly at the watermark is still on time and
	// must affect the pending event.
	j.PushRight(up("W1", k, 10, 1))
	checkEq(t, summary(j.Results()), []string{"ADD:B1:", "RET:B1:", "ADD:B1:W1"},
		"effective time == watermark is on time")

	j.AdvanceLeftWatermark(11)
	j.AdvanceRightWatermark(11)
	checkEq(t, sealedSummary(j.Sealed()), []string{"B1:W1"}, "seals once both watermarks are past t")
}

// ---------------------------------------------------------------------------
// Boundary: same effective time -> highest revision wins; late revisions and
// late retractions are side-lined without changing sealed output.
// ---------------------------------------------------------------------------

func TestSameEffectiveTimeRevisionWinsAndLateSideOutput(t *testing.T) {
	j := NewJoiner()
	const k = "K4"
	j.PushRight(up("V1", k, 10, 1))
	j.PushLeft(LeftEvent{ID: "T1", Key: k, Time: 10})
	checkEq(t, summary(j.Results()), []string{"ADD:T1:V1"}, "initial join V1")

	// Same effective time, higher revision arriving later wins.
	j.PushRight(up("V2", k, 10, 2))
	checkEq(t, summary(j.Results()), []string{
		"ADD:T1:V1", "RET:T1:V1", "ADD:T1:V2",
	}, "higher revision at same effective time wins")

	// A lower revision at the same effective time must not win.
	j.PushRight(up("V0", k, 10, 0))
	checkEq(t, len(j.Results()), 3, "lower revision at same effective time changes nothing")

	// Retraction by version ID: V2 withdrawn, V1 becomes the best again.
	j.PushRight(ret("V2", k, 10))
	checkEq(t, summary(j.Results()), []string{
		"ADD:T1:V1", "RET:T1:V1", "ADD:T1:V2", "RET:T1:V2", "ADD:T1:V1",
	}, "retracting the winner falls back to the next active version")

	// Seal T1 on V1, then a late higher revision must not change it.
	j.AdvanceLeftWatermark(11)
	j.AdvanceRightWatermark(11)
	checkEq(t, sealedSummary(j.Sealed()), []string{"T1:V1"}, "sealed on V1")

	sealedBefore := j.Sealed()
	j.PushRight(up("V3", k, 10, 9)) // effective t=10 < right watermark 11 -> late
	j.PushRight(ret("V1", k, 10))   // also late
	checkEq(t, len(j.Late()), 2, "late upsert and late retract both side-lined")
	checkEq(t, j.Sealed(), sealedBefore, "late right changes cannot modify sealed results")
}

// ---------------------------------------------------------------------------
// GC: a dominated older version is collectable mid-stream once both
// watermarks pass it and no pending left event can select it; the still-needed
// version for a later pending event is retained.
// ---------------------------------------------------------------------------

func TestGarbageCollectionDominatedVersions(t *testing.T) {
	j := NewJoiner()
	const k = "K5"
	j.PushRight(up("OLD", k, 10, 1))
	j.PushRight(up("NEW", k, 20, 1))
	j.PushLeft(LeftEvent{ID: "G1", Key: k, Time: 15}) // joins OLD
	j.PushLeft(LeftEvent{ID: "G2", Key: k, Time: 25}) // joins NEW
	checkEq(t, summary(j.Results()), []string{"ADD:G1:OLD", "ADD:G2:NEW"}, "initial pairings")

	// Before watermarks pass OLD's effective time nothing is collectable.
	j.AdvanceLeftWatermark(11)
	j.AdvanceRightWatermark(11)
	checkEq(t, j.RetainedRightVersions(k), []string{"NEW", "OLD"},
		"OLD still needed while G1 at t=15 is pending")

	// G1 seals once both watermarks pass t=15; OLD is then dominated by NEW
	// (effective 20 > 10), both watermarks are past 10 and no pending event
	// can select OLD anymore (G2 is at 25 and selects NEW).
	j.AdvanceLeftWatermark(16)
	j.AdvanceRightWatermark(16)
	checkEq(t, sealedSummary(j.Sealed()), []string{"G1:OLD"}, "G1 seals with OLD")
	checkEq(t, j.RetainedRightVersions(k), []string{"NEW"},
		"dominated OLD is garbage collected mid-stream; NEW retained for G2")

	// A late re-insert of OLD cannot bring it back.
	j.PushRight(up("OLD", k, 10, 1))
	lateOLD := false
	for _, l := range j.Late() {
		if l.Source == "right" && l.Right.VersionID == "OLD" {
			lateOLD = true
		}
	}
	check(t, lateOLD, "re-insert of collected OLD is now late and side-lined")
	checkEq(t, j.RetainedRightVersions(k), []string{"NEW"}, "late re-insert does not restore OLD")

	j.Finish()
	checkEq(t, sealedSummary(j.Sealed()), []string{"G1:OLD", "G2:NEW"}, "G2 seals on NEW")
	checkEq(t, j.RetainedRightVersions(k), []string{}, "everything collected at end-of-input")
	wantGC := []string{
		k + "/NEW/no-pending-left",
		k + "/OLD/dominated",
	}
	checkEq(t, gcIDs(j.GCLog()), wantGC, "GC reasons recorded")
}

// ---------------------------------------------------------------------------
// Fault recovery: snapshot -> restore -> replay yields identical externally
// visible state and output, including late side output and GC log.
// ---------------------------------------------------------------------------

func TestSnapshotRestoreRecovery(t *testing.T) {
	build := func(j *Joiner) {
		const k = "K6"
		j.PushRight(up("R1", k, 10, 1))
		j.PushLeft(LeftEvent{ID: "C1", Key: k, Time: 12})
		j.PushLeft(LeftEvent{ID: "C2", Key: k, Time: 18})
		j.PushRight(up("R2", k, 15, 1))
		j.AdvanceLeftWatermark(13)
		j.AdvanceRightWatermark(13)
	}

	primary := NewJoiner()
	build(primary)

	checkpoint, err := primary.Snapshot()
	if err != nil {
		t.Fatalf("snapshot: %v", err)
	}

	// Simulate failure: a fresh operator restores the checkpoint.
	recovered := NewJoiner()
	if err := recovered.Restore(checkpoint); err != nil {
		t.Fatalf("restore: %v", err)
	}

	// Replay the remaining stream identically on both.
	continueStream := func(j *Joiner) {
		const k = "K6"
		// Effective time 10 is behind the restored right watermark 13: late.
		j.PushRight(ret("R1", k, 10))
		// Effective time 17 is on time and must re-evaluate the restored
		// pending left events, switching them from R2 to R3.
		j.PushRight(up("R3", k, 17, 1))
		j.PushLeft(LeftEvent{ID: "C3", Key: k, Time: 19})
		j.AdvanceLeftWatermark(20)
		j.AdvanceRightWatermark(20)
		j.PushRight(up("LATE", k, 5, 1))
		j.Finish()
	}
	continueStream(primary)
	continueStream(recovered)

	checkEq(t, recovered.Results(), primary.Results(), "result stream identical after recovery")
	// Explicitly verify the recovered right state drove a real re-evaluation.
	got := summary(recovered.Results())
	check(t, containsStr(got, "RET:C2:R2") && containsStr(got, "ADD:C2:R3"),
		"restored right state must retract R2 and pair C2 with R3, got %v", got)
	checkEq(t, recovered.Sealed(), primary.Sealed(), "sealed pairings identical after recovery")
	checkEq(t, recovered.Late(), primary.Late(), "late side output identical after recovery")
	checkEq(t, recovered.GCLog(), primary.GCLog(), "GC log identical after recovery")
	checkEq(t, len(recovered.PendingLeftIDs()), 0, "no pending state left after Finish")
	checkEq(t, len(recovered.RetainedRightVersions("K6")), 0, "right state fully collected after Finish")
}

func containsStr(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Concurrency interleaving: many goroutines drive disjoint keys through the
// same operator (with concurrent snapshotting) while watermarks are advanced
// deterministically. Each key lives in a disjoint event-time window so the
// shared watermarks affect every key identically; per-key final state must
// equal a sequential replay. The run must be race-clean (`go test -race`).
// ---------------------------------------------------------------------------

type ckOp struct {
	kind int // 0 left, 1 right
	le   LeftEvent
	re   RightRecord
}

// scriptFor returns the on-time event pushes for a key in its own time
// window. base = 1000*idx guarantees disjoint windows across keys.
func scriptFor(idx int) (string, []ckOp) {
	base := int64(1000 * idx)
	key := fmt.Sprintf("CK%02d", idx)
	return key, []ckOp{
		{kind: 1, re: up("A", key, base+10, 1)},
		{kind: 0, le: LeftEvent{ID: key + "-L1", Key: key, Time: base + 12}},
		{kind: 1, re: up("B", key, base+14, 1)},
		{kind: 0, le: LeftEvent{ID: key + "-L2", Key: key, Time: base + 16}},
		{kind: 1, re: ret("A", key, base+10)},
	}
}

func replayPushes(j *Joiner, ops []ckOp) {
	for _, o := range ops {
		if o.kind == 0 {
			j.PushLeft(o.le)
		} else {
			j.PushRight(o.re)
		}
	}
}

func TestConcurrentInterleaving(t *testing.T) {
	const n = 24

	// Sequential reference: scripts replayed one after another, then the same
	// deterministic watermark schedule.
	ref := NewJoiner()
	allScripts := make([][]ckOp, n)
	for i := 0; i < n; i++ {
		_, ops := scriptFor(i)
		allScripts[i] = ops
		replayPushes(ref, ops)
	}
	advanceWatermarks := func(j *Joiner) {
		for i := 0; i < n; i++ {
			base := int64(1000 * i)
			j.AdvanceLeftWatermark(base + 13)
			j.AdvanceRightWatermark(base + 13)
		}
		for i := 0; i < n; i++ {
			base := int64(1000 * i)
			j.AdvanceLeftWatermark(base + 17)
			j.AdvanceRightWatermark(base + 17)
		}
	}
	advanceWatermarks(ref)
	// Deterministically late left events (behind both watermarks) are pushed
	// after the watermark schedule so the result is scheduling-independent.
	for i := 0; i < n; i++ {
		base := int64(1000 * i)
		key, _ := scriptFor(i)
		ref.PushLeft(LeftEvent{ID: key + "-LATE", Key: key, Time: base + 5})
	}

	// Concurrent run: per-key scripts on separate goroutines, plus a
	// concurrent snapshotter exercising the mutex on every code path.
	live := NewJoiner()
	var workers sync.WaitGroup
	for i := 0; i < n; i++ {
		workers.Add(1)
		go func(idx int) {
			defer workers.Done()
			replayPushes(live, allScripts[idx])
		}(i)
	}

	var snapWG sync.WaitGroup
	stopSnap := make(chan struct{})
	snapWG.Add(1)
	go func() {
		defer snapWG.Done()
		for {
			select {
			case <-stopSnap:
				return
			default:
				if _, err := live.Snapshot(); err != nil {
					t.Errorf("concurrent snapshot failed: %v", err)
					return
				}
			}
		}
	}()

	workers.Wait()
	close(stopSnap)
	snapWG.Wait()

	// Watermark schedule is applied only after all pushes joined, so the
	// late/on-time decision is independent of goroutine scheduling.
	advanceWatermarks(live)
	for i := 0; i < n; i++ {
		base := int64(1000 * i)
		key, _ := scriptFor(i)
		live.PushLeft(LeftEvent{ID: key + "-LATE", Key: key, Time: base + 5})
	}

	// Total per-key output must match the sequential reference.
	for i := 0; i < n; i++ {
		key, _ := scriptFor(i)
		checkEq(t, resultsForKey(live.Results(), key), resultsForKey(ref.Results(), key),
			"result stream for key "+key)
		checkEq(t, sealedForKey(live.Sealed(), key), sealedForKey(ref.Sealed(), key),
			"sealed pairings for key "+key)
		checkEq(t, lateCountForKey(live.Late(), key), lateCountForKey(ref.Late(), key),
			"late count for key "+key)
	}
	check(t, len(live.Sealed()) == 2*n, "every key seals exactly two events, got %d", len(live.Sealed()))
	check(t, len(live.Late()) == n, "every key contributes one late event, got %d", len(live.Late()))
}

func resultsForKey(rs []Result, key string) []string {
	var out []string
	for _, r := range rs {
		if r.Pair.Key == key {
			k := "ADD"
			if r.Kind == ResultRetract {
				k = "RET"
			}
			out = append(out, k+":"+r.Pair.LeftID+":"+rightID(r.Pair))
		}
	}
	return out
}

func sealedForKey(ss []Sealed, key string) []string {
	var out []string
	for _, s := range ss {
		if s.Pair.Key == key {
			out = append(out, s.Pair.LeftID+":"+rightID(s.Pair))
		}
	}
	return out
}

func lateCountForKey(ls []LateRecord, key string) int {
	n := 0
	for _, l := range ls {
		if (l.Left != nil && l.Left.Key == key) || (l.Right != nil && l.Right.Key == key) {
			n++
		}
	}
	return n
}

// ---------------------------------------------------------------------------
// Re-upsert of a retracted version reactivates it and updates pairings.
// ---------------------------------------------------------------------------

func TestReupsertRetractedVersion(t *testing.T) {
	j := NewJoiner()
	const k = "K7"
	j.PushRight(up("U1", k, 10, 1))
	j.PushLeft(LeftEvent{ID: "U-L1", Key: k, Time: 12})
	checkEq(t, summary(j.Results()), []string{"ADD:U-L1:U1"}, "initial join")

	j.PushRight(ret("U1", k, 10))
	checkEq(t, summary(j.Results()), []string{"ADD:U-L1:U1", "RET:U-L1:U1", "ADD:U-L1:"},
		"retraction produces empty replacement pairing")

	// Same version ID inserted again (on time) reactivates and re-matches.
	j.PushRight(up("U1", k, 10, 3))
	checkEq(t, summary(j.Results()), []string{
		"ADD:U-L1:U1", "RET:U-L1:U1", "ADD:U-L1:", "RET:U-L1:", "ADD:U-L1:U1",
	}, "re-upsert of retracted version restores the pairing")

	// After sealing, the inactive tombstone from the earlier retract must not
	// linger; the active re-upserted entry is the one collected at the end.
	j.Finish()
	checkEq(t, j.RetainedRightVersions(k), []string{}, "no lingering versions after Finish")
}
