package admit

import (
	"fmt"
	"reflect"
	"sort"
)

// VirtualClock is a manually advanced integer clock. It never sleeps or uses
// wall time, so interleaving tests are deterministic.
type VirtualClock struct {
	now int
}

func NewVirtualClock(start int) *VirtualClock {
	if start < 0 {
		start = 0
	}
	return &VirtualClock{now: start}
}

func (c *VirtualClock) Now() int { return c.now }

func (c *VirtualClock) AdvanceTo(t int) error {
	if t < c.now {
		return fmt.Errorf("virtual clock cannot move backwards: now=%d target=%d", c.now, t)
	}
	c.now = t
	return nil
}

func (c *VirtualClock) Advance(d int) error {
	if d < 0 {
		return fmt.Errorf("virtual clock advance must be non-negative")
	}
	c.now += d
	return nil
}

// EventType identifies a deterministic simulation event.
type EventType string

const (
	EventBatchStarted         EventType = "batch-started"
	EventBatchCompleted       EventType = "batch-completed"
	EventRequestFinished      EventType = "request-finished"
	EventCancellationRevealed EventType = "cancellation-revealed"
	EventObservation          EventType = "observation"
)

// Event is one entry in the virtual execution log.
type Event struct {
	Time        int          `json:"time"`
	Type        EventType    `json:"type"`
	Channel     int          `json:"channel,omitempty"`
	Batch       *BatchAction `json:"batch,omitempty"`
	RequestID   string       `json:"request_id,omitempty"`
	Observation *Observation `json:"observation,omitempty"`
	Message     string       `json:"message,omitempty"`
}

// RunReport is the result of executing a plan for one named scenario.
type RunReport struct {
	Scenario              string         `json:"scenario"`
	Events                []Event        `json:"events"`
	CompletionAt          map[string]int `json:"completion_at"`
	RevealedCancellations []string       `json:"revealed_cancellations,omitempty"`
	Margin                int            `json:"margin"`
	DeadlineMet           bool           `json:"deadline_met"`
}

// VirtualDriver executes a Plan against one concrete scenario while exposing
// cancellations only at their reveal boundaries.
type VirtualDriver struct {
	clock    *VirtualClock
	problem  Problem
	plan     *Plan
	scenario Scenario
	name     string
	node     *PlanNode

	running  map[int]*BatchAction
	finished map[string]int
	canceled map[string]bool
	revealed []string
	events   []Event
	done     bool
}

// NewVirtualDriver creates a driver for scenarioName. An empty name selects
// the first public/default scenario.
func NewVirtualDriver(p Problem, plan *Plan, scenarioName string) (*VirtualDriver, error) {
	if plan == nil || plan.Root == nil {
		return nil, fmt.Errorf("nil plan")
	}
	scenarios := normalizeScenarios(p)
	var sc Scenario
	found := scenarioName == ""
	for _, s := range scenarios {
		if s.Name == scenarioName {
			sc = s
			found = true
			break
		}
	}
	if !found {
		return nil, fmt.Errorf("scenario %q not found", scenarioName)
	}
	if sc.Continue == nil {
		sc.Continue = map[string]bool{}
	}
	return &VirtualDriver{
		clock:    NewVirtualClock(0),
		problem:  p,
		plan:     plan,
		scenario: sc,
		name:     sc.Name,
		node:     plan.Root,
		running:  map[int]*BatchAction{},
		finished: map[string]int{},
		canceled: map[string]bool{},
	}, nil
}

func normalizeScenarios(p Problem) []Scenario {
	if len(p.Scenarios) > 0 {
		out := make([]Scenario, len(p.Scenarios))
		copy(out, p.Scenarios)
		for i := range out {
			if out[i].Name == "" {
				out[i].Name = fmt.Sprintf("scenario-%d", i)
			}
		}
		return out
	}
	all := map[string]bool{}
	for _, r := range p.Requests {
		all[r.ID] = true
	}
	return []Scenario{{Name: "default-all-continue", Continue: all}}
}

func (d *VirtualDriver) Clock() *VirtualClock { return d.clock }
func (d *VirtualDriver) Now() int             { return d.clock.Now() }

// RevealedCancellations returns only cancellation IDs revealed by the current
// virtual time. Future events in Problem are deliberately not returned.
func (d *VirtualDriver) RevealedCancellations() []string {
	return append([]string(nil), d.revealed...)
}

func (d *VirtualDriver) Events() []Event { return append([]Event(nil), d.events...) }

// Step performs one plan action or one boundary observation. It returns false
// when execution has reached its leaf.
func (d *VirtualDriver) Step() (bool, error) {
	if d.done {
		return false, nil
	}
	n := d.node
	if n == nil {
		return false, fmt.Errorf("plan ended without leaf")
	}
	if n.Leaf != nil {
		if err := d.checkLeaf(n.Leaf); err != nil {
			return false, err
		}
		d.done = true
		return false, nil
	}

	if n.Start != nil {
		if n.At != d.clock.Now() {
			return false, fmt.Errorf("start node at t=%d but virtual clock is t=%d", n.At, d.clock.Now())
		}
		a := n.Start
		if d.running[a.Channel] != nil {
			return false, fmt.Errorf("channel %d already running a batch", a.Channel)
		}
		reqByID := map[string]Request{}
		for _, r := range d.problem.Requests {
			reqByID[r.ID] = r
		}
		for _, id := range a.RequestIDs {
			r, ok := reqByID[id]
			if !ok {
				return false, fmt.Errorf("plan references unknown request %q", id)
			}
			if d.clock.Now() < r.Release {
				return false, fmt.Errorf("request %q started before release %d", id, r.Release)
			}
			if d.canceled[id] {
				return false, fmt.Errorf("canceled request %q was started", id)
			}
		}
		wantDur, ok := lookupDuration(d.problem.Durations, a.Stage, a.Shape, len(a.RequestIDs))
		if !ok {
			return false, fmt.Errorf("missing duration for executed batch %+v", a)
		}
		if wantDur != a.Duration || a.Finish != a.Start+a.Duration {
			return false, fmt.Errorf("batch duration/finish mismatch for %v", a.RequestIDs)
		}
		cp := *a
		cp.RequestIDs = append([]string(nil), a.RequestIDs...)
		d.running[a.Channel] = &cp
		d.events = append(d.events, Event{
			Time: d.clock.Now(), Type: EventBatchStarted, Channel: a.Channel, Batch: a,
			Message: fmt.Sprintf("start %s batch %v", a.Stage, a.RequestIDs),
		})
		d.node = n.Then
		return true, nil
	}

	if len(n.Branches) == 0 {
		return false, fmt.Errorf("plan node at %d has neither action, branches, nor leaf", n.At)
	}
	t := n.Branches[0].Observation.Time
	for _, b := range n.Branches {
		if b.Observation.Time != t {
			return false, fmt.Errorf("branch times differ at node %d", n.At)
		}
	}
	if err := d.clock.AdvanceTo(t); err != nil {
		return false, err
	}
	obs, err := d.processBoundary(t)
	if err != nil {
		return false, err
	}
	var next *PlanNode
	matches := 0
	for i := range n.Branches {
		if observationsEqual(n.Branches[i].Observation, obs) {
			next = n.Branches[i].Next
			matches++
		}
	}
	if matches > 1 {
		return false, fmt.Errorf("observation %+v matches %d plan branches", obs, matches)
	}
	if next == nil {
		return false, fmt.Errorf("no plan branch matches observation %+v at t=%d", obs, t)
	}
	d.events = append(d.events, Event{Time: t, Type: EventObservation, Observation: &obs})
	d.node = next
	return true, nil
}

func (d *VirtualDriver) processBoundary(t int) (Observation, error) {
	obs := Observation{Time: t}
	var rawContinue []string
	var rawEarly []string

	for ch := 0; ch < 2; ch++ {
		a := d.running[ch]
		if a == nil || a.Finish != t {
			if a != nil && a.Finish < t {
				return obs, fmt.Errorf("channel %d batch finish %d was skipped", ch, a.Finish)
			}
			continue
		}
		d.events = append(d.events, Event{
			Time: t, Type: EventBatchCompleted, Channel: ch, Batch: a,
			Message: fmt.Sprintf("complete %s batch %v", a.Stage, a.RequestIDs),
		})
		for _, id := range a.RequestIDs {
			if d.canceled[id] {
				continue
			}
			if a.Stage == Stage2 {
				d.finished[id] = t
				d.events = append(d.events, Event{Time: t, Type: EventRequestFinished, Channel: ch, RequestID: id})
				continue
			}
			// Stage one completion.
			obs.Stage1Completed = append(obs.Stage1Completed, id)
			if d.scenario.Continue[id] {
				rawContinue = append(rawContinue, id)
			} else {
				rawEarly = append(rawEarly, id)
				d.finished[id] = t
				d.events = append(d.events, Event{Time: t, Type: EventRequestFinished, Channel: ch, RequestID: id})
			}
		}
		delete(d.running, ch)
	}

	var newCancellations []string
	for _, c := range d.scenario.Cancellations {
		if c.RevealAt != t || d.canceled[c.RequestID] {
			continue
		}
		d.canceled[c.RequestID] = true
		d.revealed = append(d.revealed, c.RequestID)
		newCancellations = append(newCancellations, c.RequestID)
		d.events = append(d.events, Event{
			Time: t, Type: EventCancellationRevealed, RequestID: c.RequestID,
			Message: fmt.Sprintf("cancellation for %q revealed", c.RequestID),
		})
	}
	obs.RevealedCancellations = append(obs.RevealedCancellations, newCancellations...)
	newCancelSet := map[string]bool{}
	for _, id := range newCancellations {
		newCancelSet[id] = true
	}
	for _, id := range rawContinue {
		if !newCancelSet[id] {
			obs.ContinueToStage2 = append(obs.ContinueToStage2, id)
		}
	}
	for _, id := range rawEarly {
		if !newCancelSet[id] {
			obs.FinishAfterStage1 = append(obs.FinishAfterStage1, id)
		}
	}
	sortStringsInObservation(&obs)
	return obs, nil
}

func sortStringsInObservation(o *Observation) {
	sort.Strings(o.Stage1Completed)
	sort.Strings(o.ContinueToStage2)
	sort.Strings(o.FinishAfterStage1)
	sort.Strings(o.RevealedCancellations)
}

func observationsEqual(a, b Observation) bool {
	sortStringsInObservation(&a)
	sortStringsInObservation(&b)
	return reflect.DeepEqual(a, b)
}

func (d *VirtualDriver) checkLeaf(leaf *Leaf) error {
	if len(d.running) != 0 {
		return fmt.Errorf("plan reached leaf with running batches: %d", len(d.running))
	}
	for id, got := range d.finished {
		if want, ok := leaf.CompletionAt[id]; ok && want != got {
			return fmt.Errorf("completion time mismatch for %q: plan=%d actual=%d", id, want, got)
		}
	}
	return nil
}

// Run executes the complete plan and returns a deterministic report.
func (d *VirtualDriver) Run() (*RunReport, error) {
	for {
		ok, err := d.Step()
		if err != nil {
			return nil, err
		}
		if !ok {
			break
		}
	}
	margin := vacuousMargin
	deadlineMet := true
	reqByID := map[string]Request{}
	for _, r := range d.problem.Requests {
		reqByID[r.ID] = r
	}
	for id, at := range d.finished {
		r := reqByID[id]
		m := r.Deadline - at
		if m < margin {
			margin = m
		}
		if m < 0 {
			deadlineMet = false
		}
	}
	vacuous := margin >= vacuousMargin/2
	if vacuous {
		margin = 0
	}
	return &RunReport{
		Scenario:              d.name,
		Events:                append([]Event(nil), d.events...),
		CompletionAt:          copyIntMap(d.finished),
		RevealedCancellations: append([]string(nil), d.revealed...),
		Margin:                margin,
		DeadlineMet:           deadlineMet,
	}, nil
}

func copyIntMap(m map[string]int) map[string]int {
	out := make(map[string]int, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// ValidatePlan executes the contingent plan for every public scenario with
// the virtual clock and returns its margin report.
func ValidatePlan(p Problem, plan *Plan) (*MarginReport, error) {
	return planMargins(p, plan)
}

func planMargins(p Problem, plan *Plan) (*MarginReport, error) {
	scenarios := normalizeScenarios(p)
	rep := &MarginReport{ByScenario: map[string]int{}, ByRequest: map[string]int{}}
	anyCompletion := false
	worst := vacuousMargin
	for _, sc := range scenarios {
		d, err := NewVirtualDriver(p, plan, sc.Name)
		if err != nil {
			return nil, err
		}
		run, err := d.Run()
		if err != nil {
			return nil, fmt.Errorf("execute plan for scenario %q: %w", sc.Name, err)
		}
		if !run.DeadlineMet {
			return nil, fmt.Errorf("executed plan misses a deadline in scenario %q", sc.Name)
		}
		rep.ByScenario[sc.Name] = run.Margin
		if run.Margin < worst {
			worst = run.Margin
		}
		for id, at := range run.CompletionAt {
			anyCompletion = true
			r, ok := requestByID(p, id)
			if !ok {
				return nil, fmt.Errorf("execution completed unknown request %q", id)
			}
			m := r.Deadline - at
			if old, ok := rep.ByRequest[id]; !ok || m < old {
				rep.ByRequest[id] = m
			}
		}
	}
	if !anyCompletion {
		rep.Vacuous = true
		rep.Worst = 0
		return rep, nil
	}
	rep.Worst = worst
	return rep, nil
}

func requestByID(p Problem, id string) (Request, bool) {
	for _, r := range p.Requests {
		if r.ID == id {
			return r, true
		}
	}
	return Request{}, false
}
