package pathmtu

import (
	"errors"
	"strings"
	"testing"
	"time"
)

type fakeClock struct{ t time.Time }

func newFakeClock() *fakeClock {
	return &fakeClock{t: time.Date(2026, 11, 23, 12, 0, 0, 0, time.UTC)}
}
func (c *fakeClock) Now() time.Time          { return c.t }
func (c *fakeClock) Advance(d time.Duration) { c.t = c.t.Add(d) }

func newDisc(t *testing.T, cfg Config) (*Discoverer, *fakeClock) {
	t.Helper()
	clk := newFakeClock()
	cfg.Now = clk.Now
	d, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return d, clk
}

func mustProbe(t *testing.T, d *Discoverer) Probe {
	t.Helper()
	p, ok := d.SendProbe()
	if !ok {
		t.Fatalf("expected a probe to be available; state: %s", d.Current())
	}
	return p
}

func TestConfigValidation(t *testing.T) {
	cases := []Config{
		{MinPayload: 0, MaxPayload: 10},
		{MinPayload: -1, MaxPayload: 10},
		{MinPayload: 10, MaxPayload: 9},
		{MinPayload: 10, MaxPayload: 10, EvidenceTTL: -time.Second},
	}
	for i, cfg := range cases {
		if _, err := New(cfg); !errors.Is(err, ErrInvalidConfig) {
			t.Fatalf("case %d: expected ErrInvalidConfig, got %v", i, err)
		}
	}
	if _, err := New(Config{MinPayload: 1200, MaxPayload: 1200}); err != nil {
		t.Fatalf("MinPayload == MaxPayload should be valid: %v", err)
	}
}

// Normal scenario: successful probes raise only the verified lower bound, and
// ordinary traffic always rides the verified lower bound up to the ceiling.
func TestNormalProbeGrowth(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})

	if snap := d.Current(); snap.Generation != 0 || snap.OrdinarySendSize != 0 {
		t.Fatalf("before migration, expected inactive state, got %+v", snap)
	}
	if _, ok := d.SendProbe(); ok {
		t.Fatal("no probe should be available without an active generation")
	}

	res := d.MigrateTo(1, 1200)
	if !res.Applied || res.Snapshot.Kind != ChangeMigratedFresh {
		t.Fatalf("migration: applied=%v kind=%s", res.Applied, res.Snapshot.Kind)
	}
	if got, want := res.Snapshot.OrdinarySendSize, 1200; got != want {
		t.Fatalf("ordinary send size after migration = %d, want %d", got, want)
	}
	if got, want := res.Snapshot.NextProbe, 1350; got != want || !res.Snapshot.NextProbeValid {
		t.Fatalf("next probe after migration = %d valid=%v, want %d", got, res.Snapshot.NextProbeValid, want)
	}

	wantProbes := []int{1350, 1425, 1462}
	for i, size := range wantProbes {
		p := mustProbe(t, d)
		if p.Size != size {
			t.Fatalf("probe %d size = %d, want %d", i, p.Size, size)
		}
		before := d.Current().OrdinarySendSize
		r := d.OnProbeSuccess(p.ID)
		if r.Snapshot.Kind != ChangeVerifiedRaised {
			t.Fatalf("probe %d success kind = %s, want %s", i, r.Snapshot.Kind, ChangeVerifiedRaised)
		}
		if r.Snapshot.OrdinarySendSize != size {
			t.Fatalf("probe %d: ordinary send size = %d, want %d", i, r.Snapshot.OrdinarySendSize, size)
		}
		if r.Snapshot.OrdinarySendSize < before {
			t.Fatalf("verified bound went backwards: %d -> %d", before, r.Snapshot.OrdinarySendSize)
		}
		if r.Snapshot.Verified > r.Snapshot.Ceiling {
			t.Fatalf("verified %d exceeds ceiling %d", r.Snapshot.Verified, r.Snapshot.Ceiling)
		}
	}
}

// Boundary scenario: a "too big" report lowers the ceiling; a later report
// below the verified bound forces an immediate drop and re-verification.
func TestTooBigLowersCeilingThenRevalidates(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})
	d.MigrateTo(1, 1200)

	p := mustProbe(t, d) // 1350
	r := d.OnProbeTooBig(p.ID, 1300)
	if r.Snapshot.Kind != ChangeCeilingLowered {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeCeilingLowered)
	}
	if r.Snapshot.Ceiling != 1300 {
		t.Fatalf("ceiling = %d, want 1300", r.Snapshot.Ceiling)
	}
	if r.Snapshot.Verified != 1200 {
		t.Fatalf("verified = %d, want 1200 (untouched above verified)", r.Snapshot.Verified)
	}
	if r.Snapshot.OrdinarySendSize != 1200 {
		t.Fatalf("ordinary = %d, want 1200", r.Snapshot.OrdinarySendSize)
	}

	// Probe at the midpoint of [1200,1300] succeeds -> verified 1250.
	p = mustProbe(t, d)
	if p.Size != 1250 {
		t.Fatalf("probe size = %d, want 1250", p.Size)
	}
	d.OnProbeSuccess(p.ID)

	// Now a hard limit below the verified bound arrives.
	p = mustProbe(t, d) // 1275
	r = d.OnProbeTooBig(p.ID, 1240)
	if r.Snapshot.Kind != ChangeRevalidating {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeRevalidating)
	}
	if r.Snapshot.Verified != 1240 || r.Snapshot.Ceiling != 1240 {
		t.Fatalf("bounds = verified %d ceiling %d, want both 1240", r.Snapshot.Verified, r.Snapshot.Ceiling)
	}
	if r.Snapshot.OrdinarySendSize != 1240 {
		t.Fatalf("ordinary = %d, want immediate drop to 1240", r.Snapshot.OrdinarySendSize)
	}
	if !r.Snapshot.Revalidating {
		t.Fatal("expected revalidating flag")
	}
	if !r.Snapshot.NextProbeValid || r.Snapshot.NextProbe != 1240 {
		t.Fatalf("next probe = %d valid=%v, want confirmation at 1240", r.Snapshot.NextProbe, r.Snapshot.NextProbeValid)
	}

	// Confirmation succeeds.
	p = mustProbe(t, d)
	if p.Size != 1240 {
		t.Fatalf("re-verification probe = %d, want 1240", p.Size)
	}
	r = d.OnProbeSuccess(p.ID)
	if r.Snapshot.Kind != ChangeRevalidated {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeRevalidated)
	}
	if r.Snapshot.Revalidating {
		t.Fatal("revalidating should be cleared after confirmation")
	}
	if r.Snapshot.OrdinarySendSize != 1240 {
		t.Fatalf("ordinary = %d, want 1240", r.Snapshot.OrdinarySendSize)
	}
}

func TestTooBigBelowFloorClampsToFloor(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})
	d.MigrateTo(1, 1200)
	p := mustProbe(t, d)
	r := d.OnProbeTooBig(p.ID, 900)
	if r.Snapshot.Ceiling != 1200 || r.Snapshot.Verified != 1200 {
		t.Fatalf("bounds = %d/%d, want floor 1200/1200", r.Snapshot.Verified, r.Snapshot.Ceiling)
	}
	if r.Snapshot.OrdinarySendSize != 1200 {
		t.Fatalf("ordinary = %d, want floor 1200", r.Snapshot.OrdinarySendSize)
	}
	if r.Snapshot.Kind != ChangeCeilingLowered {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeCeilingLowered)
	}
}

// Interleaving scenario A: a probe times out and ordinary packets in the same
// window were also lost. No liveness means no attribution: bounds are held.
func TestTimeoutWithSimultaneousOrdinaryLossHolds(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})
	d.MigrateTo(1, 1200)

	// Ordinary ack happened BEFORE the probe window opens.
	d.OnOrdinaryAck(1)
	p := mustProbe(t, d)
	r := d.OnProbeTimeout(p.ID)
	if r.Snapshot.Kind != ChangeHeld {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeHeld)
	}
	if r.Snapshot.Verified != 1200 || r.Snapshot.Ceiling != 1500 {
		t.Fatalf("bounds changed on unattributed loss: verified=%d ceiling=%d", r.Snapshot.Verified, r.Snapshot.Ceiling)
	}
	if !strings.Contains(r.Snapshot.Reason, "no ordinary acknowledgment") {
		t.Fatalf("reason should explain the held window: %q", r.Snapshot.Reason)
	}
}

// Interleaving scenario B: two consecutive probe timeouts, each with ordinary
// liveness in the same window, lower the ceiling. A single such timeout does not.
func TestTwoConsecutiveLiveTimeoutsLowerCeiling(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})
	d.MigrateTo(1, 1200)

	p := mustProbe(t, d) // 1350
	d.OnOrdinaryAck(1)
	r := d.OnProbeTimeout(p.ID)
	if r.Snapshot.Kind != ChangeHeld || r.Snapshot.Ceiling != 1500 {
		t.Fatalf("first live timeout should hold: kind=%s ceiling=%d", r.Snapshot.Kind, r.Snapshot.Ceiling)
	}
	if !strings.Contains(r.Snapshot.Reason, "1/2") {
		t.Fatalf("reason should show 1/2 progress: %q", r.Snapshot.Reason)
	}

	p = mustProbe(t, d)
	d.OnOrdinaryAck(1)
	r = d.OnProbeTimeout(p.ID)
	if r.Snapshot.Kind != ChangeCeilingLowered {
		t.Fatalf("second live timeout kind = %s, want %s", r.Snapshot.Kind, ChangeCeilingLowered)
	}
	if r.Snapshot.Ceiling != p.Size-1 {
		t.Fatalf("ceiling = %d, want %d (one below failed probe %d)", r.Snapshot.Ceiling, p.Size-1, p.Size)
	}
}

// A no-liveness timeout breaks the consecutive streak, so a later live timeout
// is counted as the first of a new pair.
func TestNoLivenessTimeoutResetsStreak(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})
	d.MigrateTo(1, 1200)

	p := mustProbe(t, d)
	d.OnOrdinaryAck(1)
	d.OnProbeTimeout(p.ID) // 1/2

	p = mustProbe(t, d)
	// no ordinary ack -> held and streak reset
	r := d.OnProbeTimeout(p.ID)
	if r.Snapshot.Kind != ChangeHeld || r.Snapshot.Ceiling != 1500 {
		t.Fatalf("unattributed timeout changed state: kind=%s ceiling=%d", r.Snapshot.Kind, r.Snapshot.Ceiling)
	}

	p = mustProbe(t, d)
	d.OnOrdinaryAck(1)
	r = d.OnProbeTimeout(p.ID)
	if r.Snapshot.Kind != ChangeHeld || r.Snapshot.Ceiling != 1500 {
		t.Fatalf("third timeout must be first of a new pair, got kind=%s ceiling=%d", r.Snapshot.Kind, r.Snapshot.Ceiling)
	}
	if !strings.Contains(r.Snapshot.Reason, "1/2") {
		t.Fatalf("expected fresh 1/2 progress: %q", r.Snapshot.Reason)
	}
}

// Old feedback must not pollute a new generation, including a probe that was in
// flight across the migration and whose round trip completes on the new path.
func TestMigrationIsolatesInFlightProbeFeedback(t *testing.T) {
	clk := newFakeClock()
	d, err := New(Config{MinPayload: 1200, MaxPayload: 1500, EvidenceTTL: time.Minute, Now: clk.Now})
	if err != nil {
		t.Fatal(err)
	}
	d.MigrateTo(1, 1200)
	oldProbe := mustProbe(t, d) // 1350 on gen 1

	clk.Advance(time.Second)
	d.MigrateTo(2, 1200) // fresh path, fresh bounds

	// The in-flight probe from gen 1 comes back successfully while gen 2 is active.
	r := d.OnProbeSuccess(oldProbe.ID)
	if r.Snapshot.Generation != 2 {
		t.Fatalf("snapshot generation = %d, want active 2", r.Snapshot.Generation)
	}
	if r.Snapshot.Verified != 1200 || r.Snapshot.Ceiling != 1500 {
		t.Fatalf("gen 2 polluted by gen 1 success: verified=%d ceiling=%d", r.Snapshot.Verified, r.Snapshot.Ceiling)
	}
	if r.Snapshot.Kind != ChangeAppliedElsewhere {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeAppliedElsewhere)
	}

	// A timeout for the same (already resolved) probe is unknown now.
	r = d.OnProbeTimeout(oldProbe.ID)
	if r.Applied || r.Snapshot.Verified != 1200 {
		t.Fatalf("resolved probe timeout must not apply: applied=%v verified=%d", r.Applied, r.Snapshot.Verified)
	}
}

// A probe in flight when we leave a path must not be accepted after we return
// to that same path generation: the generation number matches, but the
// activation does not.
func TestStaleActivationProbeRejectedAfterReturn(t *testing.T) {
	clk := newFakeClock()
	d, _ := New(Config{MinPayload: 1200, MaxPayload: 1500, EvidenceTTL: time.Minute, Now: clk.Now})
	d.MigrateTo(1, 1200)
	pSuccess := mustProbe(t, d) // issued during the first activation of gen 1
	pTooBig := mustProbe(t, d)

	d.MigrateTo(2, 1200)
	clk.Advance(time.Second)
	r := d.MigrateTo(1, 1200) // return to gen 1, reuse evidence
	if r.Snapshot.Kind != ChangeMigratedReuse {
		t.Fatalf("expected reuse on return, got %s", r.Snapshot.Kind)
	}

	// The old probe's success arrives now. Generation matches but activation is
	// stale: it must be refused and must not raise the verified bound.
	r = d.OnProbeSuccess(pSuccess.ID)
	if r.Applied || r.Snapshot.Verified != 1200 {
		t.Fatalf("stale-activation success must not apply: applied=%v verified=%d", r.Applied, r.Snapshot.Verified)
	}
	if r.Snapshot.Kind != ChangeIgnored {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeIgnored)
	}

	// A too-big report from a stale probe must not lower the ceiling either.
	r = d.OnProbeTooBig(pTooBig.ID, 1280)
	if r.Applied || r.Snapshot.Ceiling != 1500 {
		t.Fatalf("stale-activation too-big must not apply: applied=%v ceiling=%d", r.Applied, r.Snapshot.Ceiling)
	}

	// A fresh probe on the current activation still works normally.
	p := mustProbe(t, d)
	r = d.OnProbeSuccess(p.ID)
	if !r.Applied || r.Snapshot.Verified != p.Size {
		t.Fatalf("fresh probe should apply: applied=%v verified=%d size=%d", r.Applied, r.Snapshot.Verified, p.Size)
	}
}

func TestMigrationTooBigAndTimeoutFromOldGenIgnored(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})
	d.MigrateTo(1, 1200)
	pOld := mustProbe(t, d)

	d.MigrateTo(2, 1200)

	// Too-big feedback from gen 1 must not lower gen 2's ceiling.
	r := d.OnProbeTooBig(pOld.ID, 1280)
	if r.Snapshot.Ceiling != 1500 || r.Snapshot.Verified != 1200 {
		t.Fatalf("gen 2 ceiling polluted by gen 1 too-big: verified=%d ceiling=%d", r.Snapshot.Verified, r.Snapshot.Ceiling)
	}
	if r.Snapshot.Kind != ChangeAppliedElsewhere {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeAppliedElsewhere)
	}

	// A timeout for a gen-1 probe while gen 2 is active is refused outright and
	// does not touch the timeout streak of the new generation.
	pNew := mustProbe(t, d)
	d.OnOrdinaryAck(2)
	r2 := d.OnProbeTimeout(pOld.ID)
	if r2.Applied || r2.Snapshot.Ceiling != 1500 {
		t.Fatalf("old-generation timeout must not apply: applied=%v ceiling=%d", r2.Applied, r2.Snapshot.Ceiling)
	}
	// First live timeout on gen 2 remains the first of a pair.
	d.OnOrdinaryAck(2)
	r3 := d.OnProbeTimeout(pNew.ID)
	if r3.Snapshot.Kind != ChangeHeld || !strings.Contains(r3.Snapshot.Reason, "1/2") {
		t.Fatalf("gen 2 streak must be unaffected: kind=%s reason=%q", r3.Snapshot.Kind, r3.Snapshot.Reason)
	}
}

// Returning to an earlier path reuses unexpired, generation-matching evidence...
func TestReturnToPathReusesUnexpiredEvidence(t *testing.T) {
	clk := newFakeClock()
	d, err := New(Config{MinPayload: 1200, MaxPayload: 1500, EvidenceTTL: 30 * time.Second, Now: clk.Now})
	if err != nil {
		t.Fatal(err)
	}
	d.MigrateTo(1, 1200)
	p := mustProbe(t, d)
	d.OnProbeSuccess(p.ID) // verified 1350

	d.MigrateTo(2, 1200)
	clk.Advance(10 * time.Second)
	r := d.MigrateTo(1, 1200)
	if r.Snapshot.Kind != ChangeMigratedReuse {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeMigratedReuse)
	}
	if r.Snapshot.Verified != 1350 || r.Snapshot.OrdinarySendSize != 1350 {
		t.Fatalf("reused evidence lost: verified=%d ordinary=%d, want 1350", r.Snapshot.Verified, r.Snapshot.OrdinarySendSize)
	}
}

// ...but expired evidence must not be reused, and EvidenceTTL=0 disables reuse.
func TestReturnToPathRejectsExpiredEvidence(t *testing.T) {
	clk := newFakeClock()
	d, _ := New(Config{MinPayload: 1200, MaxPayload: 1500, EvidenceTTL: 30 * time.Second, Now: clk.Now})
	d.MigrateTo(1, 1200)
	p := mustProbe(t, d)
	d.OnProbeSuccess(p.ID) // verified 1350
	d.MigrateTo(2, 1200)

	clk.Advance(31 * time.Second)
	r := d.MigrateTo(1, 1200)
	if r.Snapshot.Kind != ChangeMigratedFresh {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeMigratedFresh)
	}
	if r.Snapshot.Verified != 1200 {
		t.Fatalf("expired evidence reused: verified=%d, want fresh 1200", r.Snapshot.Verified)
	}

	clk2 := newFakeClock()
	d2, _ := New(Config{MinPayload: 1200, MaxPayload: 1500, EvidenceTTL: 0, Now: clk2.Now})
	d2.MigrateTo(1, 1200)
	p2 := mustProbe(t, d2)
	d2.OnProbeSuccess(p2.ID)
	d2.MigrateTo(2, 1200)
	clk2.Advance(time.Millisecond)
	r = d2.MigrateTo(1, 1200)
	if r.Snapshot.Kind != ChangeMigratedFresh || r.Snapshot.Verified != 1200 {
		t.Fatalf("ttl=0 must disable reuse: kind=%s verified=%d", r.Snapshot.Kind, r.Snapshot.Verified)
	}
}

// Evidence recorded on a retired generation is stored there and only reused for
// that same generation; it never becomes evidence for a different one.
func TestEvidenceIsBoundToItsGeneration(t *testing.T) {
	clk := newFakeClock()
	d, _ := New(Config{MinPayload: 1200, MaxPayload: 1500, EvidenceTTL: time.Minute, Now: clk.Now})
	d.MigrateTo(1, 1200)
	p := mustProbe(t, d)
	d.MigrateTo(2, 1200)
	// Success delivered while gen 2 is active is recorded on gen 1.
	d.OnProbeSuccess(p.ID)

	ev, ok := d.EvidenceFor(1)
	if !ok {
		t.Fatal("gen 1 evidence missing")
	}
	if ev.Generation != 1 || ev.Verified != 1350 || ev.Active {
		t.Fatalf("gen 1 evidence = %+v, want verified 1350 inactive", ev)
	}
	ev2, ok := d.EvidenceFor(2)
	if !ok || ev2.Verified != 1200 || !ev2.Active {
		t.Fatalf("gen 2 evidence = %+v, want fresh active 1200", ev2)
	}

	// Gen 3 is a brand-new generation and must not inherit anything.
	d.MigrateTo(3, 1200)
	if snap := d.Current(); snap.Verified != 1200 || snap.Ceiling != 1500 {
		t.Fatalf("gen 3 should start fresh: %s", snap)
	}
}

func TestInvalidInputsAreRejected(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})
	if r := d.MigrateTo(0, 1200); r.Applied {
		t.Fatal("generation 0 must be rejected")
	}
	if r := d.OnProbeSuccess(999); r.Applied {
		t.Fatal("unknown probe must be rejected")
	}
	if r := d.OnProbeTimeout(999); r.Applied {
		t.Fatal("unknown probe must be rejected")
	}
	if r := d.OnProbeTooBig(999, 1300); r.Applied {
		t.Fatal("unknown probe must be rejected")
	}
	if r := d.OnOrdinaryAck(1); r.Applied {
		t.Fatal("ack without active generation must be ignored")
	}

	d.MigrateTo(1, 500) // below MinPayload -> clamped
	if snap := d.Current(); snap.Floor != 1200 || snap.OrdinarySendSize != 1200 {
		t.Fatalf("floor clamp failed: %s", snap)
	}

	p := mustProbe(t, d)
	if r := d.OnProbeTooBig(p.ID, 0); r.Applied {
		t.Fatal("invalid size limit must be rejected")
	}
	// The probe was consumed by the rejected report; a fresh one is available.
	p = mustProbe(t, d)
	if r := d.OnProbeSuccess(p.ID); !r.Applied {
		t.Fatal("fresh probe should resolve")
	}
	if r := d.OnProbeSuccess(p.ID); r.Applied {
		t.Fatal("double resolution must be rejected")
	}
}

// A confirmation probe that keeps timing out in isolation must not wedge the
// sender: ordinary packets at the confirmed size prove it just as well.
func TestRevalidationDoesNotWedgeOnLostConfirmationProbe(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})
	d.MigrateTo(1, 1200)
	p := mustProbe(t, d)   // 1350
	d.OnProbeSuccess(p.ID) // verified 1350

	p = mustProbe(t, d)              // 1425
	r := d.OnProbeTooBig(p.ID, 1300) // below verified -> drop + revalidate
	if r.Snapshot.Verified != 1300 || !r.Snapshot.Revalidating {
		t.Fatalf("setup: verified=%d revalidating=%v, want 1300/true", r.Snapshot.Verified, r.Snapshot.Revalidating)
	}

	// Confirmation probe at 1300 is lost, but ordinary traffic at 1300 is acked.
	p = mustProbe(t, d)
	if p.Size != 1300 {
		t.Fatalf("confirmation probe = %d, want 1300", p.Size)
	}
	d.OnOrdinaryAck(1)
	r = d.OnProbeTimeout(p.ID)
	if r.Snapshot.Kind != ChangeRevalidated {
		t.Fatalf("kind = %s, want %s", r.Snapshot.Kind, ChangeRevalidated)
	}
	if r.Snapshot.Revalidating {
		t.Fatal("revalidating must clear once ordinary traffic proves the limit")
	}
	if _, ok := d.SendProbe(); ok {
		t.Fatal("bounds are settled; no probe should be offered")
	}
}

// A confirmation probe lost together with ordinary traffic stays pending and is
// retried at the same size; it is only cleared once there is liveness.
func TestRevalidationRetriesWhenNoLiveness(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})
	d.MigrateTo(1, 1200)
	p := mustProbe(t, d)
	d.OnProbeSuccess(p.ID)
	p = mustProbe(t, d)
	d.OnProbeTooBig(p.ID, 1300)

	for i := 0; i < 3; i++ {
		p = mustProbe(t, d)
		if p.Size != 1300 {
			t.Fatalf("retry %d probe = %d, want 1300", i, p.Size)
		}
		r := d.OnProbeTimeout(p.ID) // no ordinary ack in window
		if r.Snapshot.Kind != ChangeHeld || !r.Snapshot.Revalidating {
			t.Fatalf("retry %d: kind=%s revalidating=%v, want held/true", i, r.Snapshot.Kind, r.Snapshot.Revalidating)
		}
	}
}

func TestSnapshotsExplainBoundaryChanges(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1500})
	d.MigrateTo(1, 1200)
	p := mustProbe(t, d)
	d.OnProbeSuccess(p.ID)

	last := d.Trace()[len(d.Trace())-1]
	s := last.String()
	for _, want := range []string{"ordinary=1350", "nextProbe=1425", "kind=verified_raised"} {
		if !strings.Contains(s, want) {
			t.Fatalf("snapshot string %q missing %q", s, want)
		}
	}
	if last.Kind != ChangeVerifiedRaised || last.Reason == "" {
		t.Fatalf("snapshot lacks change reason: %+v", last)
	}
	if got, want := len(d.Trace()), 2; got != want {
		t.Fatalf("trace length = %d, want %d (migration + successful probe)", got, want)
	}
}

func TestSettledBoundsOfferNoProbe(t *testing.T) {
	d, _ := newDisc(t, Config{MinPayload: 1200, MaxPayload: 1200})
	d.MigrateTo(1, 1200)
	if _, ok := d.SendProbe(); ok {
		t.Fatal("no probe should be offered when verified == ceiling")
	}
	if snap := d.Current(); snap.NextProbeValid {
		t.Fatalf("NextProbeValid should be false when settled: %s", snap)
	}
}
