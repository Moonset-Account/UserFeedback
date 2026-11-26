package causalcodec

import (
	"fmt"
	"strings"
)

// DisplayOptions controls the deterministic plain-text causal display.
type DisplayOptions struct {
	ShowIDs     bool
	ShowOmitted bool
	ShowSources bool
}

// DefaultDisplayOptions shows enough identity to correlate distributed events.
func DefaultDisplayOptions() DisplayOptions {
	return DisplayOptions{ShowIDs: true, ShowOmitted: true, ShowSources: true}
}

// Display renders r with DefaultDisplayOptions.
func Display(r *Report) string {
	return DefaultDisplayOptions().Display(r)
}

// Display renders a stable tree-like summary. It never invents causes for
// gaps or missing anchors.
func (o DisplayOptions) Display(r *Report) string {
	if r == nil {
		return "<nil causal report>\n"
	}
	var b strings.Builder
	fmt.Fprintf(&b, "Delivery %s · event %s\n", dash(r.Delivery.ID), dash(r.Delivery.EventID))
	if r.Delivery.Source.IsZero() {
		b.WriteString("Source: -\n")
	} else {
		fmt.Fprintf(&b, "Source: %s\n", r.Delivery.Source.String())
	}

	for _, p := range r.Paths {
		label := p.Label
		if label == "" {
			label = "-"
		}
		fmt.Fprintf(&b, "\nPath %s — %s\n", dash(p.ID), label)
		if len(p.Segments) == 0 {
			b.WriteString("  (no segments)\n")
			continue
		}
		for _, s := range p.Segments {
			o.displaySegment(&b, r, s)
		}
	}

	if gaps := r.OpenGaps(); len(gaps) > 0 {
		fmt.Fprintf(&b, "\nOpen causal gaps: %d\n", len(gaps))
		for _, g := range gaps {
			fmt.Fprintf(&b, "  - %s [%s]\n", g.ID, g.Kind)
		}
	}
	return b.String()
}

func (o DisplayOptions) displaySegment(b *strings.Builder, r *Report, s Segment) {
	// A healed gap remains as a connector, followed by extension segments.
	if s.Gap != nil && s.Gap.Extended && len(s.CauseIDs) == 0 {
		fmt.Fprintf(b, "  [%s] [EXTENDED] %s\n", dash(s.Stage), s.Gap.ExtensionReason)
		if o.ShowIDs {
			fmt.Fprintf(b, "      gap=%s\n", s.Gap.ID)
		}
		return
	}

	if len(s.CauseIDs) == 0 && s.Gap != nil {
		fmt.Fprintf(b, "  [%s] [CHAIN BREAK/%s] %s\n", dash(s.Stage), s.Gap.Kind, s.Gap.Reason)
		if o.ShowIDs {
			fmt.Fprintf(b, "      gap=%s\n", s.Gap.ID)
		}
		if s.Gap.Anchor != nil {
			fmt.Fprintf(b, "      anchor required=%s\n", s.Gap.Anchor.String())
		}
		if o.ShowOmitted && len(s.Gap.Omitted) > 0 {
			for _, om := range s.Gap.Omitted {
				id := om.ID
				if !o.ShowIDs {
					id = maskID(id)
				}
				fmt.Fprintf(b, "      omitted %s (%s): %s\n", id, om.Priority, om.Reason)
			}
		}
		return
	}

	for i, id := range s.CauseIDs {
		c, ok := r.Cause(id)
		role := "primary"
		prefix := "  ●"
		if i > 0 {
			role = "alternative"
			prefix = "      ↳"
		}
		if !ok {
			fmt.Fprintf(b, "%s [%s] unresolved cause id=%s\n", prefix, role, id)
			continue
		}
		title := c.Kind
		if title == "" {
			title = "cause"
		}
		fmt.Fprintf(b, "%s [%s/%s] %s", prefix, role, c.Priority, title)
		if c.Message != "" {
			fmt.Fprintf(b, ": %s", c.Message)
		}
		b.WriteByte('\n')
		if o.ShowIDs {
			fmt.Fprintf(b, "      id=%s stage=%s\n", c.ID, dash(c.Stage))
		}
		if o.ShowSources && !c.Source.IsZero() {
			fmt.Fprintf(b, "      source=%s\n", c.Source.String())
		}
		if c.Retryable {
			b.WriteString("      retryable=true\n")
		}
		if i == 0 && s.ResolvedFrom != nil {
			fmt.Fprintf(b, "      anchor resolved=%s\n", s.ResolvedFrom.String())
		}
	}

	if o.ShowOmitted {
		for _, om := range s.Omitted {
			// If the segment is a gap, its omitted list was already printed.
			if s.Gap != nil && !s.Gap.Extended && len(s.CauseIDs) == 0 {
				continue
			}
			id := om.ID
			if !o.ShowIDs {
				id = maskID(id)
			}
			fmt.Fprintf(b, "      · pruned %s [%s] %s\n", id, om.Priority, om.Reason)
		}
	}

	if s.Gap != nil && !s.Gap.Extended && len(s.CauseIDs) > 0 {
		fmt.Fprintf(b, "      [CHAIN BREAK] gap=%s kind=%s\n", s.Gap.ID, s.Gap.Kind)
	}
}

func dash(v string) string {
	if v == "" {
		return "-"
	}
	return v
}

func maskID(id string) string {
	if id == "" {
		return "-"
	}
	if len(id) <= 4 {
		return "****"
	}
	return id[:2] + "…" + id[len(id)-2:]
}
