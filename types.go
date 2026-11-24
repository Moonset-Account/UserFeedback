// Package admit implements exact, small-scale real-time admission planning.
//
// The zero value is not usable; construct a Problem and call Admit or
// EvaluateSet. The solver is intentionally exhaustive for at most eight
// requests and uses only the Go standard library.
package admit

import (
	"fmt"
	"sort"
)

// Stage is a model execution stage. Only Stage1 and Stage2 are modeled.
type Stage int

const (
	Stage1 Stage = 1
	Stage2 Stage = 2
)

func (s Stage) String() string {
	switch s {
	case Stage1:
		return "stage1"
	case Stage2:
		return "stage2"
	default:
		return fmt.Sprintf("stage-%d", int(s))
	}
}

// Request is one inference request.
type Request struct {
	ID        string `json:"id"`
	Release   int    `json:"release"`
	Deadline  int    `json:"deadline"`
	Shape     string `json:"shape"`
	Profit    int    `json:"profit"`
	Committed bool   `json:"committed"`
}

// BatchCost identifies an integer batch duration. Size is the exact number of
// requests in the batch, not an upper bound.
type BatchCost struct {
	Stage Stage  `json:"stage"`
	Shape string `json:"shape"`
	Size  int    `json:"size"`
}

// Cancellation is a cancellation fact that is hidden until RevealAt.
// RevealAt is an inclusive virtual-time boundary.
type Cancellation struct {
	RequestID string `json:"request_id"`
	RevealAt  int    `json:"reveal_at"`
}

// Scenario is one allowed joint stage-one outcome and cancellation profile.
// Continue contains requests that reach stage two. A missing ID means that the
// request finishes after stage one in this scenario.
type Scenario struct {
	Name          string          `json:"name"`
	Continue      map[string]bool `json:"continue,omitempty"`
	Cancellations []Cancellation  `json:"cancellations,omitempty"`
}

// Problem is the complete offline input. Future cancellation fields are used by
// the solver only behind the information barrier described in the README.
type Problem struct {
	Requests  []Request         `json:"requests"`
	Durations map[BatchCost]int `json:"durations"`
	Scenarios []Scenario        `json:"scenarios,omitempty"`
}

// BatchAction is one executable non-preemptible batch.
type BatchAction struct {
	Channel    int      `json:"channel"`
	Stage      Stage    `json:"stage"`
	Shape      string   `json:"shape"`
	RequestIDs []string `json:"request_ids"`
	Start      int      `json:"start"`
	Duration   int      `json:"duration"`
	Finish     int      `json:"finish"`
}

// Observation is the information revealed at a virtual-time boundary.
type Observation struct {
	Time                  int      `json:"time"`
	Stage1Completed       []string `json:"stage1_completed,omitempty"`
	ContinueToStage2      []string `json:"continue_to_stage2,omitempty"`
	FinishAfterStage1     []string `json:"finish_after_stage1,omitempty"`
	RevealedCancellations []string `json:"revealed_cancellations,omitempty"`
}

// Leaf contains terminal completion information for one plan path.
type Leaf struct {
	CompletionAt map[string]int `json:"completion_at"`
	Margin       int            `json:"margin"`
}

// PlanBranch selects Next according to Observation.
type PlanBranch struct {
	Observation Observation `json:"observation"`
	Scenarios   []string    `json:"scenarios,omitempty"`
	Next        *PlanNode   `json:"next"`
}

// Plan is the public contingent schedule.
type Plan struct {
	Root        *PlanNode `json:"root"`
	Accepted    []string  `json:"accepted"`
	WorstMargin int       `json:"worst_margin"`
}

// PlanNode is a decision/observation node in a contingent schedule.
//
// If Start is non-nil, execute that batch and then follow Then at the same
// decision time. Otherwise Branches describe the next revealed boundary. A
// non-nil Leaf means all modeled work on that path is finished.
type PlanNode struct {
	At       int          `json:"at"`
	Start    *BatchAction `json:"start,omitempty"`
	Then     *PlanNode    `json:"then,omitempty"`
	Branches []PlanBranch `json:"branches,omitempty"`
	Leaf     *Leaf        `json:"leaf,omitempty"`
}

// MarginReport records deadline minus completion time.
type MarginReport struct {
	Worst      int            `json:"worst"`
	ByScenario map[string]int `json:"by_scenario,omitempty"`
	ByRequest  map[string]int `json:"by_request,omitempty"`
	Vacuous    bool           `json:"vacuous,omitempty"`
}

// DecisionProof explains why every legal decision at a rejected state fails.
type DecisionProof struct {
	Decision string       `json:"decision"`
	Action   *BatchAction `json:"action,omitempty"`
	Failure  *WitnessNode `json:"failure,omitempty"`
}

// WitnessNode is a structured adversarial branch/proof.
type WitnessNode struct {
	At             int             `json:"at"`
	Observation    *Observation    `json:"observation,omitempty"`
	Scenarios      []string        `json:"scenarios,omitempty"`
	Reason         string          `json:"reason"`
	RequestID      string          `json:"request_id,omitempty"`
	Deadline       int             `json:"deadline,omitempty"`
	Completion     int             `json:"completion,omitempty"`
	Child          *WitnessNode    `json:"child,omitempty"`
	DecisionProofs []DecisionProof `json:"decision_proofs,omitempty"`
}

// Witness is the branch basis for rejecting a fixed request set.
type Witness struct {
	Set     []string     `json:"set"`
	Summary string       `json:"summary"`
	Root    *WitnessNode `json:"root"`
}

// Verdict is the feasibility result for one fixed set.
type Verdict struct {
	Set           []string      `json:"set"`
	Feasible      bool          `json:"feasible"`
	OptimalMargin int           `json:"optimal_margin"`
	Margins       *MarginReport `json:"margins,omitempty"`
	Plan          *Plan         `json:"plan,omitempty"`
	Witness       *Witness      `json:"witness,omitempty"`
}

// Result is the optimal admission answer.
type Result struct {
	Feasible           bool          `json:"feasible"`
	Accepted           []string      `json:"accepted"`
	NewlyAccepted      []string      `json:"newly_accepted"`
	RejectedCandidates []string      `json:"rejected_candidates"`
	TotalProfit        int           `json:"total_profit"`
	OptimalMargin      int           `json:"optimal_margin"`
	Margins            *MarginReport `json:"margins,omitempty"`
	Plan               *Plan         `json:"plan,omitempty"`
	Witness            *Witness      `json:"witness,omitempty"`
}

// Options controls admission solving. The zero value uses safe defaults.
type Options struct {
	// AdmissionTime is the virtual time at which admission is decided.
	// Cancellations with RevealAt <= AdmissionTime are already visible.
	AdmissionTime int

	// DisableCancellationBarrier turns off the synthetic no-cancellation
	// epistemic shadow. It is intended only for tests that demonstrate the
	// false positives produced by oracle/look-ahead solving.
	DisableCancellationBarrier bool
}

func (o *Options) withDefaults() Options {
	if o == nil {
		return Options{}
	}
	return *o
}

// sortedIDs returns a deterministically sorted copy.
func sortedIDs(ids []string) []string {
	out := append([]string(nil), ids...)
	sort.Strings(out)
	return out
}

type obsKey struct {
	time             int
	completedMask    uint8
	continueMask     uint8
	cancellationMask uint8
}

func (k obsKey) String() string {
	return fmt.Sprintf("t=%d,done=%08b,continue=%08b,cancel=%08b", k.time, k.completedMask, k.continueMask, k.cancellationMask)
}
