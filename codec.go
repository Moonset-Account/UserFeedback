package causalcodec

import (
	"sort"
)

const (
	// OmitReasonPriorityThreshold marks causes removed by Codec.MinPriority.
	OmitReasonPriorityThreshold = "below_priority_threshold"
	// OmitReasonByteBudget marks causes removed to satisfy MaxBytes.
	OmitReasonByteBudget = "byte_budget_priority"
)

// Codec encodes reports under a fixed byte budget and optionally resolves
// anchors while decoding.
type Codec struct {
	// MaxBytes is the hard packet limit. Zero means DefaultMaxBytes; a
	// negative value disables budget pruning.
	MaxBytes int
	// MinPriority drops causes below this priority before byte pruning.
	MinPriority Priority
	// Anchors is used only by Decode. Nil means no anchors are available.
	Anchors AnchorResolver
}

// NewCodec returns a Codec with the given hard byte limit. A zero limit gets
// DefaultMaxBytes; use a negative value for an unlimited codec.
func NewCodec(maxBytes int) *Codec {
	if maxBytes == 0 {
		maxBytes = DefaultMaxBytes
	}
	return &Codec{MaxBytes: maxBytes}
}

// Packet is an encoded report together with the pruning plan.
type Packet struct {
	Bytes []byte
	Plan  Plan
}

// DroppedCause records why a cause left the inline packet.
type DroppedCause struct {
	ID       string   `json:"id"`
	Priority Priority `json:"priority"`
	Reason   string   `json:"reason"`
}

// Plan reports dedup, retention, anchors, and chain breaks for one encode.
type Plan struct {
	MaxBytes              int            `json:"max_bytes"`
	SizeBytes             int            `json:"size_bytes"`
	FullSizeBytes         int            `json:"full_size_bytes"`
	UniqueCauses          int            `json:"unique_causes"`
	CauseReferences       int            `json:"cause_references"`
	ReferencedCauses      int            `json:"referenced_causes"`
	RepeatedReferences    int            `json:"repeated_references"`
	DuplicateSavingsBytes int            `json:"duplicate_savings_bytes"`
	RetainedCauseIDs      []string       `json:"retained_cause_ids"`
	DroppedCauses         []DroppedCause `json:"dropped_causes,omitempty"`
	Anchors               []AnchorRef    `json:"anchors,omitempty"`
	GapIDs                []string       `json:"gap_ids,omitempty"`
}

// Encode is a convenience function with no byte-budget pruning.
func Encode(report Report) ([]byte, error) {
	pkt, err := (&Codec{MaxBytes: -1}).Encode(report)
	if err != nil {
		return nil, err
	}
	return pkt.Bytes, nil
}

// Decode is a convenience function using the supplied anchor resolver.
func Decode(data []byte, anchors AnchorResolver) (*Report, error) {
	return (&Codec{MaxBytes: -1, Anchors: anchors}).Decode(data)
}

// Encode applies validation, deduplication, anchor framing, priority pruning,
// and the hard byte budget.
func (c *Codec) Encode(report Report) (*Packet, error) {
	if err := report.Validate(); err != nil {
		return nil, err
	}

	maxBytes := DefaultMaxBytes
	minPriority := PriorityUnspecified
	if c != nil {
		if c.MaxBytes == 0 {
			maxBytes = DefaultMaxBytes
		} else {
			maxBytes = c.MaxBytes
		}
		minPriority = c.MinPriority
	}

	base := canonicalReport(report)
	useCount := make(map[string]int)
	refs := 0
	for _, p := range base.Paths {
		for _, s := range p.Segments {
			for _, id := range s.CauseIDs {
				useCount[id]++
				refs++
			}
		}
	}

	fullCandidate := buildCandidate(base, map[string]string{})
	fullBytes, err := encodePacket(fullCandidate)
	if err != nil {
		return nil, err
	}

	dropped := make(map[string]string)
	var droppedOrder []string
	if minPriority > PriorityUnspecified {
		ids := make([]string, 0, len(base.Causes))
		byID := make(map[string]Cause, len(base.Causes))
		for _, cause := range base.Causes {
			byID[cause.ID] = cause
			if cause.Priority < minPriority {
				ids = append(ids, cause.ID)
			}
		}
		sort.Slice(ids, func(i, j int) bool {
			a, b := byID[ids[i]], byID[ids[j]]
			if a.Priority != b.Priority {
				return a.Priority < b.Priority
			}
			return a.ID < b.ID
		})
		for _, id := range ids {
			if _, exists := dropped[id]; !exists {
				dropped[id] = OmitReasonPriorityThreshold
				droppedOrder = append(droppedOrder, id)
			}
		}
	}

	var candidate *Report
	var raw []byte
	for {
		candidate = buildCandidate(base, dropped)
		raw, err = encodePacket(candidate)
		if err != nil {
			return nil, err
		}
		if maxBytes < 0 || len(raw) <= maxBytes {
			break
		}

		victim := ""
		victimPriority := Priority(0)
		for i, cause := range candidate.Causes {
			if i == 0 || cause.Priority < victimPriority ||
				(cause.Priority == victimPriority && cause.ID < victim) {
				victim = cause.ID
				victimPriority = cause.Priority
			}
		}
		if victim == "" {
			return nil, &BudgetError{
				MaxBytes:     maxBytes,
				ObservedSize: len(raw),
				MinimumBytes: len(raw),
			}
		}
		if _, exists := dropped[victim]; exists {
			// Defensive: buildCandidate should have removed it.
			return nil, &BudgetError{MaxBytes: maxBytes, ObservedSize: len(raw), MinimumBytes: len(raw)}
		}
		dropped[victim] = OmitReasonByteBudget
		droppedOrder = append(droppedOrder, victim)
	}

	plan := Plan{
		MaxBytes:        maxBytes,
		SizeBytes:       len(raw),
		FullSizeBytes:   len(fullBytes),
		UniqueCauses:    len(base.Causes),
		CauseReferences: refs,
	}
	plan.ReferencedCauses = len(useCount)
	for id, n := range useCount {
		if n > 1 {
			plan.RepeatedReferences += n - 1
			var w wireWriter
			if cause, ok := base.Cause(id); ok {
				_ = w.cause(cause)
				plan.DuplicateSavingsBytes += (n - 1) * w.Len()
			}
		}
	}
	for _, cause := range candidate.Causes {
		plan.RetainedCauseIDs = append(plan.RetainedCauseIDs, cause.ID)
	}

	dropIDs := append([]string(nil), droppedOrder...)
	sort.SliceStable(dropIDs, func(i, j int) bool {
		ci, _ := base.Cause(dropIDs[i])
		cj, _ := base.Cause(dropIDs[j])
		if ci.Priority != cj.Priority {
			return ci.Priority < cj.Priority
		}
		return dropIDs[i] < dropIDs[j]
	})
	for _, id := range dropIDs {
		cause, _ := base.Cause(id)
		plan.DroppedCauses = append(plan.DroppedCauses, DroppedCause{
			ID:       id,
			Priority: cause.Priority,
			Reason:   dropped[id],
		})
	}

	anchorSeen := map[string]struct{}{}
	for _, p := range base.Paths {
		for _, s := range p.Segments {
			if s.Anchor != nil {
				key := s.Anchor.Key()
				if _, ok := anchorSeen[key]; !ok {
					anchorSeen[key] = struct{}{}
					plan.Anchors = append(plan.Anchors, *s.Anchor)
				}
			}
		}
	}
	plan.GapIDs = append(plan.GapIDs, gapIDList(candidate.Gaps()).IDs()...)

	return &Packet{Bytes: raw, Plan: plan}, nil
}

// IDs is a small helper for gap slices used internally and in tests.
type gapIDList []Gap

func (gs gapIDList) IDs() []string {
	out := make([]string, 0, len(gs))
	for _, g := range gs {
		out = append(out, g.ID)
	}
	return out
}

func canonicalReport(r Report) *Report {
	cp := r.Clone()
	seen := make(map[string]int, len(r.Causes))
	unique := make([]Cause, 0, len(r.Causes))
	for _, c := range cp.Causes {
		if i, ok := seen[c.ID]; ok {
			if causesEqual(unique[i], c) {
				continue
			}
			// Validate reports this conflict; keep the first definition so
			// encoding still has deterministic behavior if Validate is bypassed.
			continue
		}
		seen[c.ID] = len(unique)
		unique = append(unique, c)
	}
	cp.Causes = unique
	return cp
}

func buildCandidate(base *Report, dropped map[string]string) *Report {
	out := &Report{Delivery: base.Delivery}
	catalog := make(map[string]Cause, len(base.Causes))
	for _, c := range base.Causes {
		catalog[c.ID] = c
	}
	used := make(map[string]bool)

	for pi, p := range base.Paths {
		op := Path{ID: p.ID, Label: p.Label}
		for si, s := range p.Segments {
			os := Segment{Stage: s.Stage}
			if s.Anchor != nil {
				ref := *s.Anchor
				os.Anchor = &ref
			}

			omittedSeen := make(map[string]bool)
			for _, om := range s.Omitted {
				if om.ID == "" || omittedSeen[om.ID] {
					continue
				}
				omittedSeen[om.ID] = true
				os.Omitted = append(os.Omitted, om)
			}

			for _, id := range s.CauseIDs {
				if reason, drop := dropped[id]; drop {
					if !omittedSeen[id] {
						omittedSeen[id] = true
						cause := catalog[id]
						os.Omitted = append(os.Omitted, OmittedCause{
							ID:       id,
							Priority: cause.Priority,
							Reason:   reason,
						})
					}
					continue
				}
				os.CauseIDs = append(os.CauseIDs, id)
				used[id] = true
			}

			if s.Gap != nil {
				g := *s.Gap
				if g.ID == "" {
					g.ID = gapID(p.ID, si, s.Stage)
				}
				for _, om := range os.Omitted {
					if !gapHasOmitted(g, om.ID) {
						g.Omitted = append(g.Omitted, om)
					}
				}
				if s.Gap.Anchor != nil {
					a := *s.Gap.Anchor
					g.Anchor = &a
				} else if os.Anchor != nil {
					a := *os.Anchor
					g.Anchor = &a
				}
				os.Gap = &g
			} else if len(os.CauseIDs) == 0 && os.Anchor == nil {
				kind := GapEmpty
				reason := "segment has no recoverable cause"
				if len(os.Omitted) > 0 {
					kind = GapPriority
					reason = "lower-priority causes disconnected by retention policy"
				}
				g := Gap{
					ID:      gapID(p.ID, si, s.Stage),
					Kind:    kind,
					Reason:  reason,
					Omitted: append([]OmittedCause(nil), os.Omitted...),
				}
				os.Gap = &g
			}
			op.Segments = append(op.Segments, os)
		}
		_ = pi
		out.Paths = append(out.Paths, op)
	}

	for _, c := range base.Causes {
		if used[c.ID] {
			out.Causes = append(out.Causes, c)
		}
	}
	return out
}

func gapHasOmitted(g Gap, id string) bool {
	for _, om := range g.Omitted {
		if om.ID == id {
			return true
		}
	}
	return false
}
