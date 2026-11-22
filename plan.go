package errfeedback

import (
	"sort"
)

// LayoutID identifies a bucket layout. A layout is the partition of logical
// coordinates into buckets. Reusing a LayoutID for a different partition is
// rejected so that stale packets can never be mistaken for a new layout.
type LayoutID uint64

// PlanID identifies a concrete plan revision supplied with one step.
type PlanID uint64

// Bucket is one communication group: a named set of logical coordinates.
type Bucket struct {
	ID     BucketID
	Coords []Coord
}

// StepPlan is the bucket layout used for exactly one step.
//
// Buckets partition a set of logical coordinates. The sender's coordinate set
// must be covered exactly, i.e. every sender coordinate appears in exactly one
// bucket and the plan contains no foreign coordinates.
type StepPlan struct {
	ID      PlanID
	Layout  LayoutID
	Buckets []Bucket
}

// NewStepPlan validates and copies a plan definition.
func NewStepPlan(planID PlanID, layout LayoutID, buckets ...Bucket) (StepPlan, error) {
	cp := make([]Bucket, len(buckets))
	for i, b := range buckets {
		cp[i] = Bucket{ID: b.ID, Coords: append([]Coord(nil), b.Coords...)}
		sort.Slice(cp[i].Coords, func(a, b int) bool { return cp[i].Coords[a] < cp[i].Coords[b] })
	}
	p := StepPlan{ID: planID, Layout: layout, Buckets: cp}
	if err := p.Validate(); err != nil {
		return StepPlan{}, err
	}
	return p, nil
}

// Validate reports whether the plan is a well-formed partition.
func (p StepPlan) Validate() error {
	if len(p.Buckets) == 0 {
		return ErrInvalidPlan
	}
	seenBuckets := make(map[BucketID]struct{}, len(p.Buckets))
	seenCoords := make(map[Coord]struct{})
	for _, b := range p.Buckets {
		if _, dup := seenBuckets[b.ID]; dup {
			return ErrInvalidPlan
		}
		seenBuckets[b.ID] = struct{}{}
		if len(b.Coords) == 0 {
			return ErrInvalidPlan
		}
		for _, c := range b.Coords {
			if _, dup := seenCoords[c]; dup {
				return ErrInvalidPlan
			}
			seenCoords[c] = struct{}{}
		}
	}
	return nil
}

// Coords returns all coordinates of the plan in ascending order.
func (p StepPlan) Coords() []Coord {
	out := make([]Coord, 0)
	for _, b := range p.Buckets {
		out = append(out, b.Coords...)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// BucketOf reports the bucket that owns coord.
func (p StepPlan) BucketOf(coord Coord) (BucketID, bool) {
	for _, b := range p.Buckets {
		for _, c := range b.Coords {
			if c == coord {
				return b.ID, true
			}
		}
	}
	return 0, false
}

// BucketByID returns a copy of the bucket with the given ID.
func (p StepPlan) BucketByID(id BucketID) (Bucket, bool) {
	for _, b := range p.Buckets {
		if b.ID == id {
			return Bucket{ID: b.ID, Coords: append([]Coord(nil), b.Coords...)}, true
		}
	}
	return Bucket{}, false
}

func (p StepPlan) clone() StepPlan {
	cp := StepPlan{ID: p.ID, Layout: p.Layout, Buckets: make([]Bucket, len(p.Buckets))}
	for i, b := range p.Buckets {
		cp.Buckets[i] = Bucket{ID: b.ID, Coords: append([]Coord(nil), b.Coords...)}
	}
	return cp
}

// coversExactly reports whether the plan assigns each coordinate in coords to
// exactly one bucket and contains no other coordinate.
func (p StepPlan) coversExactly(coords []Coord) bool {
	want := make(map[Coord]struct{}, len(coords))
	for _, c := range coords {
		want[c] = struct{}{}
	}
	got := make(map[Coord]struct{}, len(coords))
	for _, b := range p.Buckets {
		for _, c := range b.Coords {
			if _, ok := want[c]; !ok {
				return false
			}
			if _, dup := got[c]; dup {
				return false
			}
			got[c] = struct{}{}
		}
	}
	return len(got) == len(want)
}

// fingerprint is a stable hash of the layout content so that a LayoutID reused
// for a different partition can be detected.
func (p StepPlan) fingerprint() uint64 {
	const offset64 = 1469598103934665603
	const prime64 = 1099511628211
	var h uint64 = offset64
	mix := func(v uint64) {
		for i := 0; i < 8; i++ {
			h ^= v & 0xff
			h *= prime64
			v >>= 8
		}
	}
	buckets := append([]Bucket(nil), p.Buckets...)
	sort.Slice(buckets, func(i, j int) bool { return buckets[i].ID < buckets[j].ID })
	for _, b := range buckets {
		mix(uint64(int64(b.ID)))
		mix(uint64(len(b.Coords)))
		coords := append([]Coord(nil), b.Coords...)
		sort.Slice(coords, func(i, j int) bool { return coords[i] < coords[j] })
		for _, c := range coords {
			mix(uint64(int64(c)))
		}
	}
	return h
}
