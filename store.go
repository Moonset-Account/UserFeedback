package seatledger

import (
	"bufio"
	"bytes"
	"encoding/json"
	"os"
)

// Event types persisted in the per-branch log.
const (
	evAllocate = "allocate"   // HQ issued seats to this branch
	evCheckout = "checkout"   // seats moved available -> in use
	evCheckin  = "checkin"    // seats moved in use -> available
	evFreeze   = "freeze"     // outgoing transfer initiated, seats frozen
	evAccept   = "accept"     // incoming transfer verified and credited
	evRejectIn = "reject_in"  // incoming transfer refused
	evConfirm  = "confirm"    // outgoing transfer acked, seats left the quota
	evUnfreeze = "unfreeze"   // outgoing transfer rejected, seats back to available
)

// Event is one persisted state transition of a branch ledger.
type Event struct {
	Type  string `json:"type"`
	Seq   uint64 `json:"seq,omitempty"`
	ID    string `json:"id,omitempty"`
	Peer  string `json:"peer,omitempty"`
	Count int    `json:"count,omitempty"`
}

// Store appends events to a branch's log file, one JSON object per line.
type Store struct {
	path string
	f    *os.File
}

// OpenStore opens (creating if needed) the event log at path for appending.
func OpenStore(path string) (*Store, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	return &Store{path: path, f: f}, nil
}

// Path returns the log file path.
func (s *Store) Path() string { return s.path }

// Append persists one event and flushes it to stable storage.
func (s *Store) Append(e Event) error {
	b, err := json.Marshal(e)
	if err != nil {
		return err
	}
	if _, err := s.f.Write(append(b, '\n')); err != nil {
		return err
	}
	return s.f.Sync()
}

// Close closes the underlying file.
func (s *Store) Close() error { return s.f.Close() }

// ReadPrefix loads the persisted event prefix from path. A missing file
// yields no events. Parsing stops at the first undecodable line, so a torn
// final write (crash mid-append) is ignored and only the complete prefix is
// replayed.
func ReadPrefix(path string) ([]Event, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	defer f.Close()

	var events []Event
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 1<<20), 1<<20)
	for sc.Scan() {
		line := bytes.TrimSpace(sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var e Event
		if err := json.Unmarshal(line, &e); err != nil {
			break // torn tail: keep only the valid prefix
		}
		events = append(events, e)
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	return events, nil
}
