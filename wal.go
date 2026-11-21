package twopc

import (
	"bufio"
	"bytes"
	"encoding/json"
	"os"
	"sync"
)

// Record types persisted in the append-only logs.
const (
	RecInit     = "init"     // participant: initial balance
	RecPrepare  = "prepare"  // participant: prepared, change pending
	RecCommit   = "commit"   // participant: pending change applied
	RecAbort    = "abort"    // participant: pending change discarded
	RecBegin    = "begin"    // coordinator: transaction started
	RecDecision = "decision" // coordinator: commit/abort decision
)

// Decision values stored in RecDecision records.
const (
	DecisionCommit = "commit"
	DecisionAbort  = "abort"
)

// Record is a single persistent log entry. Fields are reused across record
// types to keep the on-disk format a simple JSON stream.
type Record struct {
	Type     string         `json:"type"`
	TxID     string         `json:"txid,omitempty"`
	Delta    int            `json:"delta,omitempty"`
	Balance  int            `json:"balance,omitempty"`
	Deltas   map[string]int `json:"deltas,omitempty"`
	Decision string         `json:"decision,omitempty"`
}

// Log is an append-only persistent record log. Every Append is written and
// fsynced before it returns, so a party may "crash" immediately after any
// single Append and later recover everything written so far by replaying
// the file.
type Log struct {
	mu sync.Mutex
	f  *os.File
}

// OpenLog opens (creating if necessary) the log at path and returns it
// together with all records already persisted in it.
func OpenLog(path string) (*Log, []Record, error) {
	var recs []Record
	data, err := os.ReadFile(path)
	switch {
	case err == nil:
		sc := bufio.NewScanner(bytes.NewReader(data))
		for sc.Scan() {
			line := sc.Bytes()
			if len(bytes.TrimSpace(line)) == 0 {
				continue
			}
			var r Record
			if err := json.Unmarshal(line, &r); err != nil {
				return nil, nil, err
			}
			recs = append(recs, r)
		}
		if err := sc.Err(); err != nil {
			return nil, nil, err
		}
	case os.IsNotExist(err):
		// fresh log
	default:
		return nil, nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, nil, err
	}
	return &Log{f: f}, recs, nil
}

// Append persists one record and fsyncs before returning.
func (l *Log) Append(r Record) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	if _, err := l.f.Write(append(b, '\n')); err != nil {
		return err
	}
	return l.f.Sync()
}

// Close closes the underlying file.
func (l *Log) Close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.f.Close()
}
