package negotiation

// box is an exact Cartesian product of one contiguous protocol range,
// source address interval, destination address interval, and port interval.
type box struct {
	protocol ProtocolRange
	source   AddressRange
	dest     AddressRange
	ports    PortRange
}

func (b box) intersect(other box) (box, bool) {
	proto, ok := intersectProtocolRange(b.protocol, other.protocol)
	if !ok {
		return box{}, false
	}
	source, ok := intersectAddressRange(b.source, other.source)
	if !ok {
		return box{}, false
	}
	dest, ok := intersectAddressRange(b.dest, other.dest)
	if !ok {
		return box{}, false
	}
	ports, ok := intersectPortRange(b.ports, other.ports)
	if !ok {
		return box{}, false
	}
	return box{protocol: proto, source: source, dest: dest, ports: ports}, true
}

// subtract returns b\cut as disjoint boxes. The decomposition peels off
// non-intersecting slabs one dimension at a time.
func (b box) subtract(cut box) []box {
	if _, ok := b.intersect(cut); !ok {
		return []box{b}
	}

	current := b
	var out []box

	// Source address slabs.
	if current.source.Start.Compare(cut.source.Start) < 0 {
		piece := current
		piece.source.End = cut.source.Start.Prev()
		out = append(out, piece)
		current.source.Start = cut.source.Start
	}
	if current.source.End.Compare(cut.source.End) > 0 {
		piece := current
		piece.source.Start = cut.source.End.Next()
		out = append(out, piece)
		current.source.End = cut.source.End
	}

	// Destination address slabs. Pieces already have the clipped source
	// overlap, which is sufficient because source-outside slabs were emitted.
	if current.dest.Start.Compare(cut.dest.Start) < 0 {
		piece := current
		piece.dest.End = cut.dest.Start.Prev()
		out = append(out, piece)
		current.dest.Start = cut.dest.Start
	}
	if current.dest.End.Compare(cut.dest.End) > 0 {
		piece := current
		piece.dest.Start = cut.dest.End.Next()
		out = append(out, piece)
		current.dest.End = cut.dest.End
	}

	// Protocol slabs.
	if current.protocol.Start < cut.protocol.Start {
		piece := current
		piece.protocol.End = cut.protocol.Start - 1
		out = append(out, piece)
		current.protocol.Start = cut.protocol.Start
	}
	if current.protocol.End > cut.protocol.End {
		piece := current
		piece.protocol.Start = cut.protocol.End + 1
		out = append(out, piece)
		current.protocol.End = cut.protocol.End
	}

	// Destination port slabs.
	if current.ports.Start < cut.ports.Start {
		piece := current
		piece.ports.End = cut.ports.Start - 1
		out = append(out, piece)
		current.ports.Start = cut.ports.Start
	}
	if current.ports.End > cut.ports.End {
		piece := current
		piece.ports.Start = cut.ports.End + 1
		out = append(out, piece)
		current.ports.End = cut.ports.End
	}

	// current is now the common intersection and is removed.
	return out
}

// disjointizeBoxes returns the same union as in, but with pairwise-disjoint
// boxes. Earlier boxes are retained; later boxes are trimmed around them.
func disjointizeBoxes(in []box) []box {
	var kept []box
	for _, candidate := range in {
		fragments := []box{candidate}
		for _, owner := range kept {
			if len(fragments) == 0 {
				break
			}
			var next []box
			for _, fragment := range fragments {
				next = append(next, fragment.subtract(owner)...)
			}
			fragments = next
		}
		kept = append(kept, fragments...)
	}
	return kept
}

func intersectBoxes(left, right []box) []box {
	var intersections []box
	for _, a := range left {
		for _, b := range right {
			if got, ok := a.intersect(b); ok {
				intersections = append(intersections, got)
			}
		}
	}
	return disjointizeBoxes(intersections)
}

func subtractBoxes(source, cuts []box) []box {
	var residual []box
	for _, original := range source {
		fragments := []box{original}
		for _, cut := range cuts {
			if len(fragments) == 0 {
				break
			}
			var next []box
			for _, fragment := range fragments {
				next = append(next, fragment.subtract(cut)...)
			}
			fragments = next
		}
		residual = append(residual, fragments...)
	}
	return disjointizeBoxes(residual)
}

func intersectProtocolRange(a, b ProtocolRange) (ProtocolRange, bool) {
	start := a.Start
	if b.Start > start {
		start = b.Start
	}
	end := a.End
	if b.End < end {
		end = b.End
	}
	if start > end {
		return ProtocolRange{}, false
	}
	return ProtocolRange{Start: start, End: end}, true
}

func intersectPortRange(a, b PortRange) (PortRange, bool) {
	start := a.Start
	if b.Start > start {
		start = b.Start
	}
	end := a.End
	if b.End < end {
		end = b.End
	}
	if start > end {
		return PortRange{}, false
	}
	return PortRange{Start: start, End: end}, true
}
