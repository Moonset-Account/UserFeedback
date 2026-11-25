package zonetransfer

import "errors"

// Record is a single zone resource record. The tuple (Name, Type, Data)
// identifies a record; two records are equal when all three fields are equal.
type Record struct {
	Name string
	Type string
	Data string
}

// Snapshot is an immutable full zone state. Version is an identifier
// independent of Serial: different versions may carry the same serial, in
// which case their contents must differ (they are not chainable).
type Snapshot struct {
	Version string
	Serial  uint32
	Records []Record
}

// Delta is an explicitly bounded change set: applying Deletes then Adds to a
// zone whose version is FromVersion must yield the zone identified by
// ToVersion. Bytes is the on-the-wire size of carrying this delta; it is the
// cost unit used when planning a transfer.
type Delta struct {
	FromVersion string
	ToVersion   string
	FromSerial  uint32
	ToSerial    uint32
	Deletes     []Record
	Adds        []Record
	Bytes       int
}

// Kind selects a full transfer (AXFR) or an incremental chain (IXFR).
type Kind int

const (
	// KindAXFR sends the complete target snapshot.
	KindAXFR Kind = iota
	// KindIXFR sends an ordered chain of deltas replaying the backup's
	// current zone into the target zone.
	KindIXFR
)

// String returns the DNS-style name of the transfer kind.
func (k Kind) String() string {
	switch k {
	case KindAXFR:
		return "AXFR"
	case KindIXFR:
		return "IXFR"
	default:
		return "UNKNOWN"
	}
}

// Plan is the directly applicable update stream produced by the primary.
type Plan struct {
	Kind Kind
	// Reason is populated for AXFR plans and documents why an incremental
	// chain could not be selected.
	Reason string
	// Target is the snapshot the backup must end up serving.
	Target Snapshot
	// Chain holds the ordered deltas for IXFR plans, and is empty for AXFR.
	Chain []Delta
	// Bytes is the total wire size of the chosen stream.
	Bytes int
}

// Reason strings returned on AXFR decisions and input errors.
const (
	ReasonNoSnapshot      = "backup serial matches no known snapshot"
	ReasonNonUniqueStart  = "backup serial matches multiple snapshots with different contents"
	ReasonNoChain         = "no replayable delta chain reaches the target"
	ReasonFullZoneCheaper = "full zone transfer uses no more bytes than the best delta chain"
)

// ErrInvalidDelta is returned when a delta cannot be replayed as declared.
var ErrInvalidDelta = errors.New("delta does not replay to its declared target content")

// ErrVerification is returned when the assembled zone does not match the
// expected target snapshot.
var ErrVerification = errors.New("assembled zone failed verification against target")

// ErrInvalidInput is returned for malformed planning inputs (duplicate or
// empty snapshot version ids).
var ErrInvalidInput = errors.New("invalid planning input")

// ErrBadTarget is returned when the requested target version is not among
// the known snapshots.
var ErrBadTarget = errors.New("unknown target version")

// SnapshotBytes is the wire-size model for a full zone: a fixed framing
// cost plus one unit per record (the realistic alternative of summing record
// lengths changes no planning decision in this package because all costs use
// the same model; explicit Delta.Bytes stay authoritative for deltas).
func SnapshotBytes(s Snapshot) int {
	return 1 + len(s.Records)
}

// CloneRecords returns a copy that shares no backing array with the input.
func CloneRecords(in []Record) []Record {
	if in == nil {
		return nil
	}
	out := make([]Record, len(in))
	copy(out, in)
	return out
}

func sameRecords(a, b []Record) bool {
	if len(a) != len(b) {
		return false
	}
	// Order-independent comparison: snapshots are record sets, not sequences.
	counts := make(map[Record]int, len(a))
	for _, r := range a {
		counts[r]++
	}
	for _, r := range b {
		counts[r]--
		if counts[r] < 0 {
			return false
		}
	}
	return true
}
