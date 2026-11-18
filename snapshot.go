// Package hibersnapshot implements a hibernation-image snapshot builder for a
// synthetic multi-process host sample.
//
// The sample models physical pages, per-process virtual->physical mappings and
// page labels. The builder freezes one consistent snapshot boundary: if the
// mapping set changes while the boundary is being collected, the attempt is
// discarded and retried. A successful image therefore never mixes an old
// mapping reference with a new page identity.
package hibersnapshot

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"sync"
)

// Label is a per-physical-page policy label.
type Label int

const (
	// LabelNormal pages may be exported verbatim.
	LabelNormal Label = iota
	// LabelNoImage pages must never carry real content into an image.
	LabelNoImage
)

// Page is a synthetic physical page.
type Page struct {
	PFN   uint64
	Label Label
	Bytes []byte
}

// Mapping is one process virtual address bound to a physical page.
type Mapping struct {
	PID          int
	VA           uint64
	PFN          uint64
	Confidential bool // this reference requires the target content to stay private
}

// Ref identifies a single mapping reference.
type Ref struct {
	PID int
	VA  uint64
}

// Entry is one exported page entry of a snapshot image.
type Entry struct {
	PFN       uint64
	Content   []byte // nil when redacted
	Redacted  bool
	ReinitKey string // shared by every recoverable mapping of a redacted page
}

// RecoveryEntry describes one mapping that must be rebuilt on resume. All
// references of a redacted shared page carry the same ReinitKey, i.e. the same
// re-initialization marker.
type RecoveryEntry struct {
	PFN       uint64
	Ref       Ref
	ReinitKey string
}

// Image is a consistent snapshot image bound to one generation boundary.
type Image struct {
	Generation uint64
	Entries    []Entry
	Recovery   []RecoveryEntry
	Attempts   int
}

// RecoveryPlan returns the mappings that the resume entry point must rebuild.
func (im *Image) RecoveryPlan() []RecoveryEntry {
	out := make([]RecoveryEntry, len(im.Recovery))
	copy(out, im.Recovery)
	return out
}

// SnapshotInconsistentError reports that a generation moved under a build
// attempt. Such attempts are discarded rather than partially exported.
type SnapshotInconsistentError struct {
	Boundary uint64
	Observed uint64
}

func (e *SnapshotInconsistentError) Error() string {
	return fmt.Sprintf("hibersnapshot: mapping generation changed during build (boundary=%d observed=%d)", e.Boundary, e.Observed)
}

// RetryBudgetExhaustedError reports that every retry observed a fresh change,
// so no consistent boundary could be captured.
type RetryBudgetExhaustedError struct {
	Attempts int
	Last     *SnapshotInconsistentError
}

func (e *RetryBudgetExhaustedError) Error() string {
	return fmt.Sprintf("hibersnapshot: retry budget exhausted after %d attempts: %v", e.Attempts, e.Last)
}

func (e *RetryBudgetExhaustedError) Unwrap() error { return e.Last }

// reinitMarker derives a deterministic marker for a physical page. It is stable
// across retries and identical for every mapping referencing the page, so all
// recoverable mappings share one re-initialization marker.
func reinitMarker(pfn uint64) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("reinit:pfn=%d", pfn)))
	return hex.EncodeToString(sum[:16])
}

// SampleStore is the local synthetic page-table sample the builder reads from.
// Every mutating operation bumps an immutable generation counter, which is how
// the builder proves a boundary did not move underneath it.
type SampleStore struct {
	mu sync.Mutex

	pages      map[uint64]*Page
	mappings   map[Ref]Mapping
	generation uint64

	// OnBuildAttempt, when set, runs at the start of each build attempt while
	// the store lock is released. Tests use it to interleave mapping changes
	// with snapshot generation.
	OnBuildAttempt func(attempt int)
}

// NewSampleStore creates an empty sample store at generation 0.
func NewSampleStore() *SampleStore {
	return &SampleStore{pages: map[uint64]*Page{}, mappings: map[Ref]Mapping{}}
}

// AddPage registers a synthetic physical page. Its bytes are copied.
func (s *SampleStore) AddPage(p Page) {
	s.mu.Lock()
	defer s.mu.Unlock()
	cp := make([]byte, len(p.Bytes))
	copy(cp, p.Bytes)
	s.pages[p.PFN] = &Page{PFN: p.PFN, Label: p.Label, Bytes: cp}
}

// AddMapping installs a mapping. A genuinely new mapping bumps the generation.
func (s *SampleStore) AddMapping(m Mapping) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.pages[m.PFN]; !ok {
		return fmt.Errorf("hibersnapshot: unknown physical page pfn=%d", m.PFN)
	}
	ref := Ref{PID: m.PID, VA: m.VA}
	if old, ok := s.mappings[ref]; ok && old == m {
		return nil
	}
	s.mappings[ref] = m
	s.generation++
	return nil
}

// Remap moves a reference to a (possibly different) physical page and/or flips
// its confidentiality flag. The generation bump invalidates any in-flight
// boundary so the old reference and the new page identity can never be saved
// together.
func (s *SampleStore) Remap(pid int, va, newPFN uint64, confidential bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	ref := Ref{PID: pid, VA: va}
	if _, ok := s.mappings[ref]; !ok {
		return fmt.Errorf("hibersnapshot: no mapping at pid=%d va=%#x", pid, va)
	}
	if _, ok := s.pages[newPFN]; !ok {
		return fmt.Errorf("hibersnapshot: unknown physical page pfn=%d", newPFN)
	}
	next := Mapping{PID: pid, VA: va, PFN: newPFN, Confidential: confidential}
	if old := s.mappings[ref]; old == next {
		return nil
	}
	s.mappings[ref] = next
	s.generation++
	return nil
}

// RemoveMapping deletes a reference and bumps the generation.
func (s *SampleStore) RemoveMapping(pid int, va uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ref := Ref{PID: pid, VA: va}
	if _, ok := s.mappings[ref]; !ok {
		return
	}
	delete(s.mappings, ref)
	s.generation++
}

// Generation returns the current mapping-generation counter.
func (s *SampleStore) Generation() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.generation
}

// snapshot is an immutable point-in-time read used by one build attempt.
type snapshot struct {
	generation uint64
	pages      map[uint64]*Page
	mappings   map[Ref]Mapping
}

func (s *SampleStore) readSnapshot() snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	pages := make(map[uint64]*Page, len(s.pages))
	for pfn, p := range s.pages {
		cp := make([]byte, len(p.Bytes))
		copy(cp, p.Bytes)
		pages[pfn] = &Page{PFN: p.PFN, Label: p.Label, Bytes: cp}
	}
	maps := make(map[Ref]Mapping, len(s.mappings))
	for r, m := range s.mappings {
		maps[r] = m
	}
	return snapshot{generation: s.generation, pages: pages, mappings: maps}
}

func (s *SampleStore) runAttemptHook(attempt int) {
	s.mu.Lock()
	hook := s.OnBuildAttempt
	s.mu.Unlock()
	if hook != nil {
		hook(attempt)
	}
}

func sortedRefs(m map[Ref]Mapping) []Ref {
	refs := make([]Ref, 0, len(m))
	for r := range m {
		refs = append(refs, r)
	}
	sort.Slice(refs, func(i, j int) bool {
		if refs[i].PID != refs[j].PID {
			return refs[i].PID < refs[j].PID
		}
		return refs[i].VA < refs[j].VA
	})
	return refs
}
