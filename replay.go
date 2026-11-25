package zonetransfer

import "fmt"

// ApplyDelta applies a change set to a record set using strict IXFR
// semantics: every record in Deletes must currently exist and every record in
// Adds must be absent. The input slice is never modified; a new slice is
// returned. The ordering of the resulting records is deterministic (records
// are tracked as a set keyed by the Record tuple).
func ApplyDelta(current []Record, d Delta) ([]Record, error) {
	present := make(map[Record]struct{}, len(current)+len(d.Adds))
	for _, r := range current {
		if _, dup := present[r]; dup {
			return nil, fmt.Errorf("%w: zone contains duplicate record %+v", ErrInvalidDelta, r)
		}
		present[r] = struct{}{}
	}
	for _, r := range d.Deletes {
		if _, ok := present[r]; !ok {
			return nil, fmt.Errorf("%w: delete of absent record %+v", ErrInvalidDelta, r)
		}
		delete(present, r)
	}
	for _, r := range d.Adds {
		if _, ok := present[r]; ok {
			return nil, fmt.Errorf("%w: add of already present record %+v", ErrInvalidDelta, r)
		}
		present[r] = struct{}{}
	}
	out := make([]Record, 0, len(present))
	for r := range present {
		out = append(out, r)
	}
	return out, nil
}

// Replay applies an ordered delta chain to the starting record set. It stops
// at the first delta that fails strict application.
func Replay(start []Record, chain []Delta) ([]Record, error) {
	cur := CloneRecords(start)
	var err error
	for _, d := range chain {
		cur, err = ApplyDelta(cur, d)
		if err != nil {
			return nil, err
		}
	}
	return cur, nil
}
