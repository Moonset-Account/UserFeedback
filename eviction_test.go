package eviction

import (
	"fmt"
	"reflect"
	"testing"
)

// TestAdmitPrefersTwoSingleBudgetPods covers the scenario from the spec:
// two budgets each have exactly one slot left; among the candidates one Pod
// occupies both budgets and two Pods occupy one budget each. The controller
// must admit the latter two (size 2 beats size 1).
func TestAdmitPrefersTwoSingleBudgetPods(t *testing.T) {
	s := Snapshot{
		Pods: []Pod{
			{Name: "shared", Labels: map[string]string{"db": "1", "cache": "1"}, Ready: true},
			{Name: "pod-a", Labels: map[string]string{"db": "1"}, Ready: true},
			{Name: "pod-b", Labels: map[string]string{"cache": "1"}, Ready: true},
			{Name: "filler-1", Labels: map[string]string{"db": "1"}, Ready: true},
			{Name: "filler-2", Labels: map[string]string{"cache": "1"}, Ready: true},
		},
		Budgets: []Budget{
			{Name: "b-db", Selector: map[string]string{"db": "1"}, MinAvailable: 2},
			{Name: "b-cache", Selector: map[string]string{"cache": "1"}, MinAvailable: 2},
		},
	}
	c := NewController()
	got := c.Admit(s, []string{"shared", "pod-a", "pod-b"})
	want := []string{"pod-a", "pod-b"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Admit() = %v, want %v", got, want)
	}
}

// TestNoSlotsLeftRejectsAll is the must-be-blocked case: ready replicas
// already equal MinAvailable, so any eviction of a matching Pod violates the
// budget and the round must admit nothing.
func TestNoSlotsLeftRejectsAll(t *testing.T) {
	s := Snapshot{
		Pods: []Pod{
			{Name: "w1", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "w2", Labels: map[string]string{"app": "web"}, Ready: true},
		},
		Budgets: []Budget{
			{Name: "b-web", Selector: map[string]string{"app": "web"}, MinAvailable: 2},
		},
	}
	c := NewController()
	if got := c.Admit(s, []string{"w1", "w2"}); len(got) != 0 {
		t.Fatalf("Admit() = %v, want empty (budget has no slots)", got)
	}
}

// TestBoundaryExactSlotAndTieBreak is the boundary case: ready = MinAvailable
// + 1 allows exactly one eviction, never two; among equal-size feasible sets
// the lexicographically smallest sorted name sequence wins.
func TestBoundaryExactSlotAndTieBreak(t *testing.T) {
	s := Snapshot{
		Pods: []Pod{
			{Name: "keep-1", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "keep-2", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "cand-c", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "cand-a", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "cand-b", Labels: map[string]string{"app": "web"}, Ready: true},
		},
		Budgets: []Budget{
			{Name: "b-web", Selector: map[string]string{"app": "web"}, MinAvailable: 4},
		},
	}
	c := NewController()
	got := c.Admit(s, []string{"cand-c", "cand-a", "cand-b"})
	want := []string{"cand-a"} // exactly one slot; lexicographically smallest wins
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Admit() = %v, want %v", got, want)
	}
}

// TestSameSnapshotTwiceDoesNotDoubleSpend: a caller applying twice with the
// same snapshot must not spend the same budget twice. The first Admit
// records the approval; the second call sees the slot already consumed even
// though the approved Pod still shows ready in the snapshot.
func TestSameSnapshotTwiceDoesNotDoubleSpend(t *testing.T) {
	s := Snapshot{
		Pods: []Pod{
			{Name: "w1", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "w2", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "w3", Labels: map[string]string{"app": "web"}, Ready: true},
		},
		Budgets: []Budget{
			{Name: "b-web", Selector: map[string]string{"app": "web"}, MinAvailable: 2},
		},
	}
	c := NewController()
	first := c.Admit(s, []string{"w1", "w2", "w3"})
	if want := []string{"w1"}; !reflect.DeepEqual(first, want) {
		t.Fatalf("first Admit() = %v, want %v", first, want)
	}
	second := c.Admit(s, []string{"w2", "w3"})
	if len(second) != 0 {
		t.Fatalf("second Admit() = %v, want empty (budget already spent by first call)", second)
	}
}

// TestApprovedReplicaDeductedOnceAcrossReadyTransition: an approved Pod that
// is still ready in the snapshot is deducted from the ready count; once it
// turns unready it must not be deducted a second time. With MinAvailable=1
// and three ready replicas there are two slots; after approving one Pod and
// observing it unready, exactly one slot must remain (not zero).
func TestApprovedReplicaDeductedOnceAcrossReadyTransition(t *testing.T) {
	budget := Budget{Name: "b-web", Selector: map[string]string{"app": "web"}, MinAvailable: 1}
	s1 := Snapshot{
		Pods: []Pod{
			{Name: "w1", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "w2", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "w3", Labels: map[string]string{"app": "web"}, Ready: true},
		},
		Budgets: []Budget{budget},
	}
	c := NewController()
	if got := c.Admit(s1, []string{"w1"}); !reflect.DeepEqual(got, []string{"w1"}) {
		t.Fatalf("first Admit() = %v, want [w1]", got)
	}

	// w1 has now become unready; it is absent from the ready count already,
	// so deducting the approval again would wrongly consume the last slot.
	s2 := Snapshot{
		Pods: []Pod{
			{Name: "w1", Labels: map[string]string{"app": "web"}, Ready: false},
			{Name: "w2", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "w3", Labels: map[string]string{"app": "web"}, Ready: true},
		},
		Budgets: []Budget{budget},
	}
	got := c.Admit(s2, []string{"w2", "w3"})
	if want := []string{"w2"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Admit() after ready->unready transition = %v, want %v (approval deducted once)", got, want)
	}
}

// TestCandidateCapAtSixteen: a round considers at most 16 candidates; with
// no budgets constraining, exactly the 16 lexicographically smallest ready
// candidates are admitted.
func TestCandidateCapAtSixteen(t *testing.T) {
	var pods []Pod
	var candidates []string
	for i := 0; i < 17; i++ {
		name := fmt.Sprintf("p%02d", i)
		pods = append(pods, Pod{Name: name, Labels: map[string]string{"app": "x"}, Ready: true})
		candidates = append(candidates, name)
	}
	s := Snapshot{Pods: pods}
	c := NewController()
	got := c.Admit(s, candidates)
	if len(got) != MaxCandidates {
		t.Fatalf("Admit() admitted %d Pods, want %d", len(got), MaxCandidates)
	}
	want := candidates[:MaxCandidates] // p00..p15
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Admit() = %v, want %v", got, want)
	}
}

// TestUnreadyAndUnknownCandidatesDropped: candidates that are not ready or
// not present in the snapshot cannot be evicted and are ignored.
func TestUnreadyAndUnknownCandidatesDropped(t *testing.T) {
	s := Snapshot{
		Pods: []Pod{
			{Name: "ready-1", Labels: map[string]string{"app": "web"}, Ready: true},
			{Name: "down-1", Labels: map[string]string{"app": "web"}, Ready: false},
		},
	}
	c := NewController()
	got := c.Admit(s, []string{"ghost", "down-1", "ready-1", "ready-1"})
	if want := []string{"ready-1"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Admit() = %v, want %v", got, want)
	}
}
