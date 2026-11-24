// Package mergequeue decides which merge-queue candidates and test receipts
// still correspond to the current trunk and ordered queue.
//
// The adjudication is exact-commit based. In particular, a receipt is reusable
// for releasing the queue head only when its commit is byte-for-byte equal to
// the current head merge result. Similar SHAs, equivalent trees, stale
// candidates, and passing receipts from superseded builds never authorize a
// release.
package mergequeue

import (
	"errors"
	"fmt"
)

// ErrInvalidInput is wrapped by every error returned for malformed snapshots.
var ErrInvalidInput = errors.New("mergequeue: invalid input")

// SHA is an exact commit identifier. Values are compared as supplied; callers
// should normalize Git OIDs to one canonical form before adjudication.
type SHA string

// ChangeVersion identifies one queue entry and the exact version being tested.
type ChangeVersion struct {
	ID      string `json:"id"`
	Version SHA    `json:"version"`
}

func (c ChangeVersion) equal(other ChangeVersion) bool {
	return c.ID == other.ID && c.Version == other.Version
}

// TestState is the aggregated state of receipts for one exact commit.
type TestState string

const (
	// StateMissing means no receipt was supplied for the exact commit.
	StateMissing TestState = "missing"
	// StatePending means testing has been requested but has not finished.
	StatePending TestState = "pending"
	// StateRunning means testing is still running.
	StateRunning TestState = "running"
	// StatePassed means every known receipt for the commit passed.
	StatePassed TestState = "passed"
	// StateFailed means at least one receipt for the commit failed.
	StateFailed TestState = "failed"
)

// Receipt is one test result for an exact built commit.
type Receipt struct {
	Commit SHA       `json:"commit"`
	State  TestState `json:"state"`
}

// PassedReceipt is a small constructor for a passing receipt.
func PassedReceipt(commit SHA) Receipt { return Receipt{Commit: commit, State: StatePassed} }

// FailedReceipt is a small constructor for a failing receipt.
func FailedReceipt(commit SHA) Receipt { return Receipt{Commit: commit, State: StateFailed} }

// PrefixCommit records the resulting commit after merging the first Length
// entries of a candidate's registered prefix onto Candidate.Base.
//
// Length is one-based. Length == len(Candidate.Prefix) may be supplied and must
// equal Candidate.BuildCommit; shorter entries are the intermediate commits
// needed to survive an exact queue-head landing.
type PrefixCommit struct {
	Length int `json:"length"`
	Commit SHA `json:"commit"`
}

// Candidate is a queue prefix registered for speculative testing.
type Candidate struct {
	ID     string          `json:"id"`
	Base   SHA             `json:"base"`
	Prefix []ChangeVersion `json:"prefix"`
	Build  SHA             `json:"build"`
	Steps  []PrefixCommit  `json:"steps,omitempty"`
}

// Input is the complete snapshot needed for one adjudication.
type Input struct {
	// Trunk is the current trunk commit.
	Trunk SHA `json:"trunk"`
	// Queue is the current ordered queue.
	Queue []ChangeVersion `json:"queue"`
	// Candidates are all live candidate registrations, including stale ones.
	Candidates []Candidate `json:"candidates"`
	// Receipts are test results. Only receipts exactly matching a current
	// merge commit can authorize release.
	Receipts []Receipt `json:"receipts,omitempty"`
	// HeadCommit optionally supplies the externally computed current merge
	// result for Queue[0] onto Trunk. When empty, Adjudicate derives a unique
	// head commit from matching candidate steps.
	HeadCommit SHA `json:"head_commit,omitempty"`
}

// Action describes what a candidate owner should do after adjudication.
type Action string

const (
	// ActionRetired means the candidate's full prefix is already in trunk.
	ActionRetired Action = "retired"
	// ActionReuse means the candidate exactly matches current state and has
	// a passing receipt for its own Build commit.
	ActionReuse Action = "reuse"
	// ActionWait means the candidate matches but has no passing terminal
	// receipt yet.
	ActionWait Action = "wait"
	// ActionFailed means the candidate matches and has a failing receipt.
	ActionFailed Action = "failed"
	// ActionRebuild means the candidate no longer corresponds to current
	// state; Rebuilds contains the current prefix that must be reconstructed.
	ActionRebuild Action = "rebuild"
	// ActionCancel means the candidate's prefix no longer exists in the
	// queue and no same-length replacement is requested.
	ActionCancel Action = "cancel"
)

// Reason gives the primary explanation for a Decision or ReleaseDecision.
type Reason string

const (
	ReasonTestPassed      Reason = "test_passed"
	ReasonTestPending     Reason = "test_pending"
	ReasonTestRunning     Reason = "test_running"
	ReasonTestFailed      Reason = "test_failed"
	ReasonMissingReceipt  Reason = "missing_receipt"
	ReasonPrefixChanged   Reason = "prefix_changed"
	ReasonQueueShorter    Reason = "queue_shorter"
	ReasonTrunkMoved      Reason = "trunk_moved"
	ReasonLanded          Reason = "landed"
	ReasonNoHead          Reason = "no_head"
	ReasonNoCurrentCommit Reason = "no_current_commit"
	ReasonAmbiguousCommit Reason = "ambiguous_current_commit"
)

// TestSummary aggregates all receipts for one exact commit.
type TestSummary struct {
	State    TestState `json:"state"`
	Receipts []Receipt `json:"receipts,omitempty"`
}

// MatchedPrefix is a prefix step that still exactly corresponds to a prefix of
// the current queue on the current trunk.
type MatchedPrefix struct {
	// Length is the length in the current queue, after any landed registered
	// entries have been anchored to trunk.
	Length int         `json:"length"`
	Commit SHA         `json:"commit"`
	Test   TestSummary `json:"test"`
}

// Decision is the adjudication for one registered Candidate.
type Decision struct {
	CandidateID string `json:"candidate_id"`
	Action      Action `json:"action"`
	Reason      Reason `json:"reason"`

	// RegisteredLength is len(Candidate.Prefix).
	RegisteredLength int `json:"registered_length"`
	// AnchorLength is the number of initial registered entries whose result
	// commit equals the current trunk. Zero means Candidate.Base is trunk.
	AnchorLength int `json:"anchor_length"`
	// MatchedLength is the number of entries after AnchorLength that match
	// the front of the current queue.
	MatchedLength int `json:"matched_length"`
	// MismatchIndex is the zero-based index in Candidate.Prefix of the first
	// mismatch, or -1 when there is no mismatch.
	MismatchIndex int `json:"mismatch_index"`

	// BuildCommit is Candidate.Build, copied for convenient reporting.
	BuildCommit SHA `json:"build_commit"`
	// Test summarizes receipts for BuildCommit.
	Test TestSummary `json:"test"`

	// MatchedPrefixes contains still-valid exact prefix commits, including
	// prefixes before a replaced middle entry.
	MatchedPrefixes []MatchedPrefix `json:"matched_prefixes,omitempty"`
}

// RebuildRequest identifies a current queue prefix that needs a new build.
type RebuildRequest struct {
	OldCandidateID string          `json:"old_candidate_id"`
	Base           SHA             `json:"base"`
	Prefix         []ChangeVersion `json:"prefix"`
	Reason         Reason          `json:"reason"`
}

// ReleaseDecision says whether the queue head may land.
type ReleaseDecision struct {
	Allowed bool           `json:"allowed"`
	Head    *ChangeVersion `json:"head,omitempty"`
	Commit  SHA            `json:"commit,omitempty"`
	Reason  Reason         `json:"reason"`
	Test    TestSummary    `json:"test,omitempty"`
	// Sources are candidate IDs that recorded Commit as a matching head step.
	Sources []string `json:"sources,omitempty"`
}

// Report is the result of Adjudicate.
type Report struct {
	Trunk      SHA              `json:"trunk"`
	Head       *ChangeVersion   `json:"head,omitempty"`
	HeadCommit SHA              `json:"head_commit,omitempty"`
	Release    ReleaseDecision  `json:"release"`
	Decisions  []Decision       `json:"decisions"`
	Rebuilds   []RebuildRequest `json:"rebuilds,omitempty"`
}

// Judge is a readable alias for Adjudicate.
func Judge(in Input) (*Report, error) { return Adjudicate(in) }

// Adjudicate evaluates every candidate and the queue-head release decision.
func Adjudicate(in Input) (*Report, error) {
	if err := validateInput(in); err != nil {
		return nil, err
	}

	receiptsByCommit := make(map[SHA][]Receipt, len(in.Receipts))
	for _, r := range in.Receipts {
		receiptsByCommit[r.Commit] = append(receiptsByCommit[r.Commit], r)
	}
	summary := func(commit SHA) TestSummary {
		return summarizeReceipts(commit, receiptsByCommit[commit])
	}

	report := &Report{Trunk: in.Trunk}
	if len(in.Queue) > 0 {
		head := in.Queue[0]
		report.Head = &head
	}

	headSources := make(map[SHA][]string)
	headOrder := make([]SHA, 0)

	for _, candidate := range in.Candidates {
		decision, rebuild := judgeCandidate(in.Trunk, in.Queue, candidate, summary)
		report.Decisions = append(report.Decisions, decision)
		if rebuild != nil {
			report.Rebuilds = append(report.Rebuilds, *rebuild)
		}
		for _, mp := range decision.MatchedPrefixes {
			if mp.Length == 1 {
				if _, seen := headSources[mp.Commit]; !seen {
					headOrder = append(headOrder, mp.Commit)
				}
				headSources[mp.Commit] = append(headSources[mp.Commit], candidate.ID)
			}
		}
	}

	report.Release = decideRelease(in, headOrder, headSources, summary)
	if report.Release.Commit != "" && report.Release.Reason != ReasonAmbiguousCommit {
		report.HeadCommit = report.Release.Commit
	}
	return report, nil
}

// Candidate returns the decision for id.
func (r *Report) Candidate(id string) (Decision, bool) {
	if r == nil {
		return Decision{}, false
	}
	for _, d := range r.Decisions {
		if d.CandidateID == id {
			return d, true
		}
	}
	return Decision{}, false
}

// RebuildIDs returns OldCandidateID values from Report.Rebuilds in order.
func (r *Report) RebuildIDs() []string {
	if r == nil {
		return nil
	}
	ids := make([]string, 0, len(r.Rebuilds))
	for _, req := range r.Rebuilds {
		ids = append(ids, req.OldCandidateID)
	}
	return ids
}

// ReuseIDs returns IDs of candidates with ActionReuse.
func (r *Report) ReuseIDs() []string {
	if r == nil {
		return nil
	}
	ids := make([]string, 0)
	for _, d := range r.Decisions {
		if d.Action == ActionReuse {
			ids = append(ids, d.CandidateID)
		}
	}
	return ids
}

// CanReleaseHead reports whether the exact-current-commit receipt rule passed.
func (r *Report) CanReleaseHead() bool {
	return r != nil && r.Release.Allowed
}

func validateInput(in Input) error {
	if in.Trunk == "" {
		return fmt.Errorf("%w: trunk commit is required", ErrInvalidInput)
	}
	if err := validateChanges("queue", in.Queue); err != nil {
		return err
	}

	ids := make(map[string]struct{}, len(in.Candidates))
	for i, c := range in.Candidates {
		if c.ID == "" {
			return fmt.Errorf("%w: candidates[%d].id is required", ErrInvalidInput, i)
		}
		if _, exists := ids[c.ID]; exists {
			return fmt.Errorf("%w: duplicate candidate id %q", ErrInvalidInput, c.ID)
		}
		ids[c.ID] = struct{}{}
		if c.Base == "" {
			return fmt.Errorf("%w: candidate %q base is required", ErrInvalidInput, c.ID)
		}
		if c.Build == "" {
			return fmt.Errorf("%w: candidate %q build commit is required", ErrInvalidInput, c.ID)
		}
		if len(c.Prefix) == 0 {
			return fmt.Errorf("%w: candidate %q prefix must not be empty", ErrInvalidInput, c.ID)
		}
		if err := validateChanges(fmt.Sprintf("candidate %q prefix", c.ID), c.Prefix); err != nil {
			return err
		}
		if err := validateSteps(c); err != nil {
			return err
		}
	}

	for i, r := range in.Receipts {
		if r.Commit == "" {
			return fmt.Errorf("%w: receipts[%d].commit is required", ErrInvalidInput, i)
		}
		switch r.State {
		case StatePending, StateRunning, StatePassed, StateFailed:
		default:
			return fmt.Errorf("%w: receipts[%d].state %q is invalid", ErrInvalidInput, i, r.State)
		}
	}
	return nil
}

func validateChanges(field string, changes []ChangeVersion) error {
	for i, c := range changes {
		if c.ID == "" {
			return fmt.Errorf("%w: %s[%d].id is required", ErrInvalidInput, field, i)
		}
		if c.Version == "" {
			return fmt.Errorf("%w: %s[%d].version is required", ErrInvalidInput, field, i)
		}
	}
	return nil
}

func validateSteps(c Candidate) error {
	n := len(c.Prefix)
	lengths := make(map[int]struct{}, len(c.Steps)+1)
	lengths[n] = struct{}{}
	commits := map[SHA]int{c.Base: 0, c.Build: n}

	for i, step := range c.Steps {
		if step.Length < 1 || step.Length > n {
			return fmt.Errorf("%w: candidate %q steps[%d].length %d outside [1,%d]",
				ErrInvalidInput, c.ID, i, step.Length, n)
		}
		if step.Commit == "" {
			return fmt.Errorf("%w: candidate %q steps[%d].commit is required", ErrInvalidInput, c.ID, i)
		}
		if _, dup := lengths[step.Length]; dup {
			if step.Length == n && step.Commit == c.Build {
				continue
			}
			return fmt.Errorf("%w: candidate %q has duplicate step length %d", ErrInvalidInput, c.ID, step.Length)
		}
		lengths[step.Length] = struct{}{}
		if step.Length == n && step.Commit != c.Build {
			return fmt.Errorf("%w: candidate %q final step commit does not match build", ErrInvalidInput, c.ID)
		}
		if prevLength, seen := commits[step.Commit]; seen && prevLength != step.Length {
			return fmt.Errorf("%w: candidate %q step commit %q recorded at lengths %d and %d",
				ErrInvalidInput, c.ID, step.Commit, prevLength, step.Length)
		}
		commits[step.Commit] = step.Length
	}

	if c.Base == c.Build && n > 0 {
		return fmt.Errorf("%w: candidate %q base and build must differ for a non-empty prefix", ErrInvalidInput, c.ID)
	}
	return nil
}

func summarizeReceipts(commit SHA, receipts []Receipt) TestSummary {
	summary := TestSummary{State: StateMissing}
	if len(receipts) == 0 {
		return summary
	}
	summary.Receipts = append([]Receipt(nil), receipts...)

	var failed, running, pending, passed bool
	for _, r := range receipts {
		switch r.State {
		case StateFailed:
			failed = true
		case StateRunning:
			running = true
		case StatePending:
			pending = true
		case StatePassed:
			passed = true
		}
	}
	switch {
	case failed:
		summary.State = StateFailed
	case running:
		summary.State = StateRunning
	case pending:
		summary.State = StatePending
	case passed:
		summary.State = StatePassed
	default:
		summary.State = StateMissing
	}
	return summary
}

func inferQueueOffset(prefix []ChangeVersion, queue []ChangeVersion) int {
	n := len(prefix)

	// Prefer the smallest non-empty suffix that exactly equals a queue prefix.
	// This handles a head landing whose commit SHA differs from the recorded
	// speculative step: the first registered entry has left the queue.
	for offset := 0; offset < n; offset++ {
		remaining := prefix[offset:]
		if len(remaining) > len(queue) {
			continue
		}
		exact := true
		for i, change := range remaining {
			if !change.equal(queue[i]) {
				exact = false
				break
			}
		}
		if exact {
			return offset
		}
	}

	// No exact suffix alignment. Keep the full candidate when the current
	// queue is long enough; otherwise shift by the number needed to make the
	// remaining prefix fit the current queue.
	if n <= len(queue) {
		return 0
	}
	return n - len(queue)
}

func judgeCandidate(
	trunk SHA,
	queue []ChangeVersion,
	c Candidate,
	summary func(SHA) TestSummary,
) (Decision, *RebuildRequest) {
	n := len(c.Prefix)
	d := Decision{
		CandidateID:      c.ID,
		RegisteredLength: n,
		MismatchIndex:    -1,
		BuildCommit:      c.Build,
		Test:             summary(c.Build),
	}
	steps := make(map[int]SHA, n+1)
	steps[0] = c.Base
	steps[n] = c.Build
	for _, step := range c.Steps {
		steps[step.Length] = step.Commit
	}

	anchor := -1
	for length := 0; length <= n; length++ {
		commit, ok := steps[length]
		if ok && commit == trunk {
			anchor = length
			break
		}
	}

	if anchor < 0 {
		// Trunk is neither the registered base nor any exact recorded step.
		// A direct insertion or differently-hashed landing therefore breaks
		// receipt reuse even if change IDs/versions still align. Infer the
		// queue offset only to address the rebuild at the right prefix length.
		offset := inferQueueOffset(c.Prefix, queue)
		remaining := c.Prefix[offset:]
		want := len(remaining)
		matched := 0
		for matched < want && matched < len(queue) && remaining[matched].equal(queue[matched]) {
			matched++
		}
		d.MatchedLength = matched
		if matched < want {
			d.MismatchIndex = offset + matched
		}

		if want == 0 || want > len(queue) {
			d.Action = ActionCancel
			if want > len(queue) {
				d.Reason = ReasonQueueShorter
			} else {
				d.Reason = ReasonPrefixChanged
			}
			return d, nil
		}

		d.Action = ActionRebuild
		d.Reason = ReasonTrunkMoved
		return d, &RebuildRequest{
			OldCandidateID: c.ID,
			Base:           trunk,
			Prefix:         append([]ChangeVersion(nil), queue[:want]...),
			Reason:         ReasonTrunkMoved,
		}
	}

	d.AnchorLength = anchor
	remaining := c.Prefix[anchor:]
	want := len(remaining)

	matched := 0
	for matched < len(remaining) && matched < len(queue) && remaining[matched].equal(queue[matched]) {
		matched++
	}
	d.MatchedLength = matched

	// Preserve every recorded step that is still an exact prefix of the
	// current queue on the current trunk.
	for currentLength := 1; currentLength <= matched; currentLength++ {
		originalLength := anchor + currentLength
		if commit, ok := steps[originalLength]; ok {
			d.MatchedPrefixes = append(d.MatchedPrefixes, MatchedPrefix{
				Length: currentLength,
				Commit: commit,
				Test:   summary(commit),
			})
		}
	}

	if matched < len(remaining) && matched < len(queue) {
		d.MismatchIndex = anchor + matched
		if want > len(queue) {
			d.Action = ActionCancel
			d.Reason = ReasonPrefixChanged
			return d, nil
		}
		d.Action = ActionRebuild
		d.Reason = ReasonPrefixChanged
		return d, &RebuildRequest{
			OldCandidateID: c.ID,
			Base:           trunk,
			Prefix:         append([]ChangeVersion(nil), queue[:want]...),
			Reason:         ReasonPrefixChanged,
		}
	}

	if want > len(queue) {
		// The queue ended before the registered prefix. There is no candidate
		// of this exact length to rebuild.
		d.Action = ActionCancel
		d.Reason = ReasonQueueShorter
		return d, nil
	}

	if want == 0 {
		d.Action = ActionRetired
		d.Reason = ReasonLanded
		return d, nil
	}

	switch d.Test.State {
	case StatePassed:
		d.Action = ActionReuse
		d.Reason = ReasonTestPassed
	case StateFailed:
		d.Action = ActionFailed
		d.Reason = ReasonTestFailed
	case StateRunning:
		d.Action = ActionWait
		d.Reason = ReasonTestRunning
	case StatePending:
		d.Action = ActionWait
		d.Reason = ReasonTestPending
	default:
		d.Action = ActionWait
		d.Reason = ReasonMissingReceipt
	}
	return d, nil
}

func decideRelease(
	in Input,
	headOrder []SHA,
	headSources map[SHA][]string,
	summary func(SHA) TestSummary,
) ReleaseDecision {
	release := ReleaseDecision{}
	if len(in.Queue) == 0 {
		release.Reason = ReasonNoHead
		return release
	}
	head := in.Queue[0]
	release.Head = &head

	commit := in.HeadCommit
	if commit == "" {
		switch len(headOrder) {
		case 0:
			release.Reason = ReasonNoCurrentCommit
			return release
		case 1:
			commit = headOrder[0]
		default:
			release.Commit = ""
			release.Reason = ReasonAmbiguousCommit
			return release
		}
	}

	release.Commit = commit
	release.Sources = append([]string(nil), headSources[commit]...)
	release.Test = summary(commit)

	if in.HeadCommit == "" && len(headOrder) > 1 {
		// Defensive: normally handled above, but never choose when multiple
		// distinct derived head commits exist.
		release.Reason = ReasonAmbiguousCommit
		return release
	}

	switch release.Test.State {
	case StatePassed:
		release.Allowed = true
		release.Reason = ReasonTestPassed
	case StateFailed:
		release.Reason = ReasonTestFailed
	case StateRunning:
		release.Reason = ReasonTestRunning
	case StatePending:
		release.Reason = ReasonTestPending
	default:
		release.Reason = ReasonMissingReceipt
	}
	return release
}
