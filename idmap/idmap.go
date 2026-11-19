// Package idmap provides a read-only verifier that decides whether a
// requested range of user (or group) identifiers can be mapped, through
// several layers of Linux-style namespace translation, onto identifiers
// inside an approved host-account range.
//
// The verifier never walks individual identifiers: its work is proportional
// to the number of mapping segments and the number of split points, so a
// request spanning a billion identifiers costs the same as one spanning ten.
package idmap

import (
	"errors"
	"sort"
)

// MaxID is the largest valid identifier. The identifier domain is the Linux
// uid_t/gid_t domain, the unsigned 32-bit integers [0, MaxID].
const MaxID uint64 = (1 << 32) - 1

// Sentinel errors returned for invalid verifier configurations and requests.
// Callers may distinguish them with errors.Is.
var (
	// ErrOverlappingSegments reports two segments on one layer whose source
	// ranges overlap, or whose translated destination ranges overlap.
	ErrOverlappingSegments = errors.New("idmap: layer contains overlapping source or destination identifier ranges")

	// ErrInvalidRange reports a segment or range that is empty or reaches
	// outside the uint32 identifier domain.
	ErrInvalidRange = errors.New("idmap: range is empty or leaves the uint32 identifier domain")
)

// Segment describes one translation segment of a namespace layer, following
// the Linux uid_map convention: identifiers in the half-open source range
// [SrcStart, SrcStart+Length) translate to the half-open destination range
// [DstStart, DstStart+Length) by a constant offset.
type Segment struct {
	SrcStart uint64
	DstStart uint64
	Length   uint64
}

func (s Segment) srcEnd() uint64 { return s.SrcStart + s.Length }
func (s Segment) dstEnd() uint64 { return s.DstStart + s.Length }

// Layer is one namespace level. Segments translate identifiers expressed in
// this layer's coordinates into the parent layer's coordinates. A layer with
// no segments maps nothing: every identifier hits a hole and loses any
// identity it may have inherited from higher layers.
type Layer struct {
	Name     string
	Segments []Segment
}

// Config is the immutable description of a namespace stack. Layers are
// ordered from the layer closest to the request (the innermost namespace)
// toward the host; ApprovedRange is expressed in final host coordinates.
type Config struct {
	Layers        []Layer
	ApprovedRange Range
}

// Range is a half-open interval of identifiers [Start, End).
type Range struct {
	Start uint64
	End   uint64
}

// Valid reports whether r is a non-empty interval inside the identifier
// domain (End may equal MaxID+1, the exclusive upper bound of the domain).
func (r Range) Valid() bool {
	return r.Start < r.End && r.End <= MaxID+1
}

// Result holds the partition of a verified request. Every sub-range is
// expressed in the request's original coordinates. The three slices are
// pairwise disjoint, individually sorted and coalesced, and their union is
// exactly the requested interval.
type Result struct {
	// Confirmed holds request pieces that reach the host inside the
	// approved host-account range.
	Confirmed []Range
	// OutOfBounds holds request pieces that reach the host but outside the
	// approved host-account range.
	OutOfBounds []Range
	// MissingMapping holds request pieces that fall into a layer hole and
	// never reach the parent/host; a hole does not inherit the identity the
	// piece had in the layer above.
	MissingMapping []Range
}

// Verifier is the read-only verification entry point. It owns a normalized
// copy of its configuration: the slices passed to NewVerifier are never
// retained, and Verify never mutates any of its inputs. A Verifier is safe
// for concurrent Verify calls.
type Verifier struct {
	layers   []Layer
	approved Range
}

// NewVerifier validates and normalizes a configuration.
//
// It returns ErrInvalidRange for zero-length segments, segments whose source
// or destination range leaves the uint32 identifier domain, or an invalid
// approved range. It returns ErrOverlappingSegments when a layer's source
// segments overlap (merely touching is allowed) or when the translated
// destination ranges of two segments overlap.
func NewVerifier(cfg Config) (*Verifier, error) {
	if !cfg.ApprovedRange.Valid() {
		return nil, ErrInvalidRange
	}

	layers := make([]Layer, len(cfg.Layers))
	for i, layer := range cfg.Layers {
		segs := make([]Segment, len(layer.Segments))
		copy(segs, layer.Segments)

		for _, s := range segs {
			if s.Length == 0 || s.srcEnd() > MaxID+1 || s.dstEnd() > MaxID+1 {
				return nil, ErrInvalidRange
			}
		}

		sort.Slice(segs, func(a, b int) bool { return segs[a].SrcStart < segs[b].SrcStart })
		for j := 1; j < len(segs); j++ {
			if segs[j].SrcStart < segs[j-1].srcEnd() {
				return nil, ErrOverlappingSegments
			}
		}

		// Destination ranges must be pairwise disjoint too. Check on a
		// separately sorted copy; the layer keeps the source-sorted order.
		dsts := make([]Segment, len(segs))
		copy(dsts, segs)
		sort.Slice(dsts, func(a, b int) bool { return dsts[a].DstStart < dsts[b].DstStart })
		for j := 1; j < len(dsts); j++ {
			if dsts[j].DstStart < dsts[j-1].dstEnd() {
				return nil, ErrOverlappingSegments
			}
		}

		layers[i] = Layer{Name: layer.Name, Segments: segs}
	}

	return &Verifier{layers: layers, approved: cfg.ApprovedRange}, nil
}

// fragment tracks a piece of the request while it travels through the stack.
// cur is the piece expressed in the current layer's coordinates; orig is the
// same piece expressed in the request's original coordinates.
type fragment struct {
	cur  Range
	orig Range
}

// Verify partitions request into confirmed, out-of-bounds and
// missing-mapping sub-ranges, all reported in the request's original
// coordinates. An invalid request returns ErrInvalidRange.
//
// Cost is proportional to the number of mapping segments and split points,
// never to the width of the identifier ranges.
func (v *Verifier) Verify(request Range) (Result, error) {
	var res Result
	if !request.Valid() {
		return res, ErrInvalidRange
	}

	active := []fragment{{cur: request, orig: request}}
	var missing []Range

	for _, layer := range v.layers {
		segs := layer.Segments
		var next []fragment

		for _, f := range active {
			// First segment whose source end is strictly past the fragment
			// start; segments ending exactly at the start are half-open and
			// therefore do not cover it.
			idx := sort.Search(len(segs), func(i int) bool {
				return segs[i].srcEnd() > f.cur.Start
			})
			pos := f.cur.Start

			for pos < f.cur.End {
				if idx == len(segs) || segs[idx].SrcStart >= f.cur.End {
					// The rest of the fragment lies in a layer hole and
					// reaches neither parent nor host.
					missing = append(missing, Range{
						Start: f.orig.Start + (pos - f.cur.Start),
						End:   f.orig.End,
					})
					break
				}

				if segs[idx].SrcStart > pos {
					// Gap before the next segment: a hole with no inherited
					// identity from the layer above.
					holeEnd := minUint64(segs[idx].SrcStart, f.cur.End)
					missing = append(missing, Range{
						Start: f.orig.Start + (pos - f.cur.Start),
						End:   f.orig.Start + (holeEnd - f.cur.Start),
					})
					pos = holeEnd
					if pos >= f.cur.End {
						break
					}
				}

				coveredEnd := minUint64(segs[idx].srcEnd(), f.cur.End)
				next = append(next, fragment{
					cur: Range{
						Start: translateID(pos, segs[idx].SrcStart, segs[idx].DstStart),
						End:   translateID(coveredEnd, segs[idx].SrcStart, segs[idx].DstStart),
					},
					orig: Range{
						Start: f.orig.Start + (pos - f.cur.Start),
						End:   f.orig.Start + (coveredEnd - f.cur.Start),
					},
				})
				pos = coveredEnd
				idx++
			}
		}

		active = next
	}

	var confirmed, outOfBounds []Range
	for _, f := range active {
		switch {
		case f.cur.Start >= v.approved.Start && f.cur.End <= v.approved.End:
			confirmed = append(confirmed, f.orig)
		case f.cur.End <= v.approved.Start || f.cur.Start >= v.approved.End:
			outOfBounds = append(outOfBounds, f.orig)
		default:
			// The fragment straddles one or both approved-range boundaries.
			// Split in current coordinates and translate the cuts back to
			// original coordinates (length is preserved by translation).
			if f.cur.Start < v.approved.Start && f.cur.End > v.approved.Start {
				firstCut := f.orig.Start + (v.approved.Start - f.cur.Start)
				outOfBounds = append(outOfBounds, Range{Start: f.orig.Start, End: firstCut})
				if f.cur.End > v.approved.End {
					// The fragment spans the whole approved range.
					secondCut := f.orig.Start + (v.approved.End - f.cur.Start)
					confirmed = append(confirmed, Range{Start: firstCut, End: secondCut})
					outOfBounds = append(outOfBounds, Range{Start: secondCut, End: f.orig.End})
				} else {
					confirmed = append(confirmed, Range{Start: firstCut, End: f.orig.End})
				}
			} else {
				// Starts inside the approved range and extends past its end.
				cut := f.orig.Start + (v.approved.End - f.cur.Start)
				confirmed = append(confirmed, Range{Start: f.orig.Start, End: cut})
				outOfBounds = append(outOfBounds, Range{Start: cut, End: f.orig.End})
			}
		}
	}

	res.Confirmed = coalesce(confirmed)
	res.OutOfBounds = coalesce(outOfBounds)
	res.MissingMapping = coalesce(missing)
	return res, nil
}

// translateID maps one identifier through a segment with an arbitrary
// constant offset, including negative ones. Segment validation guarantees the
// result stays inside the uint32 domain, so the signed arithmetic is exact.
func translateID(id, srcStart, dstStart uint64) uint64 {
	return uint64(int64(id) + int64(dstStart) - int64(srcStart))
}

func minUint64(a, b uint64) uint64 {
	if a < b {
		return a
	}
	return b
}

// coalesce sorts ranges by Start and merges adjacent or overlapping ranges of
// the same class into maximal intervals. It returns nil for an empty input so
// empty result buckets are distinguishable only by length, never by content.
func coalesce(rs []Range) []Range {
	if len(rs) == 0 {
		return nil
	}
	sort.Slice(rs, func(i, j int) bool { return rs[i].Start < rs[j].Start })

	merged := make([]Range, 0, len(rs))
	cur := rs[0]
	for _, r := range rs[1:] {
		if r.Start <= cur.End {
			if r.End > cur.End {
				cur.End = r.End
			}
		} else {
			merged = append(merged, cur)
			cur = r
		}
	}
	return append(merged, cur)
}
