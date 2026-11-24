package egress

import (
	"errors"
	"fmt"
	"sort"
	"strings"
)

// Scenario is one possible true gate table/phase.
type Scenario struct {
	Name  string     `json:"name"`
	Table *GateTable `json:"table"`
}

// Probe is at most one hardware timestamp observation. Stamps may be supplied
// directly; if omitted for a scenario the next raw gate-edge timestamp at
// ReadAt is used.
type Probe struct {
	ReadAt int            `json:"read_at"`
	Stamps map[string]int `json:"stamps,omitempty"`
}

// PolicyConfig builds an online common-prefix policy.
type PolicyConfig struct {
	Scenarios     []Scenario `json:"scenarios"`
	Frames        []Frame    `json:"frames"`
	MinNonLast    int        `json:"min_non_last"`
	Overhead      int        `json:"overhead"`
	StartAt       int        `json:"start_at,omitempty"`
	Probes        []Probe    `json:"probes,omitempty"`
	CommitFastIDs []string   `json:"commit_fast_ids,omitempty"`
}

// ObservationEvent records when a timestamp was latched and when the port can
// first branch. A timestamp read inside a non-preemptable fragment has
// BranchAt equal to the fragment end; the on-chain fragment cannot be undone.
type ObservationEvent struct {
	ProbeIndex int `json:"probe_index"`
	LatchedAt  int `json:"latched_at"`
	BranchAt   int `json:"branch_at"`
}

// PolicyBranch is one distinct observed timestamp and the scenarios remaining
// after observing it.
type PolicyBranch struct {
	Stamp      int         `json:"stamp"`
	Candidates []string    `json:"candidates"`
	Next       *PolicyNode `json:"next"`
}

// PolicyNode is either a shared action, an observation branch, a wait, or a
// scenario-specific leaf plan.
type PolicyNode struct {
	ID          string             `json:"id"`
	Kind        string             `json:"kind"` // action|wait|observe|leaf|terminal
	At          int                `json:"at"`
	Candidates  []string           `json:"candidates"`
	Action      *Fragment          `json:"action,omitempty"`
	WaitUntil   int                `json:"wait_until,omitempty"`
	Next        *PolicyNode        `json:"next,omitempty"`
	Observation *ObservationEvent  `json:"observation,omitempty"`
	Branches    []PolicyBranch     `json:"branches,omitempty"`
	Suffix      *Plan              `json:"suffix,omitempty"`
	Outcomes    map[string]Outcome `json:"outcomes,omitempty"`
	Reason      string             `json:"reason,omitempty"`
}

// Outcome is the robust score vector for one scenario.
type Outcome struct {
	FastOnTime   int  `json:"fast_on_time"`
	NormalOnTime int  `json:"normal_on_time"`
	Overhead     int  `json:"overhead"`
	FinishSum    int  `json:"finish_sum"`
	CommittedMet bool `json:"committed_met"`
}

// Policy is the generated online decision tree.
type Policy struct {
	Root       *PolicyNode `json:"root"`
	Scenarios  []Scenario  `json:"scenarios"`
	Probes     []Probe     `json:"probes"`
	Committed  []string    `json:"committed_fast_ids"`
	frames     []Frame
	minNonLast int
	overhead   int
}

var ErrNoRobustPolicy = errors.New("no robust online policy")

type robustResult struct {
	node     *PolicyNode
	outcomes map[string]Outcome
	feasible bool
}

type policySolver struct {
	frames        []Frame
	fast          []Frame
	normal        []Frame
	scenarios     []Scenario
	probes        []Probe
	commit        map[string]bool
	memo          map[string]*robustResult
	nodeSeq       int
	overheadBytes int
	minFragment   int
}

// BuildPolicy synthesizes a common-prefix online policy. Committed fast IDs
// must meet deadlines in every possible scenario. Among those policies, the
// solver maximizes the worst-case number of on-time normal frames.
func BuildPolicy(cfg PolicyConfig) (*Policy, error) {
	if len(cfg.Scenarios) == 0 {
		return nil, fmt.Errorf("%w: no scenarios", ErrNoRobustPolicy)
	}
	if len(cfg.Probes) > 2 {
		return nil, fmt.Errorf("at most two timestamp observations are allowed, got %d", len(cfg.Probes))
	}
	base := Config{
		Table:      cfg.Scenarios[0].Table,
		Frames:     cfg.Frames,
		MinNonLast: cfg.MinNonLast,
		Overhead:   cfg.Overhead,
		StartAt:    cfg.StartAt,
	}
	frames, err := validateConfig(base)
	if err != nil {
		return nil, err
	}
	for _, sc := range cfg.Scenarios {
		if sc.Name == "" || sc.Table == nil {
			return nil, fmt.Errorf("%w: scenario has empty name or nil table", ErrNoRobustPolicy)
		}
		if err := sc.Table.Validate(); err != nil {
			return nil, fmt.Errorf("scenario %q: %w", sc.Name, err)
		}
	}
	names := map[string]bool{}
	for _, sc := range cfg.Scenarios {
		if names[sc.Name] {
			return nil, fmt.Errorf("duplicate scenario name %q", sc.Name)
		}
		names[sc.Name] = true
	}

	probes := make([]Probe, len(cfg.Probes))
	copy(probes, cfg.Probes)
	sort.SliceStable(probes, func(i, j int) bool { return probes[i].ReadAt < probes[j].ReadAt })
	for i := range probes {
		if probes[i].ReadAt < cfg.StartAt {
			return nil, fmt.Errorf("probe %d read time %d before start %d", i+1, probes[i].ReadAt, cfg.StartAt)
		}
		supplied := probes[i].Stamps
		probes[i].Stamps = map[string]int{}
		for k, v := range supplied {
			probes[i].Stamps[k] = v
		}
		for _, sc := range cfg.Scenarios {
			if _, ok := probes[i].Stamps[sc.Name]; !ok {
				probes[i].Stamps[sc.Name] = sc.Table.NextEdgeTimestamp(probes[i].ReadAt)
			}
		}
	}

	commit := map[string]bool{}
	for _, id := range cfg.CommitFastIDs {
		commit[id] = true
	}
	for _, f := range frames {
		if f.Committed {
			commit[f.ID] = true
		}
	}
	for id := range commit {
		f, ok := frameByID(frames, id)
		if !ok {
			return nil, fmt.Errorf("committed frame %q not found", id)
		}
		if f.Class != Fast {
			return nil, fmt.Errorf("committed frame %q is not fast class", id)
		}
	}

	var fast, normal []Frame
	for _, f := range frames {
		if f.Class == Fast {
			fast = append(fast, f)
		} else {
			normal = append(normal, f)
		}
	}
	ps := &policySolver{
		frames:        frames,
		fast:          fast,
		normal:        normal,
		scenarios:     cfg.Scenarios,
		probes:        probes,
		commit:        commit,
		memo:          map[string]*robustResult{},
		overheadBytes: cfg.Overhead,
		minFragment:   cfg.MinNonLast,
	}
	for _, sc := range cfg.Scenarios {
		s := &solver{cfg: Config{Table: sc.Table, Frames: cfg.Frames, MinNonLast: cfg.MinNonLast, Overhead: cfg.Overhead}, fast: fast, normal: normal}
		if err := s.precheck(); err != nil {
			return nil, err
		}
	}

	k := stateKey{t: cfg.StartAt}
	if len(fast) > 0 {
		k.frem = fast[0].Length
	}
	if len(normal) > 0 {
		k.nrem = normal[0].Length
	}
	all := make([]int, len(cfg.Scenarios))
	for i := range all {
		all[i] = i
	}
	rr := ps.solve(k, all, 0)
	if !rr.feasible {
		return nil, fmt.Errorf("%w: committed fast deadlines or queue/gate completion are infeasible in at least one scenario", ErrNoRobustPolicy)
	}
	pol := &Policy{
		Root:       rr.node,
		Scenarios:  cfg.Scenarios,
		Probes:     probes,
		frames:     frames,
		minNonLast: cfg.MinNonLast,
		overhead:   cfg.Overhead,
	}
	for id := range commit {
		pol.Committed = append(pol.Committed, id)
	}
	sort.Strings(pol.Committed)
	ps.assignIDs(pol.Root)
	return pol, nil
}

func frameByID(frames []Frame, id string) (Frame, bool) {
	for _, f := range frames {
		if f.ID == id {
			return f, true
		}
	}
	return Frame{}, false
}

func (ps *policySolver) table(i int) *GateTable { return ps.scenarios[i].Table }

func (ps *policySolver) memoKey(k stateKey, set []int, pi int) string {
	names := make([]string, 0, len(set))
	for _, i := range set {
		names = append(names, ps.scenarios[i].Name)
	}
	sort.Strings(names)
	return fmt.Sprintf("%d|%d|%d|%d|%d|%d|%s", k.fi, k.frem, k.ni, k.nrem, k.t, pi, strings.Join(names, ","))
}

func (ps *policySolver) solve(k stateKey, set []int, pi int) *robustResult {
	mk := ps.memoKey(k, set, pi)
	if r, ok := ps.memo[mk]; ok {
		return r
	}

	// A timestamp whose read tick has been reached is processed before any new
	// transmission decision at this tick. If the previous fragment crossed the
	// read tick, BranchAt is the current legal fragment boundary k.t.
	if pi < len(ps.probes) && ps.probes[pi].ReadAt <= k.t {
		r := ps.solveObservation(k, set, pi)
		ps.memo[mk] = r
		return r
	}

	if k.fi >= len(ps.fast) && k.ni >= len(ps.normal) {
		out := map[string]Outcome{}
		for _, si := range set {
			out[ps.scenarios[si].Name] = Outcome{CommittedMet: true}
		}
		node := &PolicyNode{Kind: "terminal", At: k.t, Candidates: ps.names(set), Outcomes: out}
		r := &robustResult{node: node, outcomes: out, feasible: true}
		ps.memo[mk] = r
		return r
	}

	// A uniquely identified true phase can use the exact offline optimizer.
	if len(set) == 1 {
		r := ps.solveSingleton(k, set[0])
		ps.memo[mk] = r
		return r
	}

	var candidates []*robustResult

	// Idle to the next timestamp rather than launching an action across the
	// observation opportunity.
	if pi < len(ps.probes) && ps.probes[pi].ReadAt > k.t {
		rk := k
		rk.t = ps.probes[pi].ReadAt
		child := ps.solve(rk, set, pi)
		if child.feasible {
			node := &PolicyNode{
				Kind:       "wait",
				At:         k.t,
				Candidates: ps.names(set),
				WaitUntil:  ps.probes[pi].ReadAt,
				Next:       child.node,
				Outcomes:   child.outcomes,
			}
			candidates = append(candidates, &robustResult{node: node, outcomes: child.outcomes, feasible: true})
		}
	}

	// Exact shared fragments legal in every still-possible scenario.
	for _, class := range []Class{Fast, Normal} {
		idx, rem := k.fi, k.frem
		frames := ps.fast
		if class == Normal {
			idx, rem = k.ni, k.nrem
			frames = ps.normal
		}
		if idx >= len(frames) || rem <= 0 {
			continue
		}
		f := frames[idx]
		low := k.t
		if f.Arrival > low {
			low = f.Arrival
		}
		var sizes []int
		minCap := 1 << 30
		for _, si := range set {
			if c := ps.table(si).MaxPayload(class, ps.overhead()); c < minCap {
				minCap = c
			}
		}
		if rem <= minCap {
			sizes = append(sizes, rem)
		}
		upper := rem - 1
		if minCap < upper {
			upper = minCap
		}
		for x := ps.minNonLast(); x <= upper; x++ {
			sizes = append(sizes, x)
		}
		for _, x := range sizes {
			before := 0
			hasBefore := false
			if pi < len(ps.probes) && ps.probes[pi].ReadAt > k.t {
				before = ps.probes[pi].ReadAt
				hasBefore = true
			}
			start, ok := ps.commonEarliestFit(set, class, low, x, before, hasBefore)
			if !ok {
				continue
			}
			end := start + ps.overhead() + x
			final := x == rem
			edge := Fragment{FrameID: f.ID, Class: class, Start: start, Overhead: ps.overhead(), Data: x, End: end, Bytes: ps.overhead() + x, Final: final}
			nk := k
			nk.t = end
			if class == Fast {
				if final {
					nk.fi++
					if nk.fi < len(ps.fast) {
						nk.frem = ps.fast[nk.fi].Length
					} else {
						nk.frem = 0
					}
				} else {
					nk.frem -= x
				}
			} else {
				if final {
					nk.ni++
					if nk.ni < len(ps.normal) {
						nk.nrem = ps.normal[nk.ni].Length
					} else {
						nk.nrem = 0
					}
				} else {
					nk.nrem -= x
				}
			}
			child := ps.solve(nk, set, pi)
			if !child.feasible {
				continue
			}
			out := map[string]Outcome{}
			allMet := true
			for _, si := range set {
				name := ps.scenarios[si].Name
				o := child.outcomes[name]
				if final && end <= f.Deadline {
					if class == Fast {
						o.FastOnTime++
					} else {
						o.NormalOnTime++
					}
				}
				o.Overhead += ps.overhead()
				if final {
					o.FinishSum += end
				}
				if final && ps.commit[f.ID] && end > f.Deadline {
					o.CommittedMet = false
				}
				if !o.CommittedMet {
					allMet = false
				}
				out[name] = o
			}
			if !allMet {
				continue
			}
			node := &PolicyNode{Kind: "action", At: k.t, Candidates: ps.names(set), Action: &edge, Next: child.node, Outcomes: out}
			candidates = append(candidates, &robustResult{node: node, outcomes: out, feasible: true})
		}
	}

	if len(candidates) == 0 {
		r := &robustResult{
			node:     &PolicyNode{Kind: "infeasible", At: k.t, Candidates: ps.names(set), Reason: "no shared action preserves commitments", Outcomes: map[string]Outcome{}},
			feasible: false,
		}
		ps.memo[mk] = r
		return r
	}
	sort.SliceStable(candidates, func(i, j int) bool {
		return robustBetter(candidates[i].outcomes, candidates[j].outcomes, set)
	})
	best := candidates[0]
	ps.memo[mk] = best
	return best
}

func (ps *policySolver) overhead() int {
	// BuildPolicy validates equal config through one stored field indirectly.
	return ps.overheadBytes
}

func (ps *policySolver) minNonLast() int { return ps.minFragment }

type policySolverFields struct {
	overheadBytes int
	minFragment   int
}

func (ps *policySolver) solveObservation(k stateKey, set []int, pi int) *robustResult {
	pr := ps.probes[pi]
	groups := map[int][]int{}
	var stamps []int
	for _, si := range set {
		stamp := pr.Stamps[ps.scenarios[si].Name]
		if _, ok := groups[stamp]; !ok {
			stamps = append(stamps, stamp)
		}
		groups[stamp] = append(groups[stamp], si)
	}
	sort.Ints(stamps)

	node := &PolicyNode{
		Kind:       "observe",
		At:         k.t,
		Candidates: ps.names(set),
		Observation: &ObservationEvent{
			ProbeIndex: pi,
			LatchedAt:  pr.ReadAt,
			BranchAt:   k.t,
		},
	}
	out := map[string]Outcome{}
	feasible := true
	for _, stamp := range stamps {
		g := groups[stamp]
		child := ps.solve(k, g, pi+1)
		branch := PolicyBranch{Stamp: stamp, Candidates: ps.names(g), Next: child.node}
		node.Branches = append(node.Branches, branch)
		if !child.feasible {
			feasible = false
		}
		for name, o := range child.outcomes {
			out[name] = o
		}
	}
	node.Outcomes = out
	r := &robustResult{node: node, outcomes: out, feasible: feasible}
	return r
}

func (ps *policySolver) solveSingleton(k stateKey, si int) *robustResult {
	sc := ps.scenarios[si]
	s := &solver{
		cfg:       Config{Table: sc.Table, Frames: ps.framesAsConfig(), MinNonLast: ps.minFragment, Overhead: ps.overheadBytes},
		fast:      ps.fast,
		normal:    ps.normal,
		memo:      map[stateKey]*dpResult{},
		nodeLimit: 200000,
		online:    true,
		commit:    ps.commit,
	}
	dr, err := s.best(k)
	if err != nil || dr == nil {
		node := &PolicyNode{Kind: "infeasible", At: k.t, Candidates: []string{sc.Name}, Reason: fmt.Sprint(err)}
		return &robustResult{node: node, feasible: false, outcomes: map[string]Outcome{}}
	}
	suffix := s.buildPlan(dr.steps)
	o := Outcome{
		FastOnTime:   dr.fastOT,
		NormalOnTime: dr.normalOT,
		Overhead:     dr.overhead,
		FinishSum:    dr.finishSum,
		CommittedMet: dr.committedMet,
	}
	out := map[string]Outcome{sc.Name: o}
	node := &PolicyNode{Kind: "leaf", At: k.t, Candidates: []string{sc.Name}, Suffix: suffix, Outcomes: out}
	return &robustResult{node: node, outcomes: out, feasible: dr.committedMet}
}

func (ps *policySolver) framesAsConfig() []Frame { return ps.frames }

func (ps *policySolver) names(set []int) []string {
	out := make([]string, 0, len(set))
	for _, i := range set {
		out = append(out, ps.scenarios[i].Name)
	}
	sort.Strings(out)
	return out
}

func (ps *policySolver) commonEarliestFit(set []int, class Class, low, x, before int, hasBefore bool) (int, bool) {
	s := low
	for iter := 0; iter < 100000; iter++ {
		next := s
		for _, si := range set {
			t := ps.table(si)
			if t.FitsAt(s, x, ps.overheadBytes, class) {
				continue
			}
			n, ok := t.EarliestFit(s, x, ps.overheadBytes, class)
			if !ok {
				return 0, false
			}
			if n > next {
				next = n
			}
		}
		if next == s {
			if hasBefore && s >= before {
				return 0, false
			}
			return s, true
		}
		if next < s {
			return 0, false
		}
		s = next
		if hasBefore && s >= before {
			return 0, false
		}
	}
	return 0, false
}

func robustBetter(a, b map[string]Outcome, set []int) bool {
	wa, wb := worstNormal(a, set), worstNormal(b, set)
	if wa != wb {
		return wa > wb
	}
	wfa, wfb := worstFast(a, set), worstFast(b, set)
	if wfa != wfb {
		return wfa > wfb
	}
	ohA, ohB := worstOverhead(a, set), worstOverhead(b, set)
	if ohA != ohB {
		return ohA < ohB
	}
	fsA, fsB := worstFinish(a, set), worstFinish(b, set)
	return fsA < fsB
}

func worstNormal(m map[string]Outcome, set []int) int {
	w := 1 << 30
	for name, o := range m {
		_ = name
		if o.NormalOnTime < w {
			w = o.NormalOnTime
		}
	}
	return w
}
func worstFast(m map[string]Outcome, set []int) int {
	w := 1 << 30
	for _, o := range m {
		if o.FastOnTime < w {
			w = o.FastOnTime
		}
	}
	return w
}
func worstOverhead(m map[string]Outcome, set []int) int {
	w := 0
	for _, o := range m {
		if o.Overhead > w {
			w = o.Overhead
		}
	}
	return w
}
func worstFinish(m map[string]Outcome, set []int) int {
	w := 0
	for _, o := range m {
		if o.FinishSum > w {
			w = o.FinishSum
		}
	}
	return w
}

func (ps *policySolver) assignIDs(n *PolicyNode) {
	if n == nil {
		return
	}
	ps.nodeSeq++
	n.ID = fmt.Sprintf("n%d", ps.nodeSeq)
	ps.assignIDs(n.Next)
	for i := range n.Branches {
		ps.assignIDs(n.Branches[i].Next)
	}
}

// ScenarioPlan replays the timestamp for name and returns the complete,
// concrete plan selected by this policy.
func (p *Policy) ScenarioPlan(name string) (*Plan, error) {
	var sc Scenario
	found := false
	for _, s := range p.Scenarios {
		if s.Name == name {
			sc, found = s, true
			break
		}
	}
	if !found {
		return nil, fmt.Errorf("scenario %q not in policy", name)
	}
	var prefix []Fragment
	node := p.Root
	pi := 0
	for node != nil {
		switch node.Kind {
		case "action":
			if node.Action != nil {
				prefix = append(prefix, *node.Action)
			}
			node = node.Next
		case "wait":
			node = node.Next
		case "observe":
			if node.Observation == nil {
				return nil, fmt.Errorf("observation node lacks event")
			}
			pi = node.Observation.ProbeIndex
			if pi >= len(p.Probes) {
				return nil, fmt.Errorf("missing probe %d", pi)
			}
			stamp := p.Probes[pi].Stamps[name]
			var next *PolicyNode
			for _, b := range node.Branches {
				if b.Stamp == stamp {
					next = b.Next
					break
				}
			}
			if next == nil {
				return nil, fmt.Errorf("stamp %d for scenario %q has no branch", stamp, name)
			}
			node = next
		case "leaf":
			if node.Suffix != nil {
				prefix = append(prefix, node.Suffix.Fragments...)
			}
			node = nil
		case "terminal":
			node = nil
		default:
			return nil, fmt.Errorf("node %s has unsupported kind %q", node.ID, node.Kind)
		}
	}
	cfg := Config{Table: sc.Table, Frames: p.frames, MinNonLast: p.minNonLast, Overhead: p.overhead}
	ev, err := CheckFragments(cfg, prefix)
	if err != nil {
		return nil, err
	}
	if !ev.Valid() {
		return ev.Plan, fmt.Errorf("policy trace for %q is invalid: %+v", name, ev.Violations)
	}
	return ev.Plan, nil
}
