package twopc

import (
	"fmt"
	"sort"
	"sync"
)

// ParticipantRPC is how the coordinator talks to a participant. *Link
// implements it; tests may wrap it to drop or delay messages.
type ParticipantRPC interface {
	Prepare(txID string, delta int) (bool, error)
	Commit(txID string) error
	Abort(txID string) error
}

type txEntry struct {
	deltas   map[string]int
	decision string // "" until a decision record is persisted
	acked    map[string]bool
}

// Coordinator runs two-phase commit across shards. It persists a commit
// decision only after every participant has voted yes; any veto, or a
// restart that finds a begun transaction with no persisted decision, leads
// to a persisted abort decision. The decision record is always written
// (and fsynced) before the decision is sent to any participant.
type Coordinator struct {
	mu    sync.Mutex
	log   *Log
	parts map[string]ParticipantRPC
	txs   map[string]*txEntry
}

// OpenCoordinator opens the coordinator, recovering by replaying logPath.
// Any transaction that began but has no persisted decision is aborted: the
// abort decision is persisted during recovery.
func OpenCoordinator(logPath string, parts map[string]ParticipantRPC) (*Coordinator, error) {
	lg, recs, err := OpenLog(logPath)
	if err != nil {
		return nil, err
	}
	c := &Coordinator{log: lg, parts: parts, txs: make(map[string]*txEntry)}
	for _, r := range recs {
		switch r.Type {
		case RecBegin:
			c.txs[r.TxID] = &txEntry{deltas: r.Deltas, acked: make(map[string]bool)}
		case RecDecision:
			if tx, ok := c.txs[r.TxID]; ok {
				tx.decision = r.Decision
			}
		}
	}
	for id, tx := range c.txs {
		if tx.decision == "" {
			if err := lg.Append(Record{Type: RecDecision, TxID: id, Decision: DecisionAbort}); err != nil {
				return nil, err
			}
			tx.decision = DecisionAbort
		}
	}
	return c, nil
}

// Begin persists a begin record for a new transaction. deltas maps shard
// name to the integer change to apply on that shard.
func (c *Coordinator) Begin(txID string, deltas map[string]int) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.txs[txID]; ok {
		return fmt.Errorf("twopc: transaction %q already exists", txID)
	}
	cp := make(map[string]int, len(deltas))
	for k, v := range deltas {
		cp[k] = v
	}
	if err := c.log.Append(Record{Type: RecBegin, TxID: txID, Deltas: cp}); err != nil {
		return err
	}
	c.txs[txID] = &txEntry{deltas: cp, acked: make(map[string]bool)}
	return nil
}

// Decide runs the prepare round and persists the decision: commit only if
// every shard voted yes, abort on any veto or unreachable shard. It does
// not deliver the decision; call Deliver (possibly repeatedly) for that.
func (c *Coordinator) Decide(txID string) error {
	c.mu.Lock()
	tx, ok := c.txs[txID]
	already := ""
	if ok {
		already = tx.decision
	}
	c.mu.Unlock()
	if !ok {
		return fmt.Errorf("twopc: unknown transaction %q", txID)
	}
	if already != "" {
		return nil // decision already persisted (e.g. abort written at recovery)
	}

	names := make([]string, 0, len(tx.deltas))
	for name := range tx.deltas {
		names = append(names, name)
	}
	sort.Strings(names)

	allYes := true
	for _, name := range names {
		p, ok := c.parts[name]
		if !ok {
			allYes = false
			continue
		}
		yes, err := p.Prepare(txID, tx.deltas[name])
		if err != nil || !yes {
			allYes = false
		}
	}
	dec := DecisionAbort
	if allYes {
		dec = DecisionCommit
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.log.Append(Record{Type: RecDecision, TxID: txID, Decision: dec}); err != nil {
		return err
	}
	tx.decision = dec
	return nil
}

// Submit is the common case: Begin + Decide + one delivery attempt.
func (c *Coordinator) Submit(txID string, deltas map[string]int) error {
	if err := c.Begin(txID, deltas); err != nil {
		return err
	}
	if err := c.Decide(txID); err != nil {
		return err
	}
	c.Deliver(txID)
	return nil
}

// Deliver (re)sends the persisted decision to every shard that has not yet
// acknowledged it. Deliveries are idempotent, so it is safe to call any
// number of times. It returns true once every shard has acknowledged.
func (c *Coordinator) Deliver(txID string) bool {
	c.mu.Lock()
	tx, ok := c.txs[txID]
	if !ok || tx.decision == "" {
		c.mu.Unlock()
		return false
	}
	dec := tx.decision
	c.mu.Unlock()

	for name, p := range c.parts {
		c.mu.Lock()
		_, member := tx.deltas[name]
		acked := tx.acked[name]
		c.mu.Unlock()
		if !member || acked {
			continue
		}
		var err error
		if dec == DecisionCommit {
			err = p.Commit(txID)
		} else {
			err = p.Abort(txID)
		}
		if err != nil {
			continue // shard unreachable; retry on a later Deliver
		}
		c.mu.Lock()
		tx.acked[name] = true
		c.mu.Unlock()
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	for name := range tx.deltas {
		if !tx.acked[name] {
			return false
		}
	}
	return true
}

// DecisionOf reports the persisted decision for a transaction and whether
// any decision has been persisted. It remains queryable after restarts.
func (c *Coordinator) DecisionOf(txID string) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	tx, ok := c.txs[txID]
	if !ok || tx.decision == "" {
		return "", false
	}
	return tx.decision, true
}

// Done reports whether the decision has been persisted and acknowledged by
// every shard.
func (c *Coordinator) Done(txID string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	tx, ok := c.txs[txID]
	if !ok || tx.decision == "" {
		return false
	}
	for name := range tx.deltas {
		if !tx.acked[name] {
			return false
		}
	}
	return true
}
