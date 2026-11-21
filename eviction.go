// Package eviction implements the cluster controller's per-round selection
// of ready Pods that may be evicted simultaneously while a worker node is
// under maintenance.
//
// Budgets select Pods by labels and require a minimum number of ready
// replicas cluster-wide. Replicas already approved for eviction that are
// still ready in the observed snapshot are deducted up front; once such a
// replica turns unready it is simply absent from the ready count, so it is
// deducted exactly once in total. A round considers at most MaxCandidates
// candidates and picks the largest feasible set; ties are broken by the
// lexicographically smallest sorted name sequence.
package eviction

import "sort"

// MaxCandidates is the maximum number of candidates considered per round.
const MaxCandidates = 16

// Pod is a single observed replica.
type Pod struct {
	Name   string
	Labels map[string]string
	Ready  bool
}

// Budget selects Pods by labels and requires at least MinAvailable ready
// replicas to remain cluster-wide.
type Budget struct {
	Name         string
	Selector     map[string]string
	MinAvailable int
}

// Snapshot is one consistent observation of the cluster.
type Snapshot struct {
	Pods    []Pod
	Budgets []Budget
}

// Controller records eviction approvals so that repeated requests against
// the same snapshot cannot spend the same budget twice.
type Controller struct {
	approved map[string]struct{}
}

// NewController returns a Controller with no prior approvals.
func NewController() *Controller {
	return &Controller{approved: make(map[string]struct{})}
}

func matches(selector, labels map[string]string) bool {
	for k, v := range selector {
		if labels[k] != v {
			return false
		}
	}
	return true
}

// effectiveReady counts ready Pods matching the budget's selector that have
// not already been approved for eviction. Approved Pods still ready in the
// snapshot are deducted here; approved Pods that have since become unready
// are already absent from the ready count, so each approved replica is
// deducted exactly once.
func effectiveReady(s Snapshot, b Budget, approved map[string]struct{}) int {
	n := 0
	for _, p := range s.Pods {
		if !p.Ready || !matches(b.Selector, p.Labels) {
			continue
		}
		if _, ok := approved[p.Name]; ok {
			continue
		}
		n++
	}
	return n
}

// normalizeCandidates dedupes the candidate names, keeps only Pods that
// exist in the snapshot and are ready, sorts them, and caps the round at
// MaxCandidates.
func normalizeCandidates(s Snapshot, candidates []string) ([]string, map[string]Pod) {
	podByName := make(map[string]Pod, len(s.Pods))
	for _, p := range s.Pods {
		podByName[p.Name] = p
	}
	seen := make(map[string]struct{}, len(candidates))
	var names []string
	for _, name := range candidates {
		if _, dup := seen[name]; dup {
			continue
		}
		seen[name] = struct{}{}
		p, ok := podByName[name]
		if !ok || !p.Ready {
			continue
		}
		names = append(names, name)
	}
	sort.Strings(names)
	if len(names) > MaxCandidates {
		names = names[:MaxCandidates]
	}
	return names, podByName
}

// feasible reports whether evicting the whole subset simultaneously keeps
// every budget at or above its MinAvailable. The subset contains ready Pod
// names not yet approved.
func feasible(s Snapshot, approved map[string]struct{}, podByName map[string]Pod, subset []string) bool {
	for _, b := range s.Budgets {
		slots := effectiveReady(s, b, approved) - b.MinAvailable
		need := 0
		for _, name := range subset {
			if matches(b.Selector, podByName[name].Labels) {
				need++
			}
		}
		if need > slots {
			return false
		}
	}
	return true
}

// lessNames compares two sorted name sequences lexicographically.
func lessNames(a, b []string) bool {
	for i := 0; i < len(a) && i < len(b); i++ {
		if a[i] != b[i] {
			return a[i] < b[i]
		}
	}
	return len(a) < len(b)
}

// Select returns the largest subset of candidates that may be evicted
// simultaneously without violating any budget, given the approvals already
// recorded. Ties are broken by the lexicographically smallest sorted name
// sequence. The result is sorted by name.
func Select(s Snapshot, approved map[string]struct{}, candidates []string) []string {
	names, podByName := normalizeCandidates(s, candidates)
	n := len(names)

	var best []string
	// Enumerate all subsets; n <= MaxCandidates (16) so this is bounded.
	for mask := 0; mask < (1 << n); mask++ {
		var subset []string
		for i := 0; i < n; i++ {
			if mask&(1<<i) != 0 {
				subset = append(subset, names[i])
			}
		}
		if len(subset) < len(best) {
			continue
		}
		if len(subset) == len(best) && !lessNames(subset, best) {
			continue
		}
		if feasible(s, approved, podByName, subset) {
			best = subset
		}
	}
	if best == nil {
		return []string{}
	}
	return best
}

// Admit selects the largest feasible eviction set for this round, records
// the approvals, and returns the admitted Pod names (sorted). Recording the
// approvals means a second call against the same snapshot sees the budget
// already spent and cannot spend it again.
func (c *Controller) Admit(s Snapshot, candidates []string) []string {
	sel := Select(s, c.approved, candidates)
	for _, name := range sel {
		c.approved[name] = struct{}{}
	}
	return sel
}
