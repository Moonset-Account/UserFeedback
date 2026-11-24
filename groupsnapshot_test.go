package groupsnapshot

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

func mustWrite(t *testing.T, c *Coordinator, seq Seq, volumes ...VolumeID) {
	t.Helper()
	if err := c.AppendWrite(seq, volumes); err != nil {
		t.Fatalf("AppendWrite(%d, %v): %v", seq, volumes, err)
	}
}

func mustPersist(t *testing.T, c *Coordinator, volume VolumeID, seq Seq) {
	t.Helper()
	if err := c.MarkPersisted(volume, seq); err != nil {
		t.Fatalf("MarkPersisted(%s, %d): %v", volume, seq, err)
	}
}

func mustAck(t *testing.T, c *Coordinator, reqID string, volume VolumeID, root Root) {
	t.Helper()
	if err := c.Ack(reqID, volume, root); err != nil {
		t.Fatalf("Ack(%s, %s, %q): %v", reqID, volume, root, err)
	}
}

func assertErrorIs(t *testing.T, err error, target error) {
	t.Helper()
	if !errors.Is(err, target) {
		t.Fatalf("expected error wrapping %v, got %v", target, err)
	}
}

func assertRoots(t *testing.T, got map[VolumeID]Root, want map[VolumeID]Root) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("root set size = %d, want %d (got %v, want %v)", len(got), len(want), got, want)
	}
	for v, wantRoot := range want {
		if got[v] != wantRoot {
			t.Fatalf("root for %s = %q, want %q (full got %v)", v, got[v], wantRoot, got)
		}
	}
}

// TestNormalGroupSnapshotAndRecovery covers the happy path: derived required
// set, state progression, fixed roots, and group recovery once every required
// volume confirms durability at the pinned point.
func TestNormalGroupSnapshotAndRecovery(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a", "vol-b")
	mustWrite(t, c, 2, "vol-b", "vol-c")

	view, existed, err := c.RequestSnapshot("snap-1", 2)
	if err != nil || existed {
		t.Fatalf("RequestSnapshot: view=%+v existed=%v err=%v", view, existed, err)
	}
	if view.State != StatePending {
		t.Fatalf("initial state = %s, want pending", view.State)
	}
	wantRequired := []VolumeID{"vol-a", "vol-b", "vol-c"}
	if len(view.RequiredVolumes) != len(wantRequired) {
		t.Fatalf("required = %v, want %v", view.RequiredVolumes, wantRequired)
	}
	for i, v := range wantRequired {
		if view.RequiredVolumes[i] != v {
			t.Fatalf("required[%d] = %s, want %s (full %v)", i, view.RequiredVolumes[i], v, view.RequiredVolumes)
		}
	}

	// Recovery must be impossible while acknowledgments are outstanding.
	if _, err := c.Recover("snap-1"); !errors.Is(err, ErrSnapshotNotRecoverable) {
		t.Fatalf("partial Recover err = %v, want ErrSnapshotNotRecoverable", err)
	}

	mustPersist(t, c, "vol-a", 2)
	mustPersist(t, c, "vol-b", 2)
	mustAck(t, c, "snap-1", "vol-a", "root-a@2")
	mustAck(t, c, "snap-1", "vol-b", "root-b@2")

	view, err = c.Snapshot("snap-1")
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if view.State != StateAcknowledging {
		t.Fatalf("state after 2 acks = %s, want acknowledging", view.State)
	}
	if len(view.Roots) != 2 {
		t.Fatalf("roots = %v, want 2 entries", view.Roots)
	}

	mustPersist(t, c, "vol-c", 2)
	mustAck(t, c, "snap-1", "vol-c", "root-c@2")

	view, err = c.Snapshot("snap-1")
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if view.State != StateComplete {
		t.Fatalf("state after all acks = %s, want complete", view.State)
	}
	if view.CompletedAt.IsZero() {
		t.Fatal("CompletedAt should be set on completion")
	}

	roots, err := c.Recover("snap-1")
	if err != nil {
		t.Fatalf("Recover: %v", err)
	}
	assertRoots(t, roots, map[VolumeID]Root{
		"vol-a": "root-a@2",
		"vol-b": "root-b@2",
		"vol-c": "root-c@2",
	})
}

// TestRequiredSetDerivedFromPoint ensures a snapshot only requires volumes
// involved in writes up to its pinned point.
func TestRequiredSetDerivedFromPoint(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a", "vol-b")
	mustWrite(t, c, 2, "vol-b", "vol-c")
	mustWrite(t, c, 3, "vol-d")

	view, _, err := c.RequestSnapshot("early", 1)
	if err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	if len(view.RequiredVolumes) != 2 ||
		view.RequiredVolumes[0] != "vol-a" || view.RequiredVolumes[1] != "vol-b" {
		t.Fatalf("required at point 1 = %v, want [vol-a vol-b]", view.RequiredVolumes)
	}

	view, _, err = c.RequestSnapshot("mid", 2)
	if err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	if len(view.RequiredVolumes) != 3 {
		t.Fatalf("required at point 2 = %v, want 3 volumes", view.RequiredVolumes)
	}
	for _, v := range []VolumeID{"vol-a", "vol-b", "vol-c"} {
		if _, ok := view.Roots[v]; ok {
			t.Fatalf("no root should be recorded yet for %s", v)
		}
	}
}

// TestEarlyAckVolumesContinueWriting verifies that an acknowledged volume may
// accept later writes while the snapshot query keeps returning the fixed root.
func TestEarlyAckVolumesContinueWriting(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a", "vol-b")
	mustWrite(t, c, 2, "vol-a")

	view, _, err := c.RequestSnapshot("snap", 2)
	if err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	mustPersist(t, c, "vol-a", 2)
	mustPersist(t, c, "vol-b", 2)
	mustAck(t, c, "snap", "vol-a", "root-a@2")

	// New writes land after the snapshot point and must not perturb it.
	mustWrite(t, c, 3, "vol-a", "vol-b")
	mustWrite(t, c, 4, "vol-a")

	view, err = c.Snapshot("snap")
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if view.Roots["vol-a"] != "root-a@2" {
		t.Fatalf("fixed root changed to %q", view.Roots["vol-a"])
	}
	if len(view.Roots) != 1 {
		t.Fatalf("roots = %v, only vol-a should be present", view.Roots)
	}

	// A later snapshot pins the new write point independently.
	view2, existed, err := c.RequestSnapshot("snap-later", 4)
	if err != nil || existed {
		t.Fatalf("RequestSnapshot later: existed=%v err=%v", existed, err)
	}
	if len(view2.RequiredVolumes) != 2 {
		t.Fatalf("later required = %v, want vol-a and vol-b", view2.RequiredVolumes)
	}
}

// TestDurabilityGateRejectsPrematureAck verifies a volume cannot acknowledge
// until its relevant writes up to the point are marked persisted.
func TestDurabilityGateRejectsPrematureAck(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a", "vol-b")
	mustWrite(t, c, 2, "vol-a")

	if _, _, err := c.RequestSnapshot("snap", 2); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}

	// Nothing persisted yet.
	assertErrorIs(t, c.Ack("snap", "vol-a", "root"), ErrNotDurable)

	mustPersist(t, c, "vol-a", 1)
	// vol-a also participated in write 2, so watermark 1 is insufficient.
	assertErrorIs(t, c.Ack("snap", "vol-a", "root"), ErrNotDurable)

	// Re-marking the same watermark is an idempotent no-op.
	mustPersist(t, c, "vol-a", 1)

	mustPersist(t, c, "vol-a", 2)
	mustAck(t, c, "snap", "vol-a", "root-a@2")

	// vol-b's relevant tail is write 1.
	assertErrorIs(t, c.Ack("snap", "vol-b", "root"), ErrNotDurable)
	mustPersist(t, c, "vol-b", 1)
	mustAck(t, c, "snap", "vol-b", "root-b@1")

	view, err := c.Snapshot("snap")
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if view.State != StateComplete {
		t.Fatalf("state = %s, want complete", view.State)
	}
}

// TestRetryReturnsDeterminedState verifies idempotent retries: the same
// request ID always returns the state it already determined, even after
// cancellation or completion, and cannot be repinned to another point.
func TestRetryReturnsDeterminedState(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a")
	mustWrite(t, c, 2, "vol-a", "vol-b")

	first, existed, err := c.RequestSnapshot("snap", 1)
	if err != nil || existed {
		t.Fatalf("first request: existed=%v err=%v", existed, err)
	}
	if first.Point != 1 {
		t.Fatalf("point = %d, want 1", first.Point)
	}

	retry, existed, err := c.RequestSnapshot("snap", 1)
	if err != nil || !existed {
		t.Fatalf("retry: existed=%v err=%v", existed, err)
	}
	if retry.RequestID != "snap" || retry.Point != 1 || retry.State != StatePending {
		t.Fatalf("retry view = %+v, want identical pending request", retry)
	}

	// Retrying with a different point must not mutate the original.
	if _, _, err := c.RequestSnapshot("snap", 2); err == nil {
		t.Fatal("retry with different point should fail")
	}
	view, _ := c.Snapshot("snap")
	if view.Point != 1 {
		t.Fatalf("point mutated to %d", view.Point)
	}

	if err := c.Cancel("snap"); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	retry, existed, err = c.RequestSnapshot("snap", 1)
	if err != nil || !existed {
		t.Fatalf("retry after cancel: existed=%v err=%v", existed, err)
	}
	if retry.State != StateCancelled || retry.CancelledAt.IsZero() {
		t.Fatalf("retry after cancel view = %+v, want cancelled", retry)
	}
}

// TestCancellationAndLateAckAttribution verifies late acknowledgments survive
// cancellation with their original attribution while the state stays terminal.
func TestCancellationAndLateAckAttribution(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a", "vol-b")
	if _, _, err := c.RequestSnapshot("snap", 1); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	mustPersist(t, c, "vol-a", 1)
	mustPersist(t, c, "vol-b", 1)
	mustAck(t, c, "snap", "vol-a", "root-a@1")

	if err := c.Cancel("snap"); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	view, _ := c.Snapshot("snap")
	if view.State != StateCancelled {
		t.Fatalf("state = %s, want cancelled", view.State)
	}
	// Cancelling twice is a no-op.
	if err := c.Cancel("snap"); err != nil {
		t.Fatalf("second Cancel: %v", err)
	}

	// Late acknowledgment from the remaining required volume.
	err := c.Ack("snap", "vol-b", "root-b@1-late")
	assertErrorIs(t, err, ErrRequestCancelled)

	view, _ = c.Snapshot("snap")
	if view.State != StateCancelled {
		t.Fatalf("state after late ack = %s, want cancelled", view.State)
	}
	// Late roots must not appear in the queryable snapshot roots.
	if len(view.Roots) != 1 || view.Roots["vol-a"] != "root-a@1" {
		t.Fatalf("roots after late ack = %v", view.Roots)
	}

	late, err := c.LateAcks("snap")
	if err != nil {
		t.Fatalf("LateAcks: %v", err)
	}
	if len(late) != 1 {
		t.Fatalf("late ack count = %d, want 1", len(late))
	}
	got := late[0]
	if got.RequestID != "snap" || got.Volume != "vol-b" ||
		got.Root != "root-b@1-late" || got.Reason != LateAfterCancellation || got.At.IsZero() {
		t.Fatalf("late ack = %+v, want original attribution", got)
	}

	// A cancelled group never becomes recoverable, even once all volumes
	// have effectively confirmed.
	if _, err := c.Recover("snap"); !errors.Is(err, ErrSnapshotNotRecoverable) {
		t.Fatalf("Recover after cancel err = %v, want ErrSnapshotNotRecoverable", err)
	}

	// Re-sending the late acknowledgment with the same root is an idempotent
	// no-op and does not duplicate the record; a conflicting root is rejected.
	if err := c.Ack("snap", "vol-b", "root-b@1-late"); err != nil {
		t.Fatalf("duplicate late ack err = %v, want nil idempotent no-op", err)
	}
	if late, _ := c.LateAcks("snap"); len(late) != 1 {
		t.Fatalf("duplicate late ack recorded %d entries", len(late))
	}
	if err := c.Ack("snap", "vol-b", "other-root"); !errors.Is(err, ErrConflictingRoot) {
		t.Fatalf("conflicting late ack err = %v, want ErrConflictingRoot", err)
	}
}

// TestLateAckNotAppliedToOtherRequest ensures an acknowledgment carrying a
// cancelled request ID is never routed to another request.
func TestLateAckNotAppliedToOtherRequest(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a", "vol-b")

	if _, _, err := c.RequestSnapshot("old", 1); err != nil {
		t.Fatalf("RequestSnapshot old: %v", err)
	}
	if _, _, err := c.RequestSnapshot("new", 1); err != nil {
		t.Fatalf("RequestSnapshot new: %v", err)
	}
	mustPersist(t, c, "vol-a", 1)
	mustPersist(t, c, "vol-b", 1)
	if err := c.Cancel("old"); err != nil {
		t.Fatalf("Cancel old: %v", err)
	}

	// The late ack explicitly names the cancelled request.
	assertErrorIs(t, c.Ack("old", "vol-a", "old-root"), ErrRequestCancelled)

	oldView, _ := c.Snapshot("old")
	newView, _ := c.Snapshot("new")
	if len(oldView.Roots) != 0 {
		t.Fatalf("old request roots = %v, late ack must stay in LateAcks", oldView.Roots)
	}
	if len(newView.Roots) != 0 {
		t.Fatalf("new request roots = %v, must not receive old request's ack", newView.Roots)
	}
	if late, _ := c.LateAcks("old"); len(late) != 1 || late[0].RequestID != "old" {
		t.Fatalf("old late acks = %v", late)
	}
	if late, _ := c.LateAcks("new"); len(late) != 0 {
		t.Fatalf("new request unexpectedly has late acks: %v", late)
	}

	// The new request can still complete normally.
	mustAck(t, c, "new", "vol-a", "new-root-a")
	mustAck(t, c, "new", "vol-b", "new-root-b")
	roots, err := c.Recover("new")
	if err != nil {
		t.Fatalf("Recover new: %v", err)
	}
	assertRoots(t, roots, map[VolumeID]Root{"vol-a": "new-root-a", "vol-b": "new-root-b"})
}

// TestDuplicateAndConflictingAcks covers idempotent retransmission and root
// conflicts in both active and complete states.
func TestDuplicateAndConflictingAcks(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a")
	if _, _, err := c.RequestSnapshot("snap", 1); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	mustPersist(t, c, "vol-a", 1)

	mustAck(t, c, "snap", "vol-a", "root-1")
	// Duplicate with the same root: no-op.
	mustAck(t, c, "snap", "vol-a", "root-1")
	assertErrorIs(t, c.Ack("snap", "vol-a", "root-2"), ErrConflictingRoot)

	view, _ := c.Snapshot("snap")
	if view.State != StateComplete || view.Roots["vol-a"] != "root-1" {
		t.Fatalf("view = %+v, want complete with fixed root-1", view)
	}
	// Conflicts are still rejected after completion; the fixed root wins.
	assertErrorIs(t, c.Ack("snap", "vol-a", "root-2"), ErrConflictingRoot)
	mustAck(t, c, "snap", "vol-a", "root-1")
}

// TestNonRequiredVolumeRejected verifies acknowledgments from volumes outside
// the frozen required set are refused and recorded nowhere.
func TestNonRequiredVolumeRejected(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a")
	mustWrite(t, c, 2, "vol-b") // after the pinned point
	if _, _, err := c.RequestSnapshot("snap", 1); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	mustPersist(t, c, "vol-a", 1)
	mustPersist(t, c, "vol-b", 2)

	assertErrorIs(t, c.Ack("snap", "vol-b", "root-b"), ErrVolumeNotRequired)
	if late, _ := c.LateAcks("snap"); len(late) != 0 {
		t.Fatalf("non-required ack recorded as late: %v", late)
	}

	// Empty roots are rejected regardless of volume.
	assertErrorIs(t, c.Ack("snap", "vol-a", ""), ErrEmptyRoot)

	mustAck(t, c, "snap", "vol-a", "root-a")
	view, _ := c.Snapshot("snap")
	if view.State != StateComplete {
		t.Fatalf("state = %s, want complete", view.State)
	}
}

// TestConcurrentAcksCompleteExactlyOnce hammers Ack from many goroutines and
// verifies a single coherent complete state. Run under -race.
func TestConcurrentAcksCompleteExactlyOnce(t *testing.T) {
	const n = 16
	c := NewCoordinator()
	volumes := make([]VolumeID, 0, n)
	for i := 0; i < n; i++ {
		v := VolumeID(fmt.Sprintf("vol-%02d", i))
		volumes = append(volumes, v)
	}
	mustWrite(t, c, 1, volumes...)
	if _, _, err := c.RequestSnapshot("snap", 1); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	for _, v := range volumes {
		mustPersist(t, c, v, 1)
	}

	var wg sync.WaitGroup
	errs := make(chan error, 2*n)
	for _, v := range volumes {
		v := v
		wg.Add(1)
		go func() {
			defer wg.Done()
			// Each volume also retransmits once to exercise duplicate paths.
			errs <- c.Ack("snap", v, Root("root-"+v))
			errs <- c.Ack("snap", v, Root("root-"+v))
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent Ack: %v", err)
		}
	}

	roots, err := c.Recover("snap")
	if err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if len(roots) != n {
		t.Fatalf("recovered root count = %d, want %d", len(roots), n)
	}
	for _, v := range volumes {
		if roots[v] != Root("root-"+v) {
			t.Fatalf("root for %s = %q", v, roots[v])
		}
	}
	view, _ := c.Snapshot("snap")
	if view.State != StateComplete {
		t.Fatalf("state = %s, want complete", view.State)
	}
}

// TestWaitContextExpiryAndCompletion verifies Wait reports expiry without
// cancelling the request, then unblocks with the complete view.
func TestWaitContextExpiryAndCompletion(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a")
	if _, _, err := c.RequestSnapshot("snap", 1); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	mustPersist(t, c, "vol-a", 1)

	expired := make(chan struct{})
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
		defer cancel()
		view, err := c.Wait(ctx, "snap")
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Errorf("Wait err = %v, want DeadlineExceeded", err)
		}
		if view.State != StatePending {
			t.Errorf("expired view state = %s, want pending", view.State)
		}
		close(expired)
	}()
	select {
	case <-expired:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for context expiry branch")
	}

	// Context expiry must not have cancelled the request.
	view, _ := c.Snapshot("snap")
	if view.State != StatePending {
		t.Fatalf("state after Wait expiry = %s, want pending", view.State)
	}

	done := make(chan SnapshotView, 1)
	go func() {
		v, err := c.Wait(context.Background(), "snap")
		if err != nil {
			t.Errorf("Wait after completion: %v", err)
		}
		done <- v
	}()
	mustAck(t, c, "snap", "vol-a", "root-a")
	select {
	case v := <-done:
		if v.State != StateComplete {
			t.Fatalf("waited view state = %s, want complete", v.State)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Wait did not unblock after completion")
	}

	// Waiting on an already terminal request returns immediately.
	v, err := c.Wait(context.Background(), "snap")
	if err != nil || v.State != StateComplete {
		t.Fatalf("terminal Wait: view=%+v err=%v", v, err)
	}
}

// TestWaitOnCancelledRequest verifies Wait unblocks with the cancelled view.
func TestWaitOnCancelledRequest(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a")
	if _, _, err := c.RequestSnapshot("snap", 1); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	result := make(chan SnapshotView, 1)
	go func() {
		v, err := c.Wait(context.Background(), "snap")
		if err != nil {
			t.Errorf("Wait: %v", err)
		}
		result <- v
	}()
	time.Sleep(10 * time.Millisecond)
	if err := c.Cancel("snap"); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	select {
	case v := <-result:
		if v.State != StateCancelled {
			t.Fatalf("waited view = %+v, want cancelled", v)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Wait did not unblock after cancellation")
	}
}

// TestConflictingRootWhileAcknowledging covers the conflict branch while the
// request is still open (not yet complete) and the fixed root wins.
func TestConflictingRootWhileAcknowledging(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a", "vol-b")
	if _, _, err := c.RequestSnapshot("snap", 1); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	mustPersist(t, c, "vol-a", 1)
	mustPersist(t, c, "vol-b", 1)
	mustAck(t, c, "snap", "vol-a", "fixed-a")
	// One outstanding volume keeps the request in acknowledging.
	assertErrorIs(t, c.Ack("snap", "vol-a", "other-a"), ErrConflictingRoot)
	mustAck(t, c, "snap", "vol-a", "fixed-a")
	view, _ := c.Snapshot("snap")
	if view.State != StateAcknowledging || view.Roots["vol-a"] != "fixed-a" {
		t.Fatalf("view = %+v", view)
	}
}

// TestLateAckStillRequiresDurability verifies the durability gate applies
// equally to acknowledgments arriving after cancellation: an undurable late
// ack is rejected and recorded nowhere.
func TestLateAckStillRequiresDurability(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a", "vol-b")
	if _, _, err := c.RequestSnapshot("snap", 1); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	mustPersist(t, c, "vol-a", 1)
	mustAck(t, c, "snap", "vol-a", "root-a")
	if err := c.Cancel("snap"); err != nil {
		t.Fatalf("Cancel: %v", err)
	}

	// vol-b never marked persisted: the late ack fails the same gate.
	assertErrorIs(t, c.Ack("snap", "vol-b", "late-root"), ErrNotDurable)
	if late, _ := c.LateAcks("snap"); len(late) != 0 {
		t.Fatalf("undurable late ack recorded: %v", late)
	}

	// Once durable, the late ack is retained with original attribution.
	mustPersist(t, c, "vol-b", 1)
	assertErrorIs(t, c.Ack("snap", "vol-b", "late-root"), ErrRequestCancelled)
	late, _ := c.LateAcks("snap")
	if len(late) != 1 || late[0].Volume != "vol-b" || late[0].RequestID != "snap" {
		t.Fatalf("late acks = %v", late)
	}
}

// TestStateString covers every lifecycle name and the defensive default.
func TestStateString(t *testing.T) {
	cases := []struct {
		state State
		want  string
	}{
		{StatePending, "pending"},
		{StateAcknowledging, "acknowledging"},
		{StateComplete, "complete"},
		{StateCancelled, "cancelled"},
		{State(99), "unknown-state-99"},
	}
	for _, tc := range cases {
		if got := tc.state.String(); got != tc.want {
			t.Errorf("State(%d).String() = %q, want %q", int(tc.state), got, tc.want)
		}
	}
}

// TestCancelledConflictOnPreCancelRoot verifies a root conflict from a volume
// that acknowledged before cancellation is still rejected with the fixed root
// winning.
func TestCancelledConflictOnPreCancelRoot(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a", "vol-b")
	if _, _, err := c.RequestSnapshot("snap", 1); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	mustPersist(t, c, "vol-a", 1)
	mustPersist(t, c, "vol-b", 1)
	mustAck(t, c, "snap", "vol-a", "fixed-root")
	// vol-b is still outstanding, so the request is open when cancelled.
	if err := c.Cancel("snap"); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	assertErrorIs(t, c.Ack("snap", "vol-a", "other-root"), ErrConflictingRoot)
	// The same fixed root remains queryable and idempotent retransmits work.
	mustAck(t, c, "snap", "vol-a", "fixed-root")
	view, _ := c.Snapshot("snap")
	if view.Roots["vol-a"] != "fixed-root" || view.State != StateCancelled {
		t.Fatalf("view = %+v", view)
	}
	if late, _ := c.LateAcks("snap"); len(late) != 0 {
		t.Fatalf("unexpected late acks: %v", late)
	}
}

// TestEmptyPointSnapshotImmediatelyComplete covers the boundary where the
// pinned point has no relevant writes: the empty group is recoverable at once.
func TestEmptyPointSnapshotImmediatelyComplete(t *testing.T) {
	c := NewCoordinator()
	view, existed, err := c.RequestSnapshot("empty", 0)
	if err != nil || existed {
		t.Fatalf("RequestSnapshot: existed=%v err=%v", existed, err)
	}
	if view.State != StateComplete || len(view.RequiredVolumes) != 0 {
		t.Fatalf("view = %+v, want immediately complete empty group", view)
	}
	roots, err := c.Recover("empty")
	if err != nil {
		t.Fatalf("Recover empty: %v", err)
	}
	if len(roots) != 0 {
		t.Fatalf("empty roots = %v", roots)
	}
	v, err := c.Wait(context.Background(), "empty")
	if err != nil || v.State != StateComplete {
		t.Fatalf("Wait empty: view=%+v err=%v", v, err)
	}

	// A later write does not retroactively enlarge the frozen required set.
	mustWrite(t, c, 5, "vol-a")
	view, _ = c.Snapshot("empty")
	if len(view.RequiredVolumes) != 0 || view.State != StateComplete {
		t.Fatalf("retroactively mutated view = %+v", view)
	}
}

// TestAppendWriteAndWatermarkValidation covers sequence and watermark
// boundary checks.
func TestAppendWriteAndWatermarkValidation(t *testing.T) {
	c := NewCoordinator()

	assertErrorIs(t, c.AppendWrite(1, nil), ErrEmptyVolumeSet)
	assertErrorIs(t, c.AppendWrite(1, []VolumeID{}), ErrEmptyVolumeSet)

	mustWrite(t, c, 1, "vol-a")
	mustWrite(t, c, 2, "vol-a", "vol-b")
	assertErrorIs(t, c.AppendWrite(2, []VolumeID{"vol-a"}), ErrSequenceOutOfOrder)
	assertErrorIs(t, c.AppendWrite(1, []VolumeID{"vol-a"}), ErrSequenceOutOfOrder)
	mustWrite(t, c, 3, "vol-b") // strictly greater is fine

	mustPersist(t, c, "vol-a", 3)
	mustPersist(t, c, "vol-a", 3) // idempotent
	assertErrorIs(t, c.MarkPersisted("vol-a", 2), ErrWatermarkRegression)
}

// TestUnknownRequestOperations verifies every request-scoped operation fails
// closed for unknown IDs.
func TestUnknownRequestOperations(t *testing.T) {
	c := NewCoordinator()
	assertErrorIs(t, c.Ack("nope", "vol-a", "root"), ErrRequestNotFound)
	assertErrorIs(t, c.Cancel("nope"), ErrRequestNotFound)
	if _, err := c.Snapshot("nope"); !errors.Is(err, ErrRequestNotFound) {
		t.Fatalf("Snapshot nope err = %v, want ErrRequestNotFound", err)
	}
	_, err := c.Recover("nope")
	assertErrorIs(t, err, ErrRequestNotFound)
	_, err = c.LateAcks("nope")
	assertErrorIs(t, err, ErrRequestNotFound)
	_, err = c.Wait(context.Background(), "nope")
	assertErrorIs(t, err, ErrRequestNotFound)

	if _, _, err := c.RequestSnapshot("", 1); err == nil {
		t.Fatal("empty request id should be rejected")
	}
}

// TestCancelAfterCompleteIsNoOp verifies completion is irreversible.
func TestCancelAfterCompleteIsNoOp(t *testing.T) {
	c := NewCoordinator()
	mustWrite(t, c, 1, "vol-a")
	if _, _, err := c.RequestSnapshot("snap", 1); err != nil {
		t.Fatalf("RequestSnapshot: %v", err)
	}
	mustPersist(t, c, "vol-a", 1)
	mustAck(t, c, "snap", "vol-a", "root-a")

	if err := c.Cancel("snap"); err != nil {
		t.Fatalf("Cancel after complete: %v", err)
	}
	view, _ := c.Snapshot("snap")
	if view.State != StateComplete {
		t.Fatalf("state = %s, completion must be irreversible", view.State)
	}
	roots, err := c.Recover("snap")
	if err != nil {
		t.Fatalf("Recover: %v", err)
	}
	assertRoots(t, roots, map[VolumeID]Root{"vol-a": "root-a"})
}

// TestInterleavedLifecycleSynthetic is the end-to-end synthetic scenario with
// overlapping snapshots, early acks, cancellation, late attribution and later
// writes interleaved.
func TestInterleavedLifecycleSynthetic(t *testing.T) {
	c := NewCoordinator()
	// Shared write journal with overlapping volume sets.
	mustWrite(t, c, 1, "vol-a", "vol-b")
	mustWrite(t, c, 2, "vol-b", "vol-c")
	mustWrite(t, c, 3, "vol-a", "vol-c")
	mustWrite(t, c, 4, "vol-b")

	// R1 pins point 2 -> {a, b, c}; R2 pins point 1 -> {a, b}.
	r1, _, err := c.RequestSnapshot("R1", 2)
	if err != nil {
		t.Fatalf("RequestSnapshot R1: %v", err)
	}
	if got := r1.RequiredVolumes; len(got) != 3 {
		t.Fatalf("R1 required = %v, want 3 volumes", got)
	}
	r2, _, err := c.RequestSnapshot("R2", 1)
	if err != nil {
		t.Fatalf("RequestSnapshot R2: %v", err)
	}
	if got := r2.RequiredVolumes; len(got) != 2 ||
		got[0] != "vol-a" || got[1] != "vol-b" {
		t.Fatalf("R2 required = %v, want [vol-a vol-b]", got)
	}

	for _, spec := range []struct {
		v   VolumeID
		seq Seq
	}{
		{"vol-a", 3}, {"vol-b", 4}, {"vol-c", 3},
	} {
		mustPersist(t, c, spec.v, spec.seq)
	}

	// R2 completes while R1 is still open.
	mustAck(t, c, "R2", "vol-a", "R2-a")
	mustAck(t, c, "R2", "vol-b", "R2-b")
	roots, err := c.Recover("R2")
	if err != nil {
		t.Fatalf("Recover R2: %v", err)
	}
	assertRoots(t, roots, map[VolumeID]Root{"vol-a": "R2-a", "vol-b": "R2-b"})

	// R1 gets one early ack, then is cancelled; the rest arrive late.
	mustAck(t, c, "R1", "vol-a", "R1-a")
	if err := c.Cancel("R1"); err != nil {
		t.Fatalf("Cancel R1: %v", err)
	}
	assertErrorIs(t, c.Ack("R1", "vol-b", "R1-b-late"), ErrRequestCancelled)
	assertErrorIs(t, c.Ack("R1", "vol-c", "R1-c-late"), ErrRequestCancelled)

	// Writes continue after the early ack; R2's fixed roots are unchanged.
	mustWrite(t, c, 5, "vol-a", "vol-b", "vol-c")
	view, _ := c.Snapshot("R2")
	assertRoots(t, view.Roots, map[VolumeID]Root{"vol-a": "R2-a", "vol-b": "R2-b"})
	if view.State != StateComplete {
		t.Fatalf("R2 state = %s, want complete", view.State)
	}

	// R1 keeps cancelled state and full late attribution.
	view, _ = c.Snapshot("R1")
	if view.State != StateCancelled {
		t.Fatalf("R1 state = %s, want cancelled", view.State)
	}
	if len(view.Roots) != 1 || view.Roots["vol-a"] != "R1-a" {
		t.Fatalf("R1 queryable roots = %v", view.Roots)
	}
	late, _ := c.LateAcks("R1")
	if len(late) != 2 {
		t.Fatalf("R1 late acks = %v, want 2", late)
	}
	seen := map[VolumeID]Root{}
	for _, la := range late {
		if la.RequestID != "R1" || la.Reason != LateAfterCancellation {
			t.Fatalf("bad late attribution: %+v", la)
		}
		seen[la.Volume] = la.Root
	}
	assertRoots(t, seen, map[VolumeID]Root{
		"vol-b": "R1-b-late",
		"vol-c": "R1-c-late",
	})
	if late2, _ := c.LateAcks("R2"); len(late2) != 0 {
		t.Fatalf("R2 late acks = %v, want none", late2)
	}

	// Retrying R1 returns the determined cancelled state.
	retry, existed, err := c.RequestSnapshot("R1", 2)
	if err != nil || !existed || retry.State != StateCancelled {
		t.Fatalf("R1 retry: view=%+v existed=%v err=%v", retry, existed, err)
	}

	// A fresh snapshot at the new point can still be built and recovered.
	mustPersist(t, c, "vol-a", 5)
	mustPersist(t, c, "vol-b", 5)
	mustPersist(t, c, "vol-c", 5)
	r3, _, err := c.RequestSnapshot("R3", 5)
	if err != nil {
		t.Fatalf("RequestSnapshot R3: %v", err)
	}
	if len(r3.RequiredVolumes) != 3 {
		t.Fatalf("R3 required = %v, want 3", r3.RequiredVolumes)
	}
	for _, v := range r3.RequiredVolumes {
		mustAck(t, c, "R3", v, Root("R3-"+v))
	}
	roots, err = c.Recover("R3")
	if err != nil {
		t.Fatalf("Recover R3: %v", err)
	}
	if len(roots) != 3 {
		t.Fatalf("R3 roots = %v, want 3", roots)
	}
}
