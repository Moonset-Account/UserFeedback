// Package auditsample implements a tamper-evident, replayable sampling
// module for independent audits of high-risk changes.
//
// Workflow:
//  1. The candidate set of high-risk changes is sealed into a versioned
//     Batch (Registry.Seal). Sealing records a SHA-256 digest of the
//     canonical candidate encoding, so any later silent replacement of
//     candidates is detectable.
//  2. After sealing, the auditor supplies a random seed. Draw derives a
//     deterministic sample from (sealed candidates, seed, rules): coverage
//     minimums per control category and per business line must be met, and
//     the total sample size is capped by the quota.
//  3. Duplicate change IDs count as a single sample unit; one change may
//     satisfy a control-category minimum and a business-line minimum at
//     the same time.
//  4. If the quota makes the coverage minimums unsatisfiable, Draw returns
//     an *UncoverableError listing every condition that cannot be covered.
//  5. A candidate correction seals a new batch (Registry.Correct); the
//     original batch, seed and rules still replay the original sample
//     (Verify).
package auditsample

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"math/rand"
	"sort"
	"strings"
)

// Condition kinds reported in UncoverableError.
const (
	KindControl = "control"
	KindBizLine = "bizline"
)

// Reasons a condition cannot be covered.
const (
	// ReasonInsufficientCandidates means the sealed candidate set does not
	// contain enough distinct changes satisfying the condition.
	ReasonInsufficientCandidates = "insufficient_candidates"
	// ReasonQuotaExceeded means the condition is satisfiable in principle
	// but the remaining picks do not fit within the quota.
	ReasonQuotaExceeded = "quota_exceeded"
)

// Candidate is one high-risk change eligible for sampling.
type Candidate struct {
	ChangeID string // unique change identifier; duplicates count once
	Control  string // control category the change belongs to
	BizLine  string // business line the change belongs to
}

// Rules describe the coverage minimums and the sample size quota.
type Rules struct {
	// Quota caps the total number of sampled changes.
	Quota int
	// ControlMinimum maps control category -> minimum sampled changes.
	ControlMinimum map[string]int
	// BizLineMinimum maps business line -> minimum sampled changes.
	BizLineMinimum map[string]int
}

func (r Rules) clone() Rules {
	out := Rules{Quota: r.Quota}
	if r.ControlMinimum != nil {
		out.ControlMinimum = make(map[string]int, len(r.ControlMinimum))
		for k, v := range r.ControlMinimum {
			out.ControlMinimum[k] = v
		}
	}
	if r.BizLineMinimum != nil {
		out.BizLineMinimum = make(map[string]int, len(r.BizLineMinimum))
		for k, v := range r.BizLineMinimum {
			out.BizLineMinimum[k] = v
		}
	}
	return out
}

// Batch is a sealed, versioned snapshot of the candidate set.
type Batch struct {
	Version    int
	Digest     string // SHA-256 hex of the canonical candidate encoding
	Candidates []Candidate
}

// Sample is the outcome of one Draw. Together with the sealed batch it
// replays deterministically.
type Sample struct {
	BatchVersion int
	BatchDigest  string
	Seed         uint64
	Rules        Rules
	ChangeIDs    []string // sampled change IDs, in pick order, deduplicated
}

// UncoverableCondition describes one coverage condition that cannot be met.
type UncoverableCondition struct {
	Kind     string // KindControl or KindBizLine
	Key      string // control category or business line
	Required int    // configured minimum
	Missing  int    // picks still missing when sampling stopped
	Reason   string // ReasonInsufficientCandidates or ReasonQuotaExceeded
}

// UncoverableError is returned by Draw when the coverage minimums cannot be
// satisfied within the quota from the sealed candidates.
type UncoverableError struct {
	Conditions []UncoverableCondition
}

func (e *UncoverableError) Error() string {
	parts := make([]string, 0, len(e.Conditions))
	for _, c := range e.Conditions {
		parts = append(parts, fmt.Sprintf("%s %q: required=%d missing=%d (%s)",
			c.Kind, c.Key, c.Required, c.Missing, c.Reason))
	}
	return "auditsample: uncoverable conditions: " + strings.Join(parts, "; ")
}

// Registry stores sealed batches. Sealing is append-only: a candidate
// correction opens a new batch version and never rewrites an old one.
type Registry struct {
	batches []*Batch
}

// NewRegistry returns an empty batch registry.
func NewRegistry() *Registry { return &Registry{} }

// Seal freezes candidates into a new versioned batch and records its
// digest. The input slice is copied, so later caller-side mutation cannot
// alter the sealed batch.
func (r *Registry) Seal(candidates []Candidate) (*Batch, error) {
	for i, c := range candidates {
		if c.ChangeID == "" {
			return nil, fmt.Errorf("auditsample: candidate %d has empty change id", i)
		}
	}
	cp := make([]Candidate, len(candidates))
	copy(cp, candidates)
	b := &Batch{
		Version:    len(r.batches) + 1,
		Digest:     digestCandidates(cp),
		Candidates: cp,
	}
	r.batches = append(r.batches, b)
	return cloneBatch(b), nil
}

// Correct seals a corrected candidate set as a new batch version. The
// predecessor batch stays intact so previously drawn samples still replay.
func (r *Registry) Correct(prev *Batch, candidates []Candidate) (*Batch, error) {
	if prev == nil || prev.Version < 1 || prev.Version > len(r.batches) {
		return nil, errors.New("auditsample: unknown predecessor batch")
	}
	return r.Seal(candidates)
}

// Batch returns a copy of the sealed batch with the given version.
func (r *Registry) Batch(version int) (*Batch, error) {
	if version < 1 || version > len(r.batches) {
		return nil, fmt.Errorf("auditsample: unknown batch version %d", version)
	}
	return cloneBatch(r.batches[version-1]), nil
}

func cloneBatch(b *Batch) *Batch {
	cp := make([]Candidate, len(b.Candidates))
	copy(cp, b.Candidates)
	return &Batch{Version: b.Version, Digest: b.Digest, Candidates: cp}
}

// digestCandidates computes the canonical digest of a candidate set. The
// digest binds the exact ordered contents, so replacing, reordering or
// editing any candidate changes it.
func digestCandidates(cs []Candidate) string {
	h := sha256.New()
	for _, c := range cs {
		fmt.Fprintf(h, "%s\x1f%s\x1f%s\x1e", c.ChangeID, c.Control, c.BizLine)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// dedupeCandidates keeps the first occurrence of each change ID: duplicate
// change numbers count as a single sample unit.
func dedupeCandidates(cs []Candidate) []Candidate {
	seen := make(map[string]bool, len(cs))
	out := make([]Candidate, 0, len(cs))
	for _, c := range cs {
		if seen[c.ChangeID] {
			continue
		}
		seen[c.ChangeID] = true
		out = append(out, c)
	}
	return out
}

// shuffledIndices derives a deterministic permutation from the auditor seed
// bound to the sealed batch digest.
func shuffledIndices(n int, seed uint64, digest string) []int {
	key := sha256.Sum256([]byte(fmt.Sprintf("%d|%s", seed, digest)))
	rng := rand.New(rand.NewSource(int64(binary.BigEndian.Uint64(key[:8]))))
	idx := make([]int, n)
	for i := range idx {
		idx[i] = i
	}
	rng.Shuffle(n, func(i, j int) { idx[i], idx[j] = idx[j], idx[i] })
	return idx
}

// Draw generates a sample from a sealed batch. It is a pure function of
// (batch candidates, seed, rules): the same inputs always reproduce the
// same sample.
//
// Selection first satisfies the coverage minimums, preferring changes that
// cover an unmet control category and an unmet business line at the same
// time, then fills the remaining quota in shuffled order. If the minimums
// cannot be met, Draw returns nil and an *UncoverableError.
func Draw(batch *Batch, seed uint64, rules Rules) (*Sample, error) {
	if batch == nil {
		return nil, errors.New("auditsample: nil batch")
	}
	if rules.Quota < 0 {
		return nil, fmt.Errorf("auditsample: negative quota %d", rules.Quota)
	}
	digest := digestCandidates(batch.Candidates)
	distinct := dedupeCandidates(batch.Candidates)
	order := shuffledIndices(len(distinct), seed, digest)

	controlNeed := positiveCopy(rules.ControlMinimum)
	lineNeed := positiveCopy(rules.BizLineMinimum)

	used := make([]bool, len(distinct))
	picked := make([]int, 0, rules.Quota)

	score := func(c Candidate) int {
		s := 0
		if controlNeed[c.Control] > 0 {
			s++
		}
		if lineNeed[c.BizLine] > 0 {
			s++
		}
		return s
	}
	remaining := func() int {
		t := 0
		for _, v := range controlNeed {
			t += v
		}
		for _, v := range lineNeed {
			t += v
		}
		return t
	}

	for remaining() > 0 {
		best, bestScore := -1, 0
		for _, idx := range order {
			if used[idx] {
				continue
			}
			if s := score(distinct[idx]); s > bestScore {
				best, bestScore = idx, s
			}
		}
		if bestScore == 0 {
			return nil, newUncoverable(rules, controlNeed, lineNeed, ReasonInsufficientCandidates)
		}
		if len(picked) >= rules.Quota {
			return nil, newUncoverable(rules, controlNeed, lineNeed, ReasonQuotaExceeded)
		}
		used[best] = true
		picked = append(picked, best)
		c := distinct[best]
		if controlNeed[c.Control] > 0 {
			controlNeed[c.Control]--
		}
		if lineNeed[c.BizLine] > 0 {
			lineNeed[c.BizLine]--
		}
	}

	// Fill the remaining quota in shuffled order. If fewer distinct
	// candidates exist than the quota, the sample simply contains all of
	// them.
	for _, idx := range order {
		if len(picked) >= rules.Quota {
			break
		}
		if used[idx] {
			continue
		}
		used[idx] = true
		picked = append(picked, idx)
	}

	ids := make([]string, len(picked))
	for i, idx := range picked {
		ids[i] = distinct[idx].ChangeID
	}
	return &Sample{
		BatchVersion: batch.Version,
		BatchDigest:  digest,
		Seed:         seed,
		Rules:        rules.clone(),
		ChangeIDs:    ids,
	}, nil
}

func positiveCopy(m map[string]int) map[string]int {
	out := make(map[string]int, len(m))
	for k, v := range m {
		if v > 0 {
			out[k] = v
		}
	}
	return out
}

func newUncoverable(rules Rules, controlNeed, lineNeed map[string]int, reason string) *UncoverableError {
	err := &UncoverableError{}
	keys := make([]string, 0, len(rules.ControlMinimum))
	for k := range rules.ControlMinimum {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if miss := controlNeed[k]; miss > 0 {
			err.Conditions = append(err.Conditions, UncoverableCondition{
				Kind:     KindControl,
				Key:      k,
				Required: rules.ControlMinimum[k],
				Missing:  miss,
				Reason:   reason,
			})
		}
	}
	keys = keys[:0]
	for k := range rules.BizLineMinimum {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if miss := lineNeed[k]; miss > 0 {
			err.Conditions = append(err.Conditions, UncoverableCondition{
				Kind:     KindBizLine,
				Key:      k,
				Required: rules.BizLineMinimum[k],
				Missing:  miss,
				Reason:   reason,
			})
		}
	}
	return err
}

// Verify checks that a sample still replays from the given sealed batch:
// the batch version must match, the candidate set must not have been
// silently replaced (digest recomputed from the actual candidates), and a
// fresh draw from the same seed and rules must reproduce the sample.
func Verify(s *Sample, batch *Batch) error {
	if s == nil || batch == nil {
		return errors.New("auditsample: nil sample or batch")
	}
	if batch.Version != s.BatchVersion {
		return fmt.Errorf("auditsample: batch version mismatch: sample=%d batch=%d",
			s.BatchVersion, batch.Version)
	}
	if got := digestCandidates(batch.Candidates); got != s.BatchDigest {
		return fmt.Errorf("auditsample: candidate digest mismatch: sealed candidates were replaced or corrupted")
	}
	replay, err := Draw(batch, s.Seed, s.Rules)
	if err != nil {
		return fmt.Errorf("auditsample: replay failed: %w", err)
	}
	if !equalStrings(replay.ChangeIDs, s.ChangeIDs) {
		return errors.New("auditsample: replay produced a different sample")
	}
	return nil
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
