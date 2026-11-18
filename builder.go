package hibersnapshot

import (
	"sort"
)

// DefaultMaxAttempts is the default retry budget for Build.
const DefaultMaxAttempts = 5

// BuildOption configures a Build call.
type BuildOption func(*buildConfig)

type buildConfig struct {
	maxAttempts int
}

// WithMaxAttempts overrides the retry budget. A value <= 0 is ignored.
func WithMaxAttempts(n int) BuildOption {
	return func(c *buildConfig) {
		if n > 0 {
			c.maxAttempts = n
		}
	}
}

// Build freezes one consistent snapshot boundary from the local sample.
//
// Boundary rule: the generation read at the start of an attempt must equal the
// generation after collection. Any mapping change in between invalidates the
// attempt and triggers a full retry, so a returned image can never contain an
// old mapping reference together with a new page identity.
//
// Content rule: a page is redacted (no real bytes exported) when its label is
// LabelNoImage OR any mapping referencing it is confidential. When two
// processes share such a page, every reference to it is kept recoverable and
// all references share one deterministic re-initialization marker.
func (s *SampleStore) Build(opts ...BuildOption) (*Image, error) {
	cfg := buildConfig{maxAttempts: DefaultMaxAttempts}
	for _, o := range opts {
		o(&cfg)
	}

	var lastErr *SnapshotInconsistentError
	for attempt := 1; attempt <= cfg.maxAttempts; attempt++ {
		// 1. Pin the boundary and take an immutable copy of the sample.
		snap := s.readSnapshot()

		// 2. Interleaving point: the store lock is released here, so a
		// concurrent remap can land while the boundary is being assembled.
		s.runAttemptHook(attempt)

		// 3. Assemble entries from the pinned (possibly stale) copy.
		entries, recovery, err := buildEntries(snap)
		if err != nil {
			return nil, err
		}

		// 4. Prove the boundary did not move. Any change invalidates the
		// whole attempt — old references and new page identities can never
		// be exported together.
		current := s.Generation()
		if current != snap.generation {
			lastErr = &SnapshotInconsistentError{Boundary: snap.generation, Observed: current}
			continue // discard the whole attempt and retry at the new boundary
		}

		return &Image{
			Generation: snap.generation,
			Entries:    entries,
			Recovery:   recovery,
			Attempts:   attempt,
		}, nil
	}

	return nil, &RetryBudgetExhaustedError{Attempts: cfg.maxAttempts, Last: lastErr}
}

func buildEntries(snap snapshot) ([]Entry, []RecoveryEntry, error) {
	// Determine which physical pages require redaction and collect all of
	// their references.
	redacted := map[uint64]bool{}
	refsByPFN := map[uint64][]Ref{}

	for ref, m := range snap.mappings {
		page, ok := snap.pages[m.PFN]
		if !ok {
			// Defensive: the store rejects mappings to unknown pages.
			return nil, nil, &storeConsistencyError{pid: m.PID, va: m.VA, pfn: m.PFN}
		}
		refsByPFN[m.PFN] = append(refsByPFN[m.PFN], ref)
		if page.Label == LabelNoImage || m.Confidential {
			redacted[m.PFN] = true
		}
	}

	pfns := make([]uint64, 0, len(snap.pages))
	for pfn := range snap.pages {
		pfns = append(pfns, pfn)
	}
	sort.Slice(pfns, func(i, j int) bool { return pfns[i] < pfns[j] })

	entries := make([]Entry, 0, len(pfns))
	var recovery []RecoveryEntry

	for _, pfn := range pfns {
		page := snap.pages[pfn]
		refs := refsByPFN[pfn]
		// Entries exist only for pages that are reachable from some mapping.
		if len(refs) == 0 {
			continue
		}

		entry := Entry{PFN: pfn}
		if redacted[pfn] {
			entry.Redacted = true
			entry.Content = nil
			key := reinitMarker(pfn)
			entry.ReinitKey = key
			for _, ref := range sortedRefsMap(refs) {
				recovery = append(recovery, RecoveryEntry{PFN: pfn, Ref: ref, ReinitKey: key})
			}
		} else {
			cp := make([]byte, len(page.Bytes))
			copy(cp, page.Bytes)
			entry.Content = cp
		}
		entries = append(entries, entry)
	}

	sort.Slice(recovery, func(i, j int) bool {
		if recovery[i].PFN != recovery[j].PFN {
			return recovery[i].PFN < recovery[j].PFN
		}
		if recovery[i].Ref.PID != recovery[j].Ref.PID {
			return recovery[i].Ref.PID < recovery[j].Ref.PID
		}
		return recovery[i].Ref.VA < recovery[j].Ref.VA
	})

	return entries, recovery, nil
}

type storeConsistencyError struct {
	pid int
	va  uint64
	pfn uint64
}

func (e *storeConsistencyError) Error() string {
	return "hibersnapshot: mapping references missing page pfn=" + uitoa(e.pfn)
}

func uitoa(v uint64) string {
	if v == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for v > 0 {
		i--
		buf[i] = byte('0' + v%10)
		v /= 10
	}
	return string(buf[i:])
}

// sortedRefsMap orders a reference slice deterministically.
func sortedRefsMap(refs []Ref) []Ref {
	out := append([]Ref(nil), refs...)
	sort.Slice(out, func(i, j int) bool {
		if out[i].PID != out[j].PID {
			return out[i].PID < out[j].PID
		}
		return out[i].VA < out[j].VA
	})
	return out
}
