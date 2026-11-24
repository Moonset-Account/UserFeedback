package flowctl

import "fmt"

// RoundOrder determines whether egress service or ingress arrivals are
// applied first within a Round.
type RoundOrder int

const (
	// EgressFirst frees buffer with this round's egress budget before arrivals.
	// It is the zero value and default.
	EgressFirst RoundOrder = iota

	// ArrivalsFirst observes arrivals and pauses before applying egress. This
	// is useful when modeling same-round service of arriving frames.
	ArrivalsFirst
)

// Round is one synthetic simulation step.
//
// EgressAvailable is a total byte budget. When Departures is empty, the
// budget is allocated deterministically using Config.DrainOrder. When
// Departures is non-empty, those exact flow-attributed bytes are sent and
// EgressAvailable is not also allocated.
type Round struct {
	Arrivals        []Frame     `json:"arrivals"`
	EgressAvailable Bytes       `json:"egress_available"`
	Departures      []Departure `json:"departures,omitempty"`
	Order           RoundOrder  `json:"order"`
}

// RoundResult contains per-frame outcomes, egress allocation, new events, and
// the end-of-round snapshot.
type RoundResult struct {
	Round    int              `json:"round"`
	Arrivals []*ArrivalResult `json:"arrivals"`
	Egress   *DrainResult     `json:"egress,omitempty"`
	Events   []Event          `json:"events"`
	Snapshot Snapshot         `json:"snapshot"`
}

// Step validates the whole round before mutating state, then applies it in
// the configured order. Invalid input returns an error and leaves the
// controller unchanged.
func (c *Controller) Step(round Round) (*RoundResult, error) {
	if round.Order != EgressFirst && round.Order != ArrivalsFirst {
		return nil, fmt.Errorf("unknown round order %d", round.Order)
	}
	if round.EgressAvailable < 0 {
		return nil, fmt.Errorf("egress available must be non-negative, got %d", round.EgressAvailable)
	}
	for _, f := range round.Arrivals {
		if err := f.validate(); err != nil {
			return nil, err
		}
	}
	if len(round.Departures) > 0 {
		pending := make(map[Key]Bytes)
		for _, d := range round.Departures {
			if err := d.validate(); err != nil {
				return nil, err
			}
			pending[d.Key] += d.Bytes
		}
		for k, n := range pending {
			fs := c.existingState(k)
			used := Bytes(0)
			if fs != nil {
				used = fs.used
			}
			if n > used {
				return nil, fmt.Errorf("round departs %d bytes from %s but only %d are buffered", n, k, used)
			}
		}
	}

	c.round++
	roundNumber := c.round
	eventStart := len(c.events)
	out := &RoundResult{Round: roundNumber}

	runEgress := func() error {
		if len(round.Departures) > 0 {
			dr := &DrainResult{}
			for _, d := range round.Departures {
				if _, err := c.Depart(d); err != nil {
					return err
				}
				dr.Sent += d.Bytes
				dr.Departures = append(dr.Departures, d)
			}
			dr.Resumes = c.ReconcileResumes()
			out.Egress = dr
			return nil
		}
		if round.EgressAvailable > 0 {
			dr, err := c.Drain(round.EgressAvailable)
			if err != nil {
				return err
			}
			out.Egress = dr
		} else {
			// Reconcile even with zero budget so a caller can express a pure
			// control tick after changing state through lower-level methods.
			resumes := c.ReconcileResumes()
			if len(resumes) > 0 {
				out.Egress = &DrainResult{Resumes: resumes}
			}
		}
		return nil
	}

	runArrivals := func() error {
		for _, f := range round.Arrivals {
			ar, err := c.Arrive(f)
			if err != nil {
				return err
			}
			out.Arrivals = append(out.Arrivals, ar)
		}
		return nil
	}

	var err error
	if round.Order == ArrivalsFirst {
		err = runArrivals()
		if err == nil {
			err = runEgress()
		}
	} else {
		err = runEgress()
		if err == nil {
			err = runArrivals()
		}
	}
	if err != nil {
		// Validation above covers expected input errors; this is defensive.
		return nil, err
	}

	out.Events = c.Events()[eventStart:]
	out.Snapshot = c.Snapshot()
	return out, nil
}
