package reinject

import (
	"context"
	"sort"
	"sync"
)

// inflightResult is published to waiters while a repair is being applied.
type inflightResult struct {
	decision Decision
	err      error
}

// Engine evaluates repair requests against each consumer group's progress and
// performs the corresponding side effects. It keeps the decision taken for
// every business repair ID so retries reuse the same decision instead of
// re-injecting or re-routing anything.
type Engine struct {
	progress ProgressReader
	broker   Broker
	review   ReviewSource

	mu        sync.Mutex
	decisions map[string]Decision
	inflight  map[string]chan inflightResult
}

// NewEngine builds an Engine wired to its three side-effect ports.
func NewEngine(progress ProgressReader, broker Broker, review ReviewSource) (*Engine, error) {
	if progress == nil {
		return nil, ErrNilProgress
	}
	if broker == nil {
		return nil, ErrNilBroker
	}
	if review == nil {
		return nil, ErrNilReview
	}
	return &Engine{
		progress:  progress,
		broker:    broker,
		review:    review,
		decisions: make(map[string]Decision),
		inflight:  make(map[string]chan inflightResult),
	}, nil
}

// Redeliver applies repair to dead for every group in groups.
//
// The first call for a repair ID performs the side effects and records the
// resulting decision; subsequent calls with the same repair ID — including
// concurrent retries that arrive while the first call is still running —
// return the recorded decision unchanged and perform no side effect. If the
// in-flight call fails, its reservation is released so a later retry can
// re-attempt the repair.
func (e *Engine) Redeliver(ctx context.Context, dead DeadLetter, repair Repair, groups []Group) (Decision, error) {
	if err := ctx.Err(); err != nil {
		return Decision{}, err
	}
	if err := validate(dead, repair); err != nil {
		return Decision{}, err
	}

	for {
		if err := ctx.Err(); err != nil {
			return Decision{}, err
		}

		e.mu.Lock()
		if prior, ok := e.decisions[repair.RepairID]; ok {
			e.mu.Unlock()
			return cloneDecision(prior), nil
		}
		if wait, ok := e.inflight[repair.RepairID]; ok {
			e.mu.Unlock()
			select {
			case res, open := <-wait:
				if !open {
					continue // reservation vanished; try to acquire again
				}
				if res.err != nil {
					continue // owner failed; a retry may take over
				}
				return cloneDecision(res.decision), nil
			case <-ctx.Done():
				return Decision{}, ctx.Err()
			}
		}
		// Reserve the repair ID before touching any side effect so that
		// concurrent retries wait on this call instead of duplicating work.
		reserved := make(chan inflightResult, 1)
		e.inflight[repair.RepairID] = reserved
		e.mu.Unlock()

		decision, err := e.apply(ctx, dead, repair, groups)

		e.mu.Lock()
		delete(e.inflight, repair.RepairID)
		if err == nil {
			e.decisions[repair.RepairID] = decision
		}
		e.mu.Unlock()

		reserved <- inflightResult{decision: cloneDecision(decision), err: err}
		close(reserved)
		return cloneDecision(decision), err
	}
}

// apply evaluates every group and performs the side effects for one repair.
// It is called by exactly one Engine caller per repair ID at a time.
func (e *Engine) apply(ctx context.Context, dead DeadLetter, repair Repair, groups []Group) (Decision, error) {
	// Sort groups so the decision is stable regardless of call-site order.
	ordered := append([]Group(nil), groups...)
	sort.SliceStable(ordered, func(i, j int) bool { return ordered[i] < ordered[j] })

	actions := make([]GroupAction, 0, len(ordered))
	seen := make(map[Group]bool, len(ordered))
	for _, group := range ordered {
		if seen[group] {
			continue
		}
		seen[group] = true

		if err := ctx.Err(); err != nil {
			return Decision{}, err
		}

		prog, err := e.progress.Progress(ctx, group, dead.Key)
		if err != nil {
			return Decision{}, err
		}

		action := GroupAction{
			Group:      group,
			MessageID:  dead.MessageID,
			Key:        dead.Key,
			SeqInKey:   dead.SeqInKey,
			CustomerID: dead.CustomerID,
			Payload:    append([]byte(nil), repair.RepairedPayload...),
		}

		switch {
		case prog.HasIrrevocableSkip(dead.SeqInKey):
			// The group already decided to skip this position and moved on.
			// The repair cannot rewind acknowledged progress; it is sent to
			// the pending-review source instead.
			action.Action = RouteToReview
			if err := e.review.SendForReview(ctx, group, dead.MessageID, dead.Key, dead.SeqInKey, dead.CustomerID, repair.RepairedPayload); err != nil {
				return Decision{}, err
			}
		case prog.BeforePosition(dead.SeqInKey):
			// The group is still before the poisoned position (or has never
			// consumed the key): resume in place from the original position.
			action.Action = ResumeInPlace
			if err := e.broker.ReinstateAt(ctx, group, dead.MessageID, dead.Key, dead.SeqInKey, repair.RepairedPayload); err != nil {
				return Decision{}, err
			}
		default:
			// The group has committed past the position without recording an
			// explicit irrevocable skip. Progress must be retained, so the
			// repaired content goes to review rather than rewinding the
			// group. This is treated as an implicit irrevocable skip.
			action.Action = RouteToReview
			if err := e.review.SendForReview(ctx, group, dead.MessageID, dead.Key, dead.SeqInKey, dead.CustomerID, repair.RepairedPayload); err != nil {
				return Decision{}, err
			}
		}

		actions = append(actions, action)
	}

	return Decision{
		RepairID:   repair.RepairID,
		DeadLetter: cloneDeadLetter(dead),
		Actions:    actions,
	}, nil
}

// StoredDecision returns the recorded decision for repairID. It returns false
// when no Redeliver call has completed for that repair ID.
func (e *Engine) StoredDecision(repairID string) (Decision, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	d, ok := e.decisions[repairID]
	if !ok {
		return Decision{}, false
	}
	return cloneDecision(d), true
}

// RecordedRepairIDs reports the repair IDs that currently have a durable
// decision, in sorted order. It is primarily useful for tests and diagnostics.
func (e *Engine) RecordedRepairIDs() []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	ids := make([]string, 0, len(e.decisions))
	for id := range e.decisions {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

func validate(dead DeadLetter, repair Repair) error {
	if dead.MessageID == "" {
		return ErrEmptyDeadLetter
	}
	if repair.RepairID == "" {
		return ErrEmptyRepairID
	}
	if repair.MessageID != dead.MessageID {
		return ErrMessageMismatch
	}
	if repair.CustomerID != dead.CustomerID {
		return ErrCustomerMismatch
	}
	return nil
}

func cloneDeadLetter(d DeadLetter) DeadLetter {
	d.OriginalPayload = append([]byte(nil), d.OriginalPayload...)
	return d
}

func cloneDecision(d Decision) Decision {
	out := Decision{
		RepairID:   d.RepairID,
		DeadLetter: cloneDeadLetter(d.DeadLetter),
		Actions:    make([]GroupAction, len(d.Actions)),
	}
	for i, a := range d.Actions {
		a.Payload = append([]byte(nil), a.Payload...)
		out.Actions[i] = a
	}
	return out
}
