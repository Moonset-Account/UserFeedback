package causalcodec

import (
	"errors"
	"fmt"
	"strings"
)

// Priority controls which causes are retained first when a packet exceeds
// its fixed byte budget. Larger values are more important.
type Priority int

const (
	PriorityUnspecified Priority = 0
	PriorityDebug       Priority = 10
	PriorityLow         Priority = 25
	PriorityNormal      Priority = 50
	PriorityHigh        Priority = 75
	PriorityCritical    Priority = 100
)

func (p Priority) String() string {
	switch {
	case p >= PriorityCritical:
		return "critical"
	case p >= PriorityHigh:
		return "high"
	case p >= PriorityNormal:
		return "normal"
	case p >= PriorityLow:
		return "low"
	case p >= PriorityDebug:
		return "debug"
	default:
		return fmt.Sprintf("p%d", int(p))
	}
}

// Source identifies the concrete delivery source that produced an error.
type Source struct {
	Bus       string `json:"bus,omitempty"`
	Topic     string `json:"topic,omitempty"`
	Partition int32  `json:"partition,omitempty"`
	Offset    int64  `json:"offset,omitempty"`
	Consumer  string `json:"consumer,omitempty"`
	Attempt   uint32 `json:"attempt,omitempty"`
}

func (s Source) IsZero() bool {
	return s.Bus == "" && s.Topic == "" && s.Partition == 0 && s.Offset == 0 &&
		s.Consumer == "" && s.Attempt == 0
}

func (s Source) String() string {
	var b strings.Builder
	if s.Bus != "" {
		b.WriteString(s.Bus)
		b.WriteString("://")
	}
	if s.Topic != "" {
		b.WriteString(s.Topic)
	}
	if s.Partition != 0 || s.Offset != 0 {
		if b.Len() > 0 {
			b.WriteByte('/')
		}
		fmt.Fprintf(&b, "partition=%d@offset=%d", s.Partition, s.Offset)
	}
	if s.Consumer != "" {
		if b.Len() > 0 {
			b.WriteByte(' ')
		}
		b.WriteString("consumer=" + s.Consumer)
	}
	if s.Attempt > 0 {
		fmt.Fprintf(&b, " attempt=%d", s.Attempt)
	}
	return b.String()
}

// Delivery is the envelope whose source and causal event must survive encoding.
type Delivery struct {
	ID      string `json:"id"`
	EventID string `json:"event_id"`
	Source  Source `json:"source"`
}

// Cause is one error source/reason in a causal graph. The same ID may be
// referenced by many paths and stages; the codec stores its body once.
type Cause struct {
	ID        string   `json:"id"`
	Stage     string   `json:"stage,omitempty"`
	Kind      string   `json:"kind,omitempty"`
	Message   string   `json:"message,omitempty"`
	Source    Source   `json:"source,omitempty"`
	Priority  Priority `json:"priority,omitempty"`
	Retryable bool     `json:"retryable,omitempty"`
}

// AnchorRef is an exact, previously exchanged context identity. A display
// name alone is deliberately not a sufficient lookup key.
type AnchorRef struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	Digest  string `json:"digest"`
}

func (r AnchorRef) Key() string {
	return r.Name + "\x00" + r.Version + "\x00" + r.Digest
}

func (r AnchorRef) String() string {
	d := r.Digest
	if strings.HasPrefix(d, "sha256:") {
		d = strings.TrimPrefix(d, "sha256:")
	}
	if len(d) > 12 {
		d = d[:12]
	}
	return r.Name + "@" + r.Version + "#" + d
}

func (r AnchorRef) IsZero() bool {
	return r.Name == "" && r.Version == "" && r.Digest == ""
}

// GapKind explains why a chain could not be continuously recovered.
type GapKind string

const (
	// GapPriority means low-priority causes were disconnected, either by a
	// retention threshold or by byte-budget pruning.
	GapPriority GapKind = "priority_disconnect"
	// GapMissingAnchor means the required exact context anchor is unavailable.
	GapMissingAnchor GapKind = "missing_anchor"
	// GapMalformedBranch means one length-framed path could not be decoded.
	GapMalformedBranch GapKind = "malformed_branch"
	// GapEmpty means a segment had no cause or anchor.
	GapEmpty GapKind = "empty_segment"
)

// OmittedCause retains just enough identity to display or later extend a
// pruned alternative/reason without carrying its full verbose body.
type OmittedCause struct {
	ID       string   `json:"id"`
	Priority Priority `json:"priority,omitempty"`
	Reason   string   `json:"reason,omitempty"`
}

// Gap is an explicit break in a causal chain. It is never silently filled
// with a same-named local anchor.
type Gap struct {
	ID              string         `json:"id"`
	Kind            GapKind        `json:"kind"`
	Reason          string         `json:"reason,omitempty"`
	Omitted         []OmittedCause `json:"omitted,omitempty"`
	Threshold       Priority       `json:"threshold,omitempty"`
	Anchor          *AnchorRef     `json:"anchor,omitempty"`
	Extended        bool           `json:"extended,omitempty"`
	ExtensionReason string         `json:"extension_reason,omitempty"`
}

func (g Gap) OmittedIDs() []string {
	out := make([]string, 0, len(g.Omitted))
	for _, c := range g.Omitted {
		out = append(out, c.ID)
	}
	return out
}

// Segment is one stage in a Path. CauseIDs are alternatives at that stage;
// by convention the first retained ID is the primary explanation.
type Segment struct {
	Stage        string         `json:"stage"`
	CauseIDs     []string       `json:"cause_ids,omitempty"`
	Anchor       *AnchorRef     `json:"anchor,omitempty"`
	ResolvedFrom *AnchorRef     `json:"resolved_from,omitempty"`
	Omitted      []OmittedCause `json:"omitted,omitempty"`
	Gap          *Gap           `json:"gap,omitempty"`
}

// Path is one ordered causal route for the delivery's event.
type Path struct {
	ID       string    `json:"id"`
	Label    string    `json:"label,omitempty"`
	Segments []Segment `json:"segments,omitempty"`
}

// Report is the complete encoder input and decoder output.
type Report struct {
	Delivery Delivery `json:"delivery"`
	Causes   []Cause  `json:"causes,omitempty"`
	Paths    []Path   `json:"paths,omitempty"`
}

// Cause returns the catalog cause with id.
func (r *Report) Cause(id string) (Cause, bool) {
	if r == nil {
		return Cause{}, false
	}
	for i := range r.Causes {
		if r.Causes[i].ID == id {
			return r.Causes[i], true
		}
	}
	return Cause{}, false
}

// Gaps returns all gap markers in path order, including healed extension
// markers. Use OpenGaps for unresolved breaks.
func (r *Report) Gaps() []Gap {
	if r == nil {
		return nil
	}
	var out []Gap
	for _, p := range r.Paths {
		for _, s := range p.Segments {
			if s.Gap != nil {
				out = append(out, *s.Gap)
			}
		}
	}
	return out
}

// OpenGaps returns gaps that have not been healed by Extend.
func (r *Report) OpenGaps() []Gap {
	gaps := r.Gaps()
	out := gaps[:0:0]
	for _, g := range gaps {
		if !g.Extended {
			out = append(out, g)
		}
	}
	return out
}

// Clone returns a deep copy so Encode planning and Extend cannot mutate the
// caller's report.
func (r Report) Clone() *Report {
	out := r
	out.Causes = append([]Cause(nil), r.Causes...)
	out.Paths = make([]Path, len(r.Paths))
	for i, p := range r.Paths {
		out.Paths[i] = p
		out.Paths[i].Segments = make([]Segment, len(p.Segments))
		for j, s := range p.Segments {
			out.Paths[i].Segments[j] = s
			out.Paths[i].Segments[j].CauseIDs = append([]string(nil), s.CauseIDs...)
			out.Paths[i].Segments[j].Omitted = append([]OmittedCause(nil), s.Omitted...)
			if s.Anchor != nil {
				a := *s.Anchor
				out.Paths[i].Segments[j].Anchor = &a
			}
			if s.ResolvedFrom != nil {
				a := *s.ResolvedFrom
				out.Paths[i].Segments[j].ResolvedFrom = &a
			}
			if s.Gap != nil {
				g := *s.Gap
				g.Omitted = append([]OmittedCause(nil), s.Gap.Omitted...)
				if s.Gap.Anchor != nil {
					a := *s.Gap.Anchor
					g.Anchor = &a
				}
				out.Paths[i].Segments[j].Gap = &g
			}
		}
	}
	return &out
}

func gapID(pathID string, index int, stage string) string {
	return fmt.Sprintf("gap:%s:%d:%s", pathID, index, stage)
}

// Validate checks referential integrity and anchor identity fields.
func (r Report) Validate() error {
	var errs []error
	if r.Delivery.EventID == "" {
		errs = append(errs, fmt.Errorf("%w: delivery.event_id is required", ErrInvalidReport))
	}
	if len(r.Paths) == 0 {
		errs = append(errs, fmt.Errorf("%w: at least one path is required", ErrInvalidReport))
	}

	catalog := make(map[string]Cause, len(r.Causes))
	for i, c := range r.Causes {
		if c.ID == "" {
			errs = append(errs, fmt.Errorf("%w: causes[%d].id is required", ErrInvalidReport, i))
			continue
		}
		if existing, ok := catalog[c.ID]; ok && !causesEqual(existing, c) {
			errs = append(errs, fmt.Errorf("%w: conflicting definitions for cause %q", ErrInvalidReport, c.ID))
			continue
		}
		catalog[c.ID] = c
	}

	pathIDs := make(map[string]struct{}, len(r.Paths))
	for pi, p := range r.Paths {
		if p.ID == "" {
			errs = append(errs, fmt.Errorf("%w: paths[%d].id is required", ErrInvalidReport, pi))
		} else if _, ok := pathIDs[p.ID]; ok {
			errs = append(errs, fmt.Errorf("%w: duplicate path id %q", ErrInvalidReport, p.ID))
		}
		pathIDs[p.ID] = struct{}{}

		for si, s := range p.Segments {
			if s.Anchor != nil {
				if err := validateAnchorRef(*s.Anchor); err != nil {
					errs = append(errs, fmt.Errorf("path %q segment %d: %w", p.ID, si, err))
				}
				if len(s.CauseIDs) != 0 {
					errs = append(errs, fmt.Errorf("%w: path %q segment %d cannot carry both anchor and inline causes", ErrInvalidReport, p.ID, si))
				}
				continue
			}
			if len(s.CauseIDs) == 0 && s.Gap == nil {
				errs = append(errs, fmt.Errorf("%w: path %q segment %d has no cause ids or anchor", ErrInvalidReport, p.ID, si))
			}
			for _, id := range s.CauseIDs {
				if id == "" {
					errs = append(errs, fmt.Errorf("%w: path %q segment %d contains an empty cause id", ErrInvalidReport, p.ID, si))
					continue
				}
				if _, ok := catalog[id]; !ok {
					errs = append(errs, fmt.Errorf("%w: path %q segment %d references unknown cause %q", ErrInvalidReport, p.ID, si, id))
				}
			}
		}
	}
	return errors.Join(errs...)
}

func validateAnchorRef(ref AnchorRef) error {
	if ref.Name == "" || ref.Version == "" {
		return fmt.Errorf("%w: anchor name and version are required", ErrInvalidAnchorRef)
	}
	if ref.Digest == "" {
		return fmt.Errorf("%w: anchor %s must include an exact digest", ErrInvalidAnchorRef, ref)
	}
	return nil
}

func causesEqual(a, b Cause) bool {
	// Priority is part of the definition; duplicate references to the same ID
	// must not disagree.
	return a.ID == b.ID && a.Stage == b.Stage && a.Kind == b.Kind &&
		a.Message == b.Message && a.Source == b.Source &&
		a.Priority == b.Priority && a.Retryable == b.Retryable
}
