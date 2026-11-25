// Package mirror implements the distribution half of a boundary-switch traffic
// mirror.
//
// One Capture describes a single mirrored frame: its original ingress port, the
// original VLAN tags observed at that ingress, the frame bytes, and the byte
// range of every protocol header inside the frame. A Config lists the mirror
// receivers. Each Receiver declares the original ingresses and tags it may
// observe plus the deepest protocol header it may retain.
//
// Distribution is deliberately metadata-driven. The distributor never parses
// VLAN tags out of Frame, because intermediate links may rewrite them. It
// authorizes each receiver against Capture.Ingress and Capture.VLANs, which are
// the values seen at the original ingress.
//
// For an authorized receiver the distributor emits a byte prefix consisting only
// of headers whose boundaries are complete, contiguous and known to be inside
// the frame, up to Receiver.MaxDepth. If a header boundary is missing,
// inconsistent or deeper than the receiver limit, the copy stops at the last
// confirmed header and Copy.Truncation records the reason.
package mirror

import (
	"errors"
	"fmt"
	"strings"
)

// Depth is the ordered protocol depth used by a receiver's retention policy.
// A receiver only retains headers whose Depth is less than or equal to its
// MaxDepth.
//
// 802.1Q and QinQ tags are link-layer headers and therefore belong to DepthL2;
// the tag values themselves are a separate authorization input carried by
// Capture.VLANs.
type Depth int

const (
	DepthL2 Depth = 1 // link layer, for example Ethernet, 802.1Q, QinQ
	DepthL3 Depth = 2 // network layer, for example IPv4 or IPv6
	DepthL4 Depth = 3 // transport layer, for example TCP or UDP
	DepthL7 Depth = 4 // application payload
)

// String returns the short layer name used in diagnostics.
func (d Depth) String() string {
	switch d {
	case DepthL2:
		return "L2"
	case DepthL3:
		return "L3"
	case DepthL4:
		return "L4"
	case DepthL7:
		return "L7"
	default:
		return fmt.Sprintf("Depth(%d)", int(d))
	}
}

// Valid reports whether d is one of the defined protocol depths.
func (d Depth) Valid() bool { return d >= DepthL2 && d <= DepthL7 }

// FieldRange describes one protocol header inside Capture.Frame.
//
// Start is inclusive and End is exclusive, so the header occupies
// Frame[Start:End]. A negative Start or End means that boundary information is
// not available for that header.
type FieldRange struct {
	Name  string
	Depth Depth
	Start int
	End   int
}

// HasBounds reports whether both boundary offsets were supplied.
func (f FieldRange) HasBounds() bool { return f.Start >= 0 && f.End >= 0 }

// Capture is one mirrored frame together with its original forwarding identity.
//
// Ingress and VLANs are recorded at the original ingress port. They remain the
// authorization input even when Frame contains tags rewritten by an
// intermediate link.
//
// Fields must describe headers from outermost to innermost. When every field
// has a non-negative Start the distributor sorts them by Start before walking
// them, so callers may also supply them out of order.
type Capture struct {
	ID      string
	Ingress string
	VLANs   []int
	Frame   []byte
	Fields  []FieldRange
}

// Receiver describes one mirror destination and the scope it may observe.
type Receiver struct {
	Name string

	// Ingresses lists the original ingress ports this receiver may observe.
	// The single entry "*" matches any original ingress.
	Ingresses []string

	// VLANs lists the original VLAN tags this receiver may observe. Every tag
	// carried by a frame must be listed here; a frame with any out-of-scope tag
	// is denied as a whole. An empty list therefore denies all tagged frames.
	VLANs []int

	// AllowUntagged permits frames that carry no original VLAN tag. It is false
	// by default so a VLAN-scoped receiver does not silently receive untagged
	// traffic.
	AllowUntagged bool

	// MaxDepth is the deepest protocol header that may be retained.
	MaxDepth Depth
}

// Config lists the mirror receivers.
type Config struct {
	Receivers []Receiver
}

// Validate checks configuration invariants. Distribute is best-effort even when
// the configuration is invalid, but callers should validate first.
func (c Config) Validate() error {
	if len(c.Receivers) == 0 {
		return errors.New("mirror: configuration has no receivers")
	}
	seen := make(map[string]struct{}, len(c.Receivers))
	for i, r := range c.Receivers {
		name := strings.TrimSpace(r.Name)
		if name == "" {
			return fmt.Errorf("mirror: receiver %d has an empty name", i)
		}
		if _, ok := seen[name]; ok {
			return fmt.Errorf("mirror: receiver name %q is duplicated", name)
		}
		seen[name] = struct{}{}
		if !r.MaxDepth.Valid() {
			return fmt.Errorf("mirror: receiver %q has invalid max depth %d", name, int(r.MaxDepth))
		}
		if len(r.Ingresses) == 0 {
			return fmt.Errorf("mirror: receiver %q lists no permitted ingress", name)
		}
		for _, ingress := range r.Ingresses {
			if strings.TrimSpace(ingress) == "" {
				return fmt.Errorf("mirror: receiver %q has an empty ingress", name)
			}
		}
	}
	return nil
}

// DenyReason explains why a receiver did not receive a capture.
type DenyReason string

const (
	DenyNone     DenyReason = ""
	DenyIngress  DenyReason = "ingress_not_allowed"
	DenyVLAN     DenyReason = "vlan_not_allowed"
	DenyUntagged DenyReason = "untagged_not_allowed"
)

// Reason explains why a byte copy stopped before the end of the frame.
type Reason string

const (
	// ReasonDepthLimit means the next header is deeper than Receiver.MaxDepth.
	ReasonDepthLimit Reason = "depth_limit"
	// ReasonMissingBounds means a needed header has a negative Start or End.
	ReasonMissingBounds Reason = "missing_bounds"
	// ReasonUnknownDepth means a header carries a Depth outside the defined set.
	ReasonUnknownDepth Reason = "unknown_depth"
	// ReasonInvalidBounds means End is not greater than Start.
	ReasonInvalidBounds Reason = "invalid_bounds"
	// ReasonOutOfBounds means a header claims bytes past the end of the frame.
	ReasonOutOfBounds Reason = "out_of_bounds"
	// ReasonGap means a header starts after the confirmed prefix, leaving
	// undocumented bytes in between.
	ReasonGap Reason = "gap"
	// ReasonOverlap means a header starts inside the confirmed prefix.
	ReasonOverlap Reason = "overlap"
	// ReasonNoFieldData means the frame has bytes but no header metadata at all.
	ReasonNoFieldData Reason = "no_field_data"
	// ReasonUncoveredBytes means the frame has trailing bytes that no header
	// range accounts for, so they cannot be confirmed compliant.
	ReasonUncoveredBytes Reason = "uncovered_bytes"
)

// Truncation records where and why a copy stopped.
type Truncation struct {
	Reason Reason
	// Field is the header that could not be retained. It is empty when the stop
	// is caused by trailing bytes or by a total absence of field data.
	Field string
	// Depth is the depth of Field, zero when Field is empty.
	Depth Depth
	// Offset is the length of the retained prefix, equal to len(Copy.Data).
	Offset int
	// Detail is a human-readable explanation for diagnostics.
	Detail string
}

// Copy is the result for one receiver.
type Copy struct {
	Receiver string
	// Allowed reports whether authorization passed.
	Allowed bool
	// Denied is DenyNone when Allowed is true.
	Denied DenyReason
	// DenyDetail explains a denial and names the offending ingress or tag.
	DenyDetail string
	// Data is the retained byte prefix. It is always a private copy, so one
	// receiver cannot observe or mutate another receiver's bytes.
	Data []byte
	// Fields names the confirmed headers retained, outermost first.
	Fields []string
	// Truncation is nil only when the complete frame was retained.
	Truncation *Truncation
}

// Result is the distribution outcome for one capture.
type Result struct {
	CaptureID string
	Copies    []Copy
}

// CopyFor returns the copy produced for the named receiver.
func (r Result) CopyFor(name string) (Copy, bool) {
	for _, c := range r.Copies {
		if c.Receiver == name {
			return c, true
		}
	}
	return Copy{}, false
}

// Distribute authorizes and truncates one capture for every configured
// receiver. Denied receivers still appear in the result so that callers can
// audit why nothing was delivered.
//
// Distribute never panics on malformed capture metadata. A malformed or
// incomplete header range only stops that receiver's copy at the last confirmed
// header, with the cause recorded in Copy.Truncation.
func Distribute(cfg Config, capture Capture) Result {
	result := Result{CaptureID: capture.ID}
	for _, receiver := range cfg.Receivers {
		result.Copies = append(result.Copies, distributeOne(receiver, capture))
	}
	return result
}

// DistributeAll applies Distribute to every capture, preserving input order.
//
// Each capture is handled independently: the retained byte prefixes never alias
// the source frames, and a receiver's copy of one capture never shares storage
// with its copy of another capture. Results are therefore safe to hand to
// different consumers or goroutines.
func DistributeAll(cfg Config, captures []Capture) []Result {
	results := make([]Result, 0, len(captures))
	for _, capture := range captures {
		results = append(results, Distribute(cfg, capture))
	}
	return results
}

func distributeOne(receiver Receiver, capture Capture) Copy {
	copyOut := Copy{Receiver: receiver.Name}
	if reason, detail := denyReason(receiver, capture); reason != DenyNone {
		copyOut.Denied = reason
		copyOut.DenyDetail = detail
		return copyOut
	}

	copyOut.Allowed = true
	cut, fields, truncation := truncate(capture.Frame, capture.Fields, receiver.MaxDepth)
	copyOut.Data = make([]byte, cut)
	copy(copyOut.Data, capture.Frame[:cut])
	copyOut.Fields = fields
	copyOut.Truncation = truncation
	return copyOut
}

func denyReason(receiver Receiver, capture Capture) (DenyReason, string) {
	if !ingressAllowed(receiver.Ingresses, capture.Ingress) {
		return DenyIngress, fmt.Sprintf("original ingress %q is not in the receiver scope", capture.Ingress)
	}

	if len(capture.VLANs) == 0 {
		if !receiver.AllowUntagged {
			return DenyUntagged, "frame has no original VLAN tag and the receiver does not allow untagged frames"
		}
		return DenyNone, ""
	}

	allowed := make(map[int]struct{}, len(receiver.VLANs))
	for _, vlan := range receiver.VLANs {
		allowed[vlan] = struct{}{}
	}
	for _, vlan := range capture.VLANs {
		if _, ok := allowed[vlan]; !ok {
			return DenyVLAN, fmt.Sprintf("original VLAN tag %d is not in the receiver scope", vlan)
		}
	}
	return DenyNone, ""
}

func ingressAllowed(allowed []string, ingress string) bool {
	for _, candidate := range allowed {
		if candidate == "*" || candidate == ingress {
			return true
		}
	}
	return false
}
