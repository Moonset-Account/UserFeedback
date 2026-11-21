package expertparallel

import (
	"errors"
	"sort"
	"sync"
)

// ErrBatchesIncomplete is returned when Dispatch is called before every
// participating rank has submitted its batch.
var ErrBatchesIncomplete = errors.New("expertparallel: not every rank has submitted a batch")

type phase int

const (
	phaseCollecting phase = iota
	phaseDispatched
)

// tokenRecord stores everything the component must remember about one token
// between submission and finalization.
type tokenRecord struct {
	tokenID  string
	rankID   string
	position int
	// candidates is kept in submission order for validation only.
	candidates []Candidate
	// accepted maps expert ID -> accepted weight; filled during Dispatch.
	accepted map[string]int
}

// Round is one dispatch/reclaim round. A Round is safe for concurrent use:
// submissions, returns and queries may be issued from multiple goroutines.
type Round struct {
	mu sync.Mutex

	phase  phase
	vecLen int

	// Static configuration, in constructor order.
	ranks     []string
	rankIndex map[string]int
	experts   []string
	capacity  map[string]int

	// Collection phase state.
	submitted map[string]bool
	batchSize map[string]int
	tokens    map[string]*tokenRecord

	// Dispatch phase state.
	packages      map[string]*WorkPackage
	accepted      map[string]map[string]int // expert -> token -> weight
	returns       map[string]map[string][]float64
	totalItems    int
	returnedCount int
}

// NewRound creates a round over the given participating ranks and experts.
// Every rank must submit exactly one batch before Dispatch; every return vector
// must have vectorLength entries.
func NewRound(rankIDs []string, expertSpecs []ExpertSpec, vectorLength int) (*Round, error) {
	if len(rankIDs) == 0 {
		return nil, ErrNoRanks
	}
	if len(expertSpecs) == 0 {
		return nil, ErrNoExperts
	}
	if vectorLength <= 0 {
		return nil, ErrInvalidVectorLength
	}

	rankIndex := make(map[string]int, len(rankIDs))
	ranks := make([]string, 0, len(rankIDs))
	for _, id := range rankIDs {
		if id == "" {
			return nil, ErrInvalidRank
		}
		if _, dup := rankIndex[id]; dup {
			return nil, ErrInvalidRank
		}
		rankIndex[id] = len(ranks)
		ranks = append(ranks, id)
	}

	experts := make([]string, 0, len(expertSpecs))
	capacity := make(map[string]int, len(expertSpecs))
	for _, spec := range expertSpecs {
		if spec.ExpertID == "" {
			return nil, ErrInvalidExpert
		}
		if _, dup := capacity[spec.ExpertID]; dup {
			return nil, ErrInvalidExpert
		}
		if spec.Capacity < 0 {
			return nil, ErrInvalidCapacity
		}
		capacity[spec.ExpertID] = spec.Capacity
		experts = append(experts, spec.ExpertID)
	}

	return &Round{
		phase:     phaseCollecting,
		vecLen:    vectorLength,
		ranks:     ranks,
		rankIndex: rankIndex,
		experts:   experts,
		capacity:  capacity,
		submitted: make(map[string]bool),
		batchSize: make(map[string]int),
		tokens:    make(map[string]*tokenRecord),
		packages:  make(map[string]*WorkPackage),
		accepted:  make(map[string]map[string]int),
		returns:   make(map[string]map[string][]float64),
	}, nil
}

// SubmitBatch records one rank's batch. It validates the batch and stores it;
// nothing is dispatched until every rank has submitted.
func (r *Round) SubmitBatch(rankID string, tokens []Token) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	if r.phase != phaseCollecting {
		return ErrNotCollecting
	}
	if _, ok := r.rankIndex[rankID]; !ok {
		return ErrInvalidRank
	}
	if r.submitted[rankID] {
		return ErrDuplicateRank
	}

	// Validate the whole batch before recording anything, so a rejected batch
	// leaves the round untouched.
	seenPosition := make(map[int]bool, len(tokens))
	for _, tok := range tokens {
		if tok.TokenID == "" {
			return ErrUnknownToken
		}
		if _, dup := r.tokens[tok.TokenID]; dup {
			return ErrUnknownToken
		}
		if tok.BatchPosition < 0 || tok.BatchPosition >= len(tokens) {
			return ErrInvalidPosition
		}
		if seenPosition[tok.BatchPosition] {
			return ErrInvalidPosition
		}
		seenPosition[tok.BatchPosition] = true

		if len(tok.Candidates) > 2 {
			return ErrInvalidCandidate
		}
		seenExpert := make(map[string]bool, len(tok.Candidates))
		for _, cand := range tok.Candidates {
			if cand.Weight <= 0 {
				return ErrInvalidCandidate
			}
			if _, ok := r.capacity[cand.ExpertID]; !ok {
				return ErrInvalidCandidate
			}
			if seenExpert[cand.ExpertID] {
				return ErrDuplicateCandidate
			}
			seenExpert[cand.ExpertID] = true
		}
	}

	// Commit the batch.
	r.submitted[rankID] = true
	r.batchSize[rankID] = len(tokens)
	for _, tok := range tokens {
		candidates := make([]Candidate, len(tok.Candidates))
		copy(candidates, tok.Candidates)
		r.tokens[tok.TokenID] = &tokenRecord{
			tokenID:    tok.TokenID,
			rankID:     rankID,
			position:   tok.BatchPosition,
			candidates: candidates,
			accepted:   make(map[string]int),
		}
	}
	return nil
}

// AllBatchesCollected reports whether every participating rank has submitted.
func (r *Round) AllBatchesCollected() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.submitted) == len(r.ranks)
}

// Dispatch runs expert-capacity selection and returns the work packages, one
// per expert. Dispatch is only allowed once, after all batches are collected.
// The returned packages are independent copies; mutating them does not affect
// the round.
func (r *Round) Dispatch() ([]WorkPackage, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if r.phase != phaseCollecting {
		return nil, ErrNotCollecting
	}
	if len(r.submitted) != len(r.ranks) {
		return nil, ErrBatchesIncomplete
	}

	// Gather every candidate under each expert.
	perExpert := make(map[string][]WorkItem, len(r.experts))
	for _, rec := range r.tokens {
		for _, cand := range rec.candidates {
			perExpert[cand.ExpertID] = append(perExpert[cand.ExpertID], WorkItem{
				ExpertID: cand.ExpertID,
				TokenID:  rec.tokenID,
				Weight:   cand.Weight,
			})
		}
	}

	packages := make([]WorkPackage, 0, len(r.experts))
	for _, expertID := range r.experts {
		items := perExpert[expertID]
		// Selection order: weight descending, token ID ascending.
		sort.SliceStable(items, func(i, j int) bool {
			if items[i].Weight != items[j].Weight {
				return items[i].Weight > items[j].Weight
			}
			return items[i].TokenID < items[j].TokenID
		})

		cap := r.capacity[expertID]
		if cap > len(items) {
			cap = len(items)
		}
		chosen := items[:cap]

		itemCopy := make([]WorkItem, len(chosen))
		copy(itemCopy, chosen)
		r.packages[expertID] = &WorkPackage{ExpertID: expertID, WorkItems: itemCopy}
		r.accepted[expertID] = make(map[string]int, len(chosen))

		for _, item := range chosen {
			r.accepted[expertID][item.TokenID] = item.Weight
			r.tokens[item.TokenID].accepted[expertID] = item.Weight
			r.totalItems++
		}

		// Always emit a package per configured expert, even when it has no work.
		packages = append(packages, WorkPackage{
			ExpertID:  expertID,
			WorkItems: append([]WorkItem(nil), itemCopy...),
		})
		r.returns[expertID] = make(map[string][]float64)
	}

	r.phase = phaseDispatched
	return packages, nil
}

// Dispatched reports whether Dispatch has completed.
func (r *Round) Dispatched() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.phase == phaseDispatched
}

// TotalWorkItems returns the number of distinct accepted work items produced by
// dispatch.
func (r *Round) TotalWorkItems() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.totalItems
}

// ReturnedCount returns the number of distinct work items whose vector has been
// received. Re-sent packets are not counted again.
func (r *Round) ReturnedCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.returnedCount
}

// RemainingWorkItems lists accepted work items that still lack a return vector,
// in expert order and selection order.
func (r *Round) RemainingWorkItems() []WorkItem {
	r.mu.Lock()
	defer r.mu.Unlock()

	if r.phase != phaseDispatched {
		return nil
	}
	var out []WorkItem
	for _, expertID := range r.experts {
		for _, item := range r.packages[expertID].WorkItems {
			if _, ok := r.returns[expertID][item.TokenID]; !ok {
				out = append(out, item)
			}
		}
	}
	return out
}

// SubmitReturn accepts one return packet. Packets may arrive in any order and
// may be re-sent: a re-send for a work item already received is silently
// ignored (it never changes completion state) and the stored first vector is
// kept.
func (r *Round) SubmitReturn(pkt ReturnPacket) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	if r.phase != phaseDispatched {
		return ErrNotDispatched
	}
	items, ok := r.accepted[pkt.ExpertID]
	if !ok {
		return ErrUnknownWorkItem
	}
	if _, ok := items[pkt.TokenID]; !ok {
		return ErrUnknownWorkItem
	}
	if len(pkt.Vector) != r.vecLen {
		return ErrVectorLengthMismatch
	}
	if _, exists := r.returns[pkt.ExpertID][pkt.TokenID]; exists {
		// Duplicate re-send: idempotent no-op so a repeated packet can never
		// make any token complete early.
		return nil
	}

	vec := make([]float64, len(pkt.Vector))
	copy(vec, pkt.Vector)
	r.returns[pkt.ExpertID][pkt.TokenID] = vec
	r.returnedCount++
	return nil
}

// Complete reports whether every distinct accepted work item has returned.
func (r *Round) Complete() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.phase == phaseDispatched && r.returnedCount == r.totalItems
}

// Results finalizes the round and restores every token to its rank's original
// batch positions. It returns ErrIncomplete until all work items are back.
// Each call returns fresh slices the caller may mutate.
func (r *Round) Results() ([]RankResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if r.phase != phaseDispatched {
		return nil, ErrNotDispatched
	}
	if r.returnedCount != r.totalItems {
		return nil, ErrIncomplete
	}

	results := make([]RankResult, len(r.ranks))
	for i, rankID := range r.ranks {
		results[i] = RankResult{
			RankID: rankID,
			Tokens: make([]*TokenResult, r.batchSize[rankID]),
		}
	}

	for _, rec := range r.tokens {
		results[r.rankIndex[rec.rankID]].Tokens[rec.position] = r.buildTokenResult(rec)
	}
	return results, nil
}

// buildTokenResult must be called under r.mu.
func (r *Round) buildTokenResult(rec *tokenRecord) *TokenResult {
	out := &TokenResult{
		TokenID:       rec.tokenID,
		BatchPosition: rec.position,
		Vector:        make([]float64, r.vecLen),
	}

	if len(rec.accepted) == 0 {
		// Fully rejected: backfill an all-zero vector and flag overflow.
		out.Overflow = true
		return out
	}

	// Deterministic expert order for reporting and merging.
	expertIDs := make([]string, 0, len(rec.accepted))
	sum := 0
	for expertID, w := range rec.accepted {
		expertIDs = append(expertIDs, expertID)
		sum += w
	}
	sort.Strings(expertIDs)
	out.AcceptedExperts = expertIDs

	// Re-normalize over the weights actually accepted by this token.
	normalized := make(map[string]float64, len(expertIDs))
	for _, expertID := range expertIDs {
		normalized[expertID] = float64(rec.accepted[expertID]) / float64(sum)
	}
	for _, expertID := range expertIDs {
		vec := r.returns[expertID][rec.tokenID] // guaranteed present: round complete
		w := normalized[expertID]
		for i := range out.Vector {
			out.Vector[i] += w * vec[i]
		}
	}
	return out
}
