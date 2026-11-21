package twopc

import "sync"

// TxState is the recovery-visible state of one transaction on a participant.
type TxState string

const (
	TxUnknown   TxState = "unknown"
	TxPrepared  TxState = "prepared"  // change persisted, still pending a decision
	TxCommitted TxState = "committed" // change applied
	TxAborted   TxState = "aborted"   // change discarded
)

// Participant is one shard holding an account balance. A transaction may
// add or subtract an integer delta. All state transitions are persisted to
// an append-only log before they are acknowledged, so the participant can
// be stopped after any single record append and reopened from its log.
//
// While a transaction is prepared but undecided, reads still see the old
// (committed) balance; the pending change only becomes visible at commit.
type Participant struct {
	mu      sync.Mutex
	id      string
	log     *Log
	balance int            // committed balance
	pending map[string]int // txid -> delta, prepared but undecided
	decided map[string]TxState
}

// OpenParticipant opens the shard with the given id, recovering its state
// by replaying logPath. If the log is empty, an init record with
// startingBalance is persisted; otherwise startingBalance is ignored.
func OpenParticipant(id, logPath string, startingBalance int) (*Participant, error) {
	lg, recs, err := OpenLog(logPath)
	if err != nil {
		return nil, err
	}
	p := &Participant{
		id:      id,
		log:     lg,
		pending: make(map[string]int),
		decided: make(map[string]TxState),
	}
	hasInit := false
	for _, r := range recs {
		switch r.Type {
		case RecInit:
			p.balance = r.Balance
			hasInit = true
		case RecPrepare:
			p.pending[r.TxID] = r.Delta
		case RecCommit:
			if d, ok := p.pending[r.TxID]; ok {
				p.balance += d
				delete(p.pending, r.TxID)
			}
			p.decided[r.TxID] = TxCommitted
		case RecAbort:
			delete(p.pending, r.TxID)
			p.decided[r.TxID] = TxAborted
		}
	}
	if !hasInit {
		if err := lg.Append(Record{Type: RecInit, Balance: startingBalance}); err != nil {
			return nil, err
		}
		p.balance = startingBalance
	}
	return p, nil
}

// Prepare persists the pending change and votes yes. It votes no (without
// persisting anything) if the change would overdraw the account. Re-sent
// prepares are idempotent: an already-prepared transaction votes yes again
// without a second log append, and an already-decided transaction answers
// from its recorded decision.
func (p *Participant) Prepare(txID string, delta int) (bool, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	switch p.decided[txID] {
	case TxCommitted:
		return true, nil
	case TxAborted:
		return false, nil
	}
	if _, ok := p.pending[txID]; ok {
		return true, nil // already prepared, same vote
	}
	if p.balance+delta < 0 {
		return false, nil // veto: overdraft
	}
	if err := p.log.Append(Record{Type: RecPrepare, TxID: txID, Delta: delta}); err != nil {
		return false, err
	}
	p.pending[txID] = delta
	return true, nil
}

// Commit applies a previously prepared change exactly once and persists the
// commit before returning. Re-sent commits are no-ops.
func (p *Participant) Commit(txID string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.decided[txID] == TxCommitted {
		return nil
	}
	delta, ok := p.pending[txID]
	if !ok {
		return nil // nothing pending; treat as already handled
	}
	if err := p.log.Append(Record{Type: RecCommit, TxID: txID}); err != nil {
		return err
	}
	p.balance += delta
	delete(p.pending, txID)
	p.decided[txID] = TxCommitted
	return nil
}

// Abort discards a previously prepared change and persists the abort before
// returning. If the transaction was never prepared (for example because this
// shard vetoed it), the abort is still persisted so the decision remains
// visible after a restart. Re-sent aborts are no-ops.
func (p *Participant) Abort(txID string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.decided[txID] == TxAborted || p.decided[txID] == TxCommitted {
		return nil
	}
	if err := p.log.Append(Record{Type: RecAbort, TxID: txID}); err != nil {
		return err
	}
	delete(p.pending, txID)
	p.decided[txID] = TxAborted
	return nil
}

// Balance returns the committed balance. Pending (prepared but undecided)
// changes are not visible: during prepare, reads see the old value.
func (p *Participant) Balance() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.balance
}

// Pending reports the pending delta of a prepared, undecided transaction.
func (p *Participant) Pending(txID string) (int, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	d, ok := p.pending[txID]
	return d, ok
}

// State reports the recovery-visible state of a transaction.
func (p *Participant) State(txID string) TxState {
	p.mu.Lock()
	defer p.mu.Unlock()
	if s, ok := p.decided[txID]; ok {
		return s
	}
	if _, ok := p.pending[txID]; ok {
		return TxPrepared
	}
	return TxUnknown
}
