// Package compactlog implements a segmented key-value log for a message
// broker. Each message carries a permanent offset and a write time; deletes
// are tombstone messages. The log supports time-based compaction of a
// caller-selected range of contiguous closed segments, and offset-based
// reads that skip over compacted gaps.
package compactlog

import (
	"errors"
	"fmt"
	"time"
)

// Message is a single record in the log. A delete is represented as a
// message with Tombstone set to true (Value is nil in that case).
type Message struct {
	Offset    int64
	Key       string
	Value     []byte
	Tombstone bool
	WrittenAt time.Time
}

// Segment is an ordered run of messages. Only the active (last, unsealed)
// segment accepts appends; sealed segments are immutable except that
// Clean may rewrite a selected contiguous range of them.
type Segment struct {
	ID       int
	Messages []Message
	Sealed   bool
}

// Log is the full partitioned log: an ordered list of segments whose last
// segment is the active one.
type Log struct {
	segments   []*Segment
	nextOffset int64
}

// NewLog returns an empty log with one active segment (ID 0).
func NewLog() *Log {
	l := &Log{}
	l.segments = append(l.segments, &Segment{ID: 0})
	return l
}

// Append writes a key-value update to the active segment and returns its
// permanently assigned offset.
func (l *Log) Append(key string, value []byte, at time.Time) int64 {
	return l.append(Message{
		Key:       key,
		Value:     value,
		Tombstone: false,
		WrittenAt: at,
	})
}

// Delete writes a tombstone for key to the active segment and returns its
// permanently assigned offset.
func (l *Log) Delete(key string, at time.Time) int64 {
	return l.append(Message{
		Key:       key,
		Tombstone: true,
		WrittenAt: at,
	})
}

func (l *Log) append(m Message) int64 {
	m.Offset = l.nextOffset
	l.nextOffset++
	active := l.segments[len(l.segments)-1]
	active.Messages = append(active.Messages, m)
	return m.Offset
}

// Seal closes the active segment and opens a new empty active segment.
// The returned ID is the ID of the segment that was just sealed.
func (l *Log) Seal() int {
	active := l.segments[len(l.segments)-1]
	active.Sealed = true
	l.segments = append(l.segments, &Segment{ID: active.ID + 1})
	return active.ID
}

// Segments returns a snapshot of the segment list. The returned Messages
// slices share storage with the log; treat them as read-only.
func (l *Log) Segments() []Segment {
	out := make([]Segment, len(l.segments))
	for i, s := range l.segments {
		out[i] = *s
	}
	return out
}

// ErrEmptyRange is returned when the requested segment range is invalid.
var ErrEmptyRange = errors.New("compactlog: empty or inverted segment range")

// ErrUnsealedSegment is returned when the requested range includes the
// active (unsealed) segment.
var ErrUnsealedSegment = errors.New("compactlog: range includes the active segment")

// Clean rewrites exactly the contiguous sealed segments with IDs
// [fromID, toID]. The active segment and all other sealed segments are left
// untouched. Rules, applied against the whole log:
//
//   - A record is dropped if a later record for the same key exists
//     anywhere in the log (the later record may lie outside the selection).
//   - If the key's last record in the whole log is a tombstone, the
//     tombstone is dropped only when its retention period has elapsed
//     (now - WrittenAt >= retention) AND no earlier record of the same key
//     survives this round (i.e. every earlier record sits inside the
//     selected range and is dropped by the rule above). Otherwise the
//     tombstone is kept.
//
// Offsets of surviving messages never change.
func (l *Log) Clean(now time.Time, retention time.Duration, fromID, toID int) error {
	if fromID > toID || fromID < 0 || toID >= len(l.segments) {
		return ErrEmptyRange
	}
	for id := fromID; id <= toID; id++ {
		if !l.segments[id].Sealed {
			return fmt.Errorf("segment %d: %w", id, ErrUnsealedSegment)
		}
	}

	// Latest record per key across the whole log.
	latest := make(map[string]Message)
	// Whether a key has any record outside the selected range.
	outside := make(map[string]bool)
	for i, seg := range l.segments {
		selected := i >= fromID && i <= toID
		for _, m := range seg.Messages {
			latest[m.Key] = m
			if !selected {
				outside[m.Key] = true
			}
		}
	}

	for id := fromID; id <= toID; id++ {
		seg := l.segments[id]
		kept := seg.Messages[:0]
		for _, m := range seg.Messages {
			last := latest[m.Key]
			if last.Offset != m.Offset {
				continue // a later record for this key exists
			}
			if m.Tombstone &&
				now.Sub(m.WrittenAt) >= retention &&
				!outside[m.Key] {
				continue // expired tombstone with no surviving earlier record
			}
			kept = append(kept, m)
		}
		// Detach the dropped tail so it can be garbage collected.
		for i := len(kept); i < len(seg.Messages); i++ {
			seg.Messages[i] = Message{}
		}
		seg.Messages = kept
	}
	return nil
}

// ReadFrom returns the first existing message whose offset is greater than
// or equal to offset, tolerating gaps left by compaction. The second return
// value is false when the log holds no message at or after offset (the
// reader has reached the end of the log).
func (l *Log) ReadFrom(offset int64) (Message, bool) {
	for _, seg := range l.segments {
		for _, m := range seg.Messages {
			if m.Offset >= offset {
				return m, true
			}
		}
	}
	return Message{}, false
}
