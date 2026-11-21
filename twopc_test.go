package twopc

import (
	"path/filepath"
	"testing"
)

// twoShards builds two participant shards (balance 100 each), their links,
// and a coordinator, all logging into t.TempDir().
func twoShards(t *testing.T) (p1, p2 *Participant, l1, l2 *Link, c *Coordinator, dir string) {
	t.Helper()
	dir = t.TempDir()
	var err error
	if p1, err = OpenParticipant("s1", filepath.Join(dir, "s1.log"), 100); err != nil {
		t.Fatalf("open s1: %v", err)
	}
	if p2, err = OpenParticipant("s2", filepath.Join(dir, "s2.log"), 100); err != nil {
		t.Fatalf("open s2: %v", err)
	}
	l1 = &Link{P: p1}
	l2 = &Link{P: p2}
	parts := map[string]ParticipantRPC{"s1": l1, "s2": l2}
	if c, err = OpenCoordinator(filepath.Join(dir, "coord.log"), parts); err != nil {
		t.Fatalf("open coordinator: %v", err)
	}
	return p1, p2, l1, l2, c, dir
}

// Normal case: one transaction moves 30 from shard 1 to shard 2.
func TestCommitAppliesBothShards(t *testing.T) {
	p1, p2, _, _, c, _ := twoShards(t)

	if err := c.Submit("tx1", map[string]int{"s1": -30, "s2": 30}); err != nil {
		t.Fatalf("submit: %v", err)
	}
	if !c.Done("tx1") {
		t.Fatal("expected transaction to be fully delivered")
	}
	if d, ok := c.DecisionOf("tx1"); !ok || d != DecisionCommit {
		t.Fatalf("decision = %q, %v; want commit, true", d, ok)
	}
	if got := p1.Balance(); got != 70 {
		t.Errorf("s1 balance = %d, want 70", got)
	}
	if got := p2.Balance(); got != 130 {
		t.Errorf("s2 balance = %d, want 130", got)
	}
}

// Boundary case: a veto (overdraft on s2) must abort the whole transaction.
func TestVetoLeadsToAbort(t *testing.T) {
	p1, p2, _, _, c, _ := twoShards(t)

	if err := c.Submit("tx1", map[string]int{"s1": 10, "s2": -500}); err != nil {
		t.Fatalf("submit: %v", err)
	}
	if d, ok := c.DecisionOf("tx1"); !ok || d != DecisionAbort {
		t.Fatalf("decision = %q, %v; want abort, true", d, ok)
	}
	if !c.Done("tx1") {
		t.Fatal("abort should have been delivered to both shards")
	}
	if got := p1.Balance(); got != 100 {
		t.Errorf("s1 balance = %d, want 100 (unchanged)", got)
	}
	if got := p2.Balance(); got != 100 {
		t.Errorf("s2 balance = %d, want 100 (unchanged)", got)
	}
	if s := p1.State("tx1"); s != TxAborted {
		t.Errorf("s1 state = %q, want aborted", s)
	}
	if s := p2.State("tx1"); s != TxAborted {
		t.Errorf("s2 state = %q, want aborted", s)
	}
}

// Boundary case: while prepared, reads see the old value; the pending
// change becomes visible only at commit.
func TestReadsSeeOldValueDuringPrepare(t *testing.T) {
	dir := t.TempDir()
	p, err := OpenParticipant("s1", filepath.Join(dir, "s1.log"), 100)
	if err != nil {
		t.Fatalf("open participant: %v", err)
	}

	yes, err := p.Prepare("tx", -40)
	if err != nil || !yes {
		t.Fatalf("prepare = %v, %v; want true, nil", yes, err)
	}
	if got := p.Balance(); got != 100 {
		t.Errorf("balance during prepare = %d, want old value 100", got)
	}
	if d, ok := p.Pending("tx"); !ok || d != -40 {
		t.Errorf("pending = %d, %v; want -40, true", d, ok)
	}
	if err := p.Commit("tx"); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if got := p.Balance(); got != 60 {
		t.Errorf("balance after commit = %d, want 60", got)
	}
}

// Interleaved case: coordinator restarts after begin but before any
// decision was persisted; recovery must persist an abort decision, and the
// prepared shards must be released.
func TestRestartWithoutDecisionPersistsAbort(t *testing.T) {
	p1, p2, l1, l2, c1, dir := twoShards(t)

	if err := c1.Begin("tx", map[string]int{"s1": -30, "s2": 30}); err != nil {
		t.Fatalf("begin: %v", err)
	}
	// Prepare phase reaches both shards, then the coordinator "crashes"
	// before persisting any decision.
	if yes, err := l1.Prepare("tx", -30); err != nil || !yes {
		t.Fatalf("prepare s1 = %v, %v", yes, err)
	}
	if yes, err := l2.Prepare("tx", 30); err != nil || !yes {
		t.Fatalf("prepare s2 = %v, %v", yes, err)
	}

	// Reopen the coordinator from its log: no decision was persisted, so
	// recovery must persist abort.
	parts := map[string]ParticipantRPC{"s1": l1, "s2": l2}
	c2, err := OpenCoordinator(filepath.Join(dir, "coord.log"), parts)
	if err != nil {
		t.Fatalf("reopen coordinator: %v", err)
	}
	if d, ok := c2.DecisionOf("tx"); !ok || d != DecisionAbort {
		t.Fatalf("decision after restart = %q, %v; want abort, true", d, ok)
	}
	if !c2.Deliver("tx") {
		t.Fatal("abort delivery should succeed")
	}
	if got := p1.Balance(); got != 100 {
		t.Errorf("s1 balance = %d, want 100", got)
	}
	if got := p2.Balance(); got != 100 {
		t.Errorf("s2 balance = %d, want 100", got)
	}
	if _, ok := p1.Pending("tx"); ok {
		t.Error("s1 should have no pending change after abort")
	}
}

// Interleaved case: a prepare reply is lost after the participant persisted
// its prepare record, so the coordinator aborts. The participant is then
// unreachable, restarts from its own log while the coordinator cannot be
// asked, and must retain its pending (prepared) state until the abort
// finally arrives.
func TestPrepareReplyLostAndParticipantRestart(t *testing.T) {
	_, p2, l1, _, c, dir := twoShards(t)

	if err := c.Begin("tx", map[string]int{"s1": -30, "s2": 30}); err != nil {
		t.Fatalf("begin: %v", err)
	}
	// s1 persists the prepare, but the reply is lost.
	l1.DropNextReply = true
	if err := c.Decide("tx"); err != nil {
		t.Fatalf("decide: %v", err)
	}
	if d, ok := c.DecisionOf("tx"); !ok || d != DecisionAbort {
		t.Fatalf("decision = %q, %v; want abort, true", d, ok)
	}

	// s1 is now unreachable, so the abort cannot be delivered to it.
	l1.Down = true
	if c.Deliver("tx") {
		t.Fatal("delivery should be incomplete while s1 is down")
	}
	if s := p2.State("tx"); s != TxAborted {
		t.Errorf("s2 state = %q, want aborted", s)
	}

	// s1 restarts from its own log while the coordinator cannot be asked:
	// it must keep the pending state and still show the old balance.
	p1b, err := OpenParticipant("s1", filepath.Join(dir, "s1.log"), 0)
	if err != nil {
		t.Fatalf("reopen s1: %v", err)
	}
	if s := p1b.State("tx"); s != TxPrepared {
		t.Errorf("restarted s1 state = %q, want prepared", s)
	}
	if got := p1b.Balance(); got != 100 {
		t.Errorf("restarted s1 balance = %d, want old value 100", got)
	}
	if d, ok := p1b.Pending("tx"); !ok || d != -30 {
		t.Errorf("restarted s1 pending = %d, %v; want -30, true", d, ok)
	}

	// The link heals; the abort is delivered and the pending state clears.
	l1.P = p1b
	l1.Down = false
	if !c.Deliver("tx") {
		t.Fatal("delivery should complete once s1 is back")
	}
	if s := p1b.State("tx"); s != TxAborted {
		t.Errorf("s1 state after abort = %q, want aborted", s)
	}
	if got := p1b.Balance(); got != 100 {
		t.Errorf("s1 balance after abort = %d, want 100", got)
	}
}

// Boundary case: re-sent messages must be idempotent; a commit is applied
// exactly once no matter how many times it is delivered.
func TestDuplicateDeliveryAppliesOnce(t *testing.T) {
	p1, p2, _, _, c, _ := twoShards(t)

	if err := c.Submit("tx1", map[string]int{"s1": -30, "s2": 30}); err != nil {
		t.Fatalf("submit: %v", err)
	}
	// Re-deliver the decision and re-send prepare/commit directly.
	c.Deliver("tx1")
	if err := p1.Commit("tx1"); err != nil {
		t.Fatalf("duplicate commit: %v", err)
	}
	if yes, err := p2.Prepare("tx1", 30); err != nil || !yes {
		t.Fatalf("duplicate prepare = %v, %v", yes, err)
	}
	if got := p1.Balance(); got != 70 {
		t.Errorf("s1 balance = %d, want 70 (applied exactly once)", got)
	}
	if got := p2.Balance(); got != 130 {
		t.Errorf("s2 balance = %d, want 130 (applied exactly once)", got)
	}
}

// Headline interleaved scenario: shard 1 applies the commit but its reply
// is lost; shard 2 is down and only recovers later from its own log. Both
// sides must end up applying the change exactly once, and the
// coordinator's final state must remain queryable (including across a
// coordinator restart in the middle).
func TestCommitReplyLostAndSecondShardRecoversLater(t *testing.T) {
	p1, _, l1, l2, c1, dir := twoShards(t)

	if err := c1.Begin("tx", map[string]int{"s1": 50, "s2": -50}); err != nil {
		t.Fatalf("begin: %v", err)
	}
	if err := c1.Decide("tx"); err != nil {
		t.Fatalf("decide: %v", err)
	}
	if d, ok := c1.DecisionOf("tx"); !ok || d != DecisionCommit {
		t.Fatalf("decision = %q, %v; want commit, true", d, ok)
	}

	// Shard 1 applies the commit but the reply is lost; shard 2 is down.
	l1.DropNextReply = true
	l2.Down = true
	if c1.Deliver("tx") {
		t.Fatal("delivery should be incomplete: s1 reply lost, s2 down")
	}
	if got := p1.Balance(); got != 150 {
		t.Errorf("s1 balance = %d, want 150 (commit applied)", got)
	}

	// The coordinator restarts; the persisted commit decision must survive.
	parts := map[string]ParticipantRPC{"s1": l1, "s2": l2}
	c2, err := OpenCoordinator(filepath.Join(dir, "coord.log"), parts)
	if err != nil {
		t.Fatalf("reopen coordinator: %v", err)
	}
	if d, ok := c2.DecisionOf("tx"); !ok || d != DecisionCommit {
		t.Fatalf("decision after restart = %q, %v; want commit, true", d, ok)
	}
	if c2.Deliver("tx") {
		t.Fatal("delivery should still be incomplete while s2 is down")
	}

	// Shard 2 recovers much later from its own log: still prepared, still
	// showing the old balance.
	p2b, err := OpenParticipant("s2", filepath.Join(dir, "s2.log"), 0)
	if err != nil {
		t.Fatalf("reopen s2: %v", err)
	}
	if s := p2b.State("tx"); s != TxPrepared {
		t.Errorf("recovered s2 state = %q, want prepared", s)
	}
	if got := p2b.Balance(); got != 100 {
		t.Errorf("recovered s2 balance = %d, want old value 100", got)
	}

	// The link heals; the coordinator re-delivers. Shard 1 gets a duplicate
	// commit (must not apply twice), shard 2 applies it for the first time.
	l2.P = p2b
	l2.Down = false
	if !c2.Deliver("tx") {
		t.Fatal("delivery should complete once s2 recovers")
	}
	if got := p1.Balance(); got != 150 {
		t.Errorf("s1 final balance = %d, want 150 (applied exactly once)", got)
	}
	if got := p2b.Balance(); got != 50 {
		t.Errorf("s2 final balance = %d, want 50 (applied exactly once)", got)
	}
	if !c2.Done("tx") {
		t.Error("coordinator should report the transaction done")
	}
	// The final state remains queryable.
	if d, ok := c2.DecisionOf("tx"); !ok || d != DecisionCommit {
		t.Errorf("final decision = %q, %v; want commit, true", d, ok)
	}
}
