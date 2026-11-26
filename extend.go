package causalcodec

import (
	"fmt"
)

// Extension attaches a recovered causal tail to a gap produced by priority
// pruning or a later out-of-band context transfer.
type Extension struct {
	GapID    string
	Note     string
	Causes   []Cause
	Segments []Segment
}

// Extend returns a new report with ext.Segments inserted after the named gap.
// The gap is retained as a healed connector so display output can show the
// before/after relationship.
func (r *Report) Extend(ext Extension) (*Report, error) {
	if r == nil {
		return nil, fmt.Errorf("%w: nil report", ErrGapNotFound)
	}
	if ext.GapID == "" {
		return nil, fmt.Errorf("%w: extension gap id is required", ErrGapNotFound)
	}

	pi, _, gap := findGap(r, ext.GapID)
	if gap == nil {
		return nil, fmt.Errorf("%w: %q", ErrGapNotFound, ext.GapID)
	}
	if gap.Extended {
		return nil, fmt.Errorf("%w: %q is already extended; extend a new gap in the attached tail", ErrGapNotFound, ext.GapID)
	}
	if len(ext.Segments) == 0 {
		return nil, fmt.Errorf("%w: extension for %q requires at least one segment", ErrInvalidReport, ext.GapID)
	}

	combined := make(map[string]Cause, len(r.Causes)+len(ext.Causes))
	for _, c := range r.Causes {
		combined[c.ID] = c
	}
	for _, c := range ext.Causes {
		if c.ID == "" {
			return nil, fmt.Errorf("%w: extension cause id is required", ErrInvalidReport)
		}
		if old, ok := combined[c.ID]; ok && !causesEqual(old, c) {
			return nil, fmt.Errorf("%w: conflicting extension cause %q", ErrInvalidReport, c.ID)
		}
		combined[c.ID] = c
	}
	for i, s := range ext.Segments {
		if s.Anchor != nil {
			if err := validateAnchorRef(*s.Anchor); err != nil {
				return nil, fmt.Errorf("extension segment %d: %w", i, err)
			}
		}
		for _, id := range s.CauseIDs {
			if _, ok := combined[id]; !ok {
				return nil, fmt.Errorf("%w: extension segment %d references unknown cause %q", ErrInvalidReport, i, id)
			}
		}
	}

	cp := r.Clone()
	// Re-find in the clone and mutate its gap marker.
	_, csi, _ := findGap(cp, ext.GapID)
	g := cp.Paths[pi].Segments[csi].Gap
	g.Extended = true
	if ext.Note != "" {
		g.ExtensionReason = ext.Note
	} else {
		g.ExtensionReason = "causal tail attached by Extend"
	}

	for _, c := range ext.Causes {
		if _, ok := cp.Cause(c.ID); !ok {
			cp.Causes = append(cp.Causes, c)
		}
	}

	insert := make([]Segment, 0, len(ext.Segments))
	for _, s := range ext.Segments {
		ns := Segment{Stage: s.Stage}
		ns.CauseIDs = append([]string(nil), s.CauseIDs...)
		ns.Omitted = append([]OmittedCause(nil), s.Omitted...)
		if s.Anchor != nil {
			a := *s.Anchor
			ns.Anchor = &a
		}
		if s.ResolvedFrom != nil {
			a := *s.ResolvedFrom
			ns.ResolvedFrom = &a
		}
		if s.Gap != nil {
			ng := *s.Gap
			ng.Omitted = append([]OmittedCause(nil), s.Gap.Omitted...)
			if s.Gap.Anchor != nil {
				a := *s.Gap.Anchor
				ng.Anchor = &a
			}
			ns.Gap = &ng
		}
		insert = append(insert, ns)
	}

	old := cp.Paths[pi].Segments
	tail := append([]Segment(nil), old[csi+1:]...)
	head := append([]Segment(nil), old[:csi+1]...)
	cp.Paths[pi].Segments = append(append(head, insert...), tail...)
	return cp, nil
}

func findGap(r *Report, gapID string) (pathIndex int, segmentIndex int, gap *Gap) {
	for pi, p := range r.Paths {
		for si, s := range p.Segments {
			if s.Gap != nil && s.Gap.ID == gapID {
				return pi, si, s.Gap
			}
		}
	}
	return -1, -1, nil
}
