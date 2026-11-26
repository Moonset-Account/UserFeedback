package sharedscan

import (
	"errors"
	"fmt"
)

// ErrFinished is returned when a caller tries to consume more rows after
// Finish has finalized the operator.
var ErrFinished = errors.New("sharedscan: operator is already finished")

// CorrelationFunc decides whether an inner row belongs to a request's
// correlated candidate set. SQL WHERE accepts only TruthTrue. A comparator
// can obtain a three-valued result with CompareRows and convert UNKNOWN to
// false here.
type CorrelationFunc func(request Request, row InnerRow) (bool, error)

// Option configures an Operator.
type Option func(*Operator)

// WithCorrelator installs the correlation predicate. Without one, every
// routed inner row is treated as a candidate.
func WithCorrelator(fn CorrelationFunc) Option {
	return func(o *Operator) {
		o.correlator = fn
	}
}

// Result is one finalized outer-row expression result.
type Result struct {
	// ID is the normalized request ID.
	ID string

	// Expr is the evaluated expression kind.
	Expr Expr

	// Value is the final three-valued result.
	Value Truth

	// Fingerprint is the semantic request identity.
	Fingerprint string

	// SawUnknownComparison reports an UNKNOWN row-value comparison.
	SawUnknownComparison bool

	// CandidateRows is the number of rows accepted by routing/correlation.
	CandidateRows int

	// Comparisons is the number of composite IN/NOT IN row comparisons.
	Comparisons int

	// CorrelationChecks is the number of correlation predicate calls.
	CorrelationChecks int

	// EarlyTerminated reports that one decisive match stopped further
	// evaluation for this request.
	EarlyTerminated bool

	// Witness is InnerRow.Payload from the decisive match, when available.
	Witness any
}

// Progress is a point-in-time snapshot of one request before Finish.
type Progress struct {
	Index              int
	ID                 string
	Expr               Expr
	Resolved           bool
	EarlyTerminated    bool
	Current            Truth
	CandidateRows      int
	Comparisons        int
	UnknownComparisons int
	CorrelationChecks  int
}

// Stats contains operator-wide scan and fan-out counters.
type Stats struct {
	Chunks            int
	RowsRead          int
	CorrelationChecks int
	CandidateRows     int
	Comparisons       int
	EarlyTerminated   int
	ScanStoppedEarly  bool
}

type tracker struct {
	request     Request
	fingerprint string

	// value is the running OR of candidate row comparisons for IN, or the
	// running EXISTS OR. NOT IN displays Not(value).
	value Truth

	result Truth

	early       bool
	finalized   bool
	candidate   int
	comparisons int
	unknown     int
	corrChecks  int
	witness     any
}

// Operator is an embeddable shared inner-scan operator. It receives inner
// chunks once and fans each row out to all still-active outer requests.
//
// Operator is designed for use by one scan goroutine; do not call Consume or
// Finish concurrently without external synchronization.
type Operator struct {
	requests   []Request
	trackers   []*tracker
	correlator CorrelationFunc

	chunks            int
	rowsRead          int
	correlationChecks int
	candidateRows     int
	comparisons       int
	earlyTerminated   int

	finished bool
	results  []Result
	err      error
}

// NewOperator validates requests and creates the shared-scan operator.
func NewOperator(requests []Request, options ...Option) (*Operator, error) {
	o := &Operator{}
	for _, opt := range options {
		opt(o)
	}

	ids := make(map[string]struct{}, len(requests))
	o.requests = make([]Request, len(requests))
	o.trackers = make([]*tracker, len(requests))

	for i, raw := range requests {
		req, err := raw.normalize(i)
		if err != nil {
			return nil, err
		}
		if _, exists := ids[req.ID]; exists {
			return nil, fmt.Errorf("sharedscan: duplicate request ID %q", req.ID)
		}
		ids[req.ID] = struct{}{}

		fp, err := req.Fingerprint()
		if err != nil {
			return nil, err
		}
		o.requests[i] = req
		o.trackers[i] = &tracker{
			request:     req,
			fingerprint: fp,
			value:       TruthFalse, // OR identity
		}
	}
	return o, nil
}

// Consume processes zero or more inner rows as one default chunk.
func (o *Operator) Consume(rows ...InnerRow) error {
	return o.ConsumeChunk(Chunk{Rows: rows})
}

// ConsumeChunk processes one incremental chunk. Chunk boundaries do not alter
// three-valued semantics.
func (o *Operator) ConsumeChunk(chunk Chunk) error {
	if o.err != nil {
		return o.err
	}
	if o.finished {
		return ErrFinished
	}

	o.chunks++
	for _, row := range chunk.Rows {
		o.rowsRead++

		for _, t := range o.trackers {
			if t.early {
				// This outer row already has decisive evidence. Skip all
				// later comparisons for it, but keep scanning for others.
				continue
			}
			if t.request.CandidateSet != "" && chunk.Set != t.request.CandidateSet {
				continue
			}

			candidate, err := o.correlate(t, row)
			if err != nil {
				o.err = err
				return o.err
			}
			if !candidate {
				continue
			}

			o.candidateRows++
			t.candidate++

			switch t.request.Expr {
			case ExprExists:
				t.early = true
				t.result = TruthTrue
				t.witness = row.Payload
				o.earlyTerminated++

			case ExprIn, ExprNotIn:
				cmp, err := CompareRows(t.request.Left, row.Values, t.request.Rule)
				if err != nil {
					o.err = fmt.Errorf("request %q: compare inner row: %w", t.request.ID, err)
					return o.err
				}
				o.comparisons++
				t.comparisons++
				if cmp == TruthUnknown {
					t.unknown++
				}
				t.value = Or(t.value, cmp)

				if cmp == TruthTrue {
					t.early = true
					t.witness = row.Payload
					o.earlyTerminated++
					if t.request.Expr == ExprIn {
						t.result = TruthTrue
					} else {
						t.result = TruthFalse
					}
				}
			}
		}
	}
	return nil
}

func (o *Operator) correlate(t *tracker, row InnerRow) (bool, error) {
	if o.correlator == nil {
		return true, nil
	}
	o.correlationChecks++
	t.corrChecks++
	ok, err := o.correlator(t.request, row)
	if err != nil {
		return false, fmt.Errorf("request %q: correlation: %w", t.request.ID, err)
	}
	return ok, nil
}

// PendingCount returns the number of requests without scan-time decisive
// evidence. A request with only UNKNOWN evidence remains pending because a
// future exact match can still determine the result.
func (o *Operator) PendingCount() int {
	n := 0
	for _, t := range o.trackers {
		if !t.early {
			n++
		}
	}
	return n
}

// AllDecisive reports whether every request has found scan-time decisive
// evidence and the shared physical scan may stop early.
func (o *Operator) AllDecisive() bool {
	return o.PendingCount() == 0
}

// Progress returns snapshots in request order.
func (o *Operator) Progress() []Progress {
	out := make([]Progress, len(o.trackers))
	for i, t := range o.trackers {
		current := t.value
		resolved := t.early || t.finalized
		switch t.request.Expr {
		case ExprNotIn:
			current = Not(t.value)
		case ExprExists:
			current = t.value
		}
		if resolved {
			current = t.result
		}
		out[i] = Progress{
			Index:              i,
			ID:                 t.request.ID,
			Expr:               t.request.Expr,
			Resolved:           resolved,
			EarlyTerminated:    t.early,
			Current:            current,
			CandidateRows:      t.candidate,
			Comparisons:        t.comparisons,
			UnknownComparisons: t.unknown,
			CorrelationChecks:  t.corrChecks,
		}
	}
	return out
}

// Finish finalizes all pending requests. For IN it returns the running OR;
// for NOT IN it negates that OR; EXISTS without a candidate is FALSE.
func (o *Operator) Finish() ([]Result, error) {
	if o.err != nil {
		return nil, o.err
	}
	if o.finished {
		return append([]Result(nil), o.results...), nil
	}

	for _, t := range o.trackers {
		if t.early {
			continue
		}

		switch t.request.Expr {
		case ExprIn:
			// No TRUE row. t.value is FALSE for an empty/all-FALSE set and
			// UNKNOWN if at least one row comparison was UNKNOWN.
			t.result = t.value
		case ExprNotIn:
			t.result = Not(t.value)
		case ExprExists:
			t.result = TruthFalse
		}
		t.finalized = true
	}

	o.finished = true
	o.results = make([]Result, len(o.trackers))
	for i, t := range o.trackers {
		o.results[i] = Result{
			ID:                   t.request.ID,
			Expr:                 t.request.Expr,
			Value:                t.result,
			Fingerprint:          t.fingerprint,
			SawUnknownComparison: t.unknown > 0,
			CandidateRows:        t.candidate,
			Comparisons:          t.comparisons,
			CorrelationChecks:    t.corrChecks,
			EarlyTerminated:      t.early,
			Witness:              t.witness,
		}
	}
	return append([]Result(nil), o.results...), nil
}

// Results returns results cached by Finish. Before Finish it returns nil.
func (o *Operator) Results() []Result {
	if !o.finished {
		return nil
	}
	return append([]Result(nil), o.results...)
}

// Stats returns a copy of operator-wide counters.
func (o *Operator) Stats() Stats {
	return Stats{
		Chunks:            o.chunks,
		RowsRead:          o.rowsRead,
		CorrelationChecks: o.correlationChecks,
		CandidateRows:     o.candidateRows,
		Comparisons:       o.comparisons,
		EarlyTerminated:   o.earlyTerminated,
	}
}

// MatchParameter is the standard equality correlation predicate. It compares
// Request.Param with InnerRow.Key under Request.Rule and accepts only TRUE.
// Under SQLRule, a NULL correlation field makes the predicate UNKNOWN and
// therefore rejects the row, matching SQL WHERE semantics.
func MatchParameter(request Request, row InnerRow) (bool, error) {
	value, err := CompareRows(request.Param, row.Key, request.Rule)
	if err != nil {
		return false, err
	}
	return value == TruthTrue, nil
}
