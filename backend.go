package logcursor

import (
	"bytes"
	"errors"
	"sort"
	"sync"
)

// Backend level errors.
var (
	ErrEmptyStep           = errors.New("logcursor: step name must not be empty")
	ErrInvalidAttempt      = errors.New("logcursor: attempt number must not be negative")
	ErrInvalidOffset       = errors.New("logcursor: byte offset must not be negative")
	ErrInvalidGap          = errors.New("logcursor: gap range must be non-empty and ordered")
	ErrEmptyFragment       = errors.New("logcursor: fragment payload must not be empty")
	ErrFragmentOverlap     = errors.New("logcursor: fragment overlaps existing data with different bytes")
	ErrFragmentOverlapsGap = errors.New("logcursor: fragment overlaps a declared gap")
	ErrGapOverlapsFragment = errors.New("logcursor: gap overlaps existing fragment data")
	ErrGapOverlap          = errors.New("logcursor: gap overlaps another gap")
	ErrEndConflict         = errors.New("logcursor: conflicting end marker")
	ErrEndBeforeData       = errors.New("logcursor: end marker precedes existing data")
	ErrDataAfterEnd        = errors.New("logcursor: data would extend past the end marker")
	ErrSealed              = errors.New("logcursor: log is sealed")
)

// Log is an in-memory, append-only backend for fragmented step logs.
//
// Each step owns a sequence of attempts; every attempt numbers its own byte
// stream from zero. Workers append byte fragments at explicit stream offsets,
// declare unrecoverable gaps, and publish an end marker. The Log deliberately
// performs no ordering policy beyond validation: fragments may arrive out of
// order, and fragments belonging to an older attempt may arrive after a retry
// has started. The read side selects the highest attempt visible in a snapshot
// and ignores everything else, so late content from a superseded attempt can
// never surface through a cursor.
//
// Log is safe for concurrent use.
type Log struct {
	mu      sync.RWMutex
	sealed  bool
	version int64
	steps   map[string]*stepLog
}

type stepLog struct {
	attempts map[int]*attemptLog
}

type attemptLog struct {
	ver     int64 // version at which the attempt became visible
	nextSeq int
	frags   []*fragment
	gaps    []gap
	endSet  bool
	endOff  int64
	endVer  int64
}

type fragment struct {
	seq   int
	start int64
	data  []byte
	ver   int64 // version at which this fragment became visible
}

func (f *fragment) end() int64 { return f.start + int64(len(f.data)) }

type gap struct {
	start int64
	end   int64
	ver   int64 // version at which this gap became visible
}

// NewLog returns an empty backend.
func NewLog() *Log {
	return &Log{steps: make(map[string]*stepLog)}
}

// Version returns the current snapshot version. Every visibility-affecting call
// advances it, so a Version value identifies exactly the upstream-visible state
// and can be used to reconstruct that state later.
func (l *Log) Version() int64 {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return l.version
}

// attempt returns the attempt, creating it as visible from the current version.
// Callers must bump l.version before calling it so that creation and the first
// recorded item share one version.
func (l *Log) attempt(step string, attempt int) *attemptLog {
	sl := l.steps[step]
	if sl == nil {
		sl = &stepLog{attempts: make(map[int]*attemptLog)}
		l.steps[step] = sl
	}
	a := sl.attempts[attempt]
	if a == nil {
		a = &attemptLog{ver: l.version}
		sl.attempts[attempt] = a
	}
	return a
}

// AppendFragment records data as the bytes starting at offset within
// (step, attempt). Offsets are absolute within the attempt's byte stream and
// attempt numbering restarts at zero for every attempt.
//
// Appending the exact same bytes at the exact same range is idempotent. Any
// other overlap with already recorded bytes, or with a declared gap, is
// rejected.
func (l *Log) AppendFragment(step string, attempt int, offset int64, data []byte) error {
	if step == "" {
		return ErrEmptyStep
	}
	if attempt < 0 {
		return ErrInvalidAttempt
	}
	if offset < 0 {
		return ErrInvalidOffset
	}
	if len(data) == 0 {
		return ErrEmptyFragment
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.sealed {
		return ErrSealed
	}
	end := offset + int64(len(data))
	if a := l.peekAttempt(step, attempt); a != nil {
		if a.endSet && end > a.endOff {
			return ErrDataAfterEnd
		}
		for _, g := range a.gaps {
			if offset < g.end && g.start < end {
				return ErrFragmentOverlapsGap
			}
		}
		for _, f := range a.frags {
			if offset < f.end() && f.start < end {
				if offset == f.start && end == f.end() && bytes.Equal(data, f.data) {
					return nil
				}
				return ErrFragmentOverlap
			}
		}
	}
	l.version++
	a := l.attempt(step, attempt)
	a.frags = append(a.frags, &fragment{
		seq:   a.nextSeq,
		start: offset,
		data:  append([]byte(nil), data...),
		ver:   l.version,
	})
	a.nextSeq++
	return nil
}

// DeclareGap records that the byte range [start, end) of (step, attempt) is
// permanently unrecoverable. Every fragment previously numbered in that range
// is considered lost. Repeating an identical declaration is idempotent.
func (l *Log) DeclareGap(step string, attempt int, start, end int64) error {
	if step == "" {
		return ErrEmptyStep
	}
	if attempt < 0 {
		return ErrInvalidAttempt
	}
	if start < 0 || end < 0 || start >= end {
		return ErrInvalidGap
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.sealed {
		return ErrSealed
	}
	if a := l.peekAttempt(step, attempt); a != nil {
		if a.endSet && end > a.endOff {
			return ErrDataAfterEnd
		}
		for _, g := range a.gaps {
			if start == g.start && end == g.end {
				return nil
			}
			if start < g.end && g.start < end {
				return ErrGapOverlap
			}
		}
		for _, f := range a.frags {
			if start < f.end() && f.start < end {
				return ErrGapOverlapsFragment
			}
		}
	}
	a := l.attempt(step, attempt)
	l.version++
	a.gaps = append(a.gaps, gap{start: start, end: end, ver: l.version})
	sort.Slice(a.gaps, func(i, j int) bool { return a.gaps[i].start < a.gaps[j].start })
	return nil
}

// DeclareEnd marks endOffset as the exclusive end of (step, attempt)'s byte
// stream. It may be declared before every byte has arrived: readers keep
// waiting for missing ranges (or their gap declarations) until the stream is
// contiguous up to endOffset. An identical repeated declaration is idempotent.
func (l *Log) DeclareEnd(step string, attempt int, endOffset int64) error {
	if step == "" {
		return ErrEmptyStep
	}
	if attempt < 0 {
		return ErrInvalidAttempt
	}
	if endOffset < 0 {
		return ErrInvalidOffset
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.sealed {
		return ErrSealed
	}
	if a := l.peekAttempt(step, attempt); a != nil {
		if a.endSet {
			if a.endOff == endOffset {
				return nil
			}
			return ErrEndConflict
		}
		for _, f := range a.frags {
			if f.end() > endOffset {
				return ErrEndBeforeData
			}
		}
		for _, g := range a.gaps {
			if g.end > endOffset {
				return ErrEndBeforeData
			}
		}
	}
	l.version++
	a := l.attempt(step, attempt)
	a.endSet = true
	a.endOff = endOffset
	a.endVer = l.version
	return nil
}

// HasGapRange reports whether every byte of [start, end) in (step, attempt) is
// covered by declared gaps. It is a query helper for callers that want to show
// "these bytes are gone" summaries; cursors rely on the same declarations.
func (l *Log) HasGapRange(step string, attempt int, start, end int64) bool {
	if start >= end || start < 0 {
		return false
	}
	l.mu.RLock()
	defer l.mu.RUnlock()
	sl := l.steps[step]
	if sl == nil {
		return false
	}
	a := sl.attempts[attempt]
	if a == nil {
		return false
	}
	cur := start
	for _, g := range a.gaps { // sorted by start
		if g.end <= cur {
			continue
		}
		if g.start > cur {
			return false
		}
		cur = g.end
		if cur >= end {
			return true
		}
	}
	return cur >= end
}

// Seal closes the log to further mutations. Snapshots taken before and after
// sealing are stable; sealing is an optional visibility fence for callers.
func (l *Log) Seal() {
	l.mu.Lock()
	l.sealed = true
	l.mu.Unlock()
}

// Sealed reports whether Seal has been called.
func (l *Log) Sealed() bool {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return l.sealed
}

// peekAttempt returns the attempt without creating it.
func (l *Log) peekAttempt(step string, attempt int) *attemptLog {
	sl := l.steps[step]
	if sl == nil {
		return nil
	}
	return sl.attempts[attempt]
}

// snapshot is an immutable deep copy of the state visible at one version.
// Cursors share snapshots freely and never mutate them.
type snapshot struct {
	steps map[string]*stepSnapshot
	order []string
}

type stepSnapshot struct {
	maxAttempt int
	attempts   map[int]*attemptSnapshot
}

type attemptSnapshot struct {
	endSet bool
	endOff int64
	frags  []fragSnapshot // sorted by start, disjoint
	gaps   []gapSnapshot  // sorted by start, disjoint
}

type fragSnapshot struct {
	seq   int
	start int64
	data  []byte
}

func (f *fragSnapshot) end() int64 { return f.start + int64(len(f.data)) }

type gapSnapshot struct {
	start int64
	end   int64
}

func (s *snapshot) attempt(step string, num int) *attemptSnapshot {
	ss := s.steps[step]
	if ss == nil {
		return nil
	}
	return ss.attempts[num]
}

// freezeAt reconstructs the immutable snapshot of version v. Because every
// recorded item carries the version at which it became visible, any past
// version can be rebuilt exactly; later appends never leak into it.
func (l *Log) freezeAt(v int64) *snapshot {
	l.mu.RLock()
	defer l.mu.RUnlock()
	snap := &snapshot{steps: make(map[string]*stepSnapshot, len(l.steps))}
	for name, sl := range l.steps {
		ss := &stepSnapshot{
			maxAttempt: -1,
			attempts:   make(map[int]*attemptSnapshot, len(sl.attempts)),
		}
		for num, a := range sl.attempts {
			if a.ver > v {
				continue
			}
			as := &attemptSnapshot{endSet: a.endSet && a.endVer <= v, endOff: a.endOff}
			for _, f := range a.frags {
				if f.ver > v {
					continue
				}
				as.frags = append(as.frags, fragSnapshot{
					seq:   f.seq,
					start: f.start,
					data:  f.data,
				})
			}
			sort.Slice(as.frags, func(i, j int) bool { return as.frags[i].start < as.frags[j].start })
			for _, g := range a.gaps {
				if g.ver > v {
					continue
				}
				as.gaps = append(as.gaps, gapSnapshot{start: g.start, end: g.end})
			}
			sort.Slice(as.gaps, func(i, j int) bool { return as.gaps[i].start < as.gaps[j].start })
			ss.attempts[num] = as
			if num > ss.maxAttempt {
				ss.maxAttempt = num
			}
		}
		// A step is only visible once at least one of its attempts is. This
		// keeps historical snapshots coherent when an attempt is created
		// concurrently with a reader that already captured the older version.
		if ss.maxAttempt < 0 {
			continue
		}
		snap.steps[name] = ss
	}
	snap.order = make([]string, 0, len(snap.steps))
	for name := range snap.steps {
		snap.order = append(snap.order, name)
	}
	sort.Strings(snap.order)
	return snap
}
