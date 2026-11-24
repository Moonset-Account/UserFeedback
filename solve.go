package egress

import (
	"fmt"
	"sort"
)

type stateKey struct {
	fi   int
	frem int
	ni   int
	nrem int
	t    int
}

type dpResult struct {
	fastOT       int
	normalOT     int
	overhead     int
	finishSum    int
	committedMet bool
	steps        []Fragment
}

type solver struct {
	cfg       Config
	fast      []Frame
	normal    []Frame
	memo      map[stateKey]*dpResult
	nodes     int
	nodeLimit int
	maxFast   int
	maxNormal int
	online    bool
	commit    map[string]bool
}

// Schedule finds the lexicographically best complete schedule:
//  1. maximum on-time fast frames,
//  2. then maximum on-time normal frames,
//  3. then minimum total fragmentation overhead.
//
// Ties use the smaller sum of completion ticks and then a deterministic
// lexicographic fragment sequence. Late frames remain in FIFO order and are
// transmitted to completion.
func Schedule(cfg Config) (*Plan, error) {
	frames, err := validateConfig(cfg)
	if err != nil {
		return nil, err
	}
	var fast, normal []Frame
	for _, f := range frames {
		switch f.Class {
		case Fast:
			fast = append(fast, f)
		case Normal:
			normal = append(normal, f)
		}
	}

	s := &solver{
		cfg:       cfg,
		fast:      fast,
		normal:    normal,
		memo:      map[stateKey]*dpResult{},
		nodeLimit: cfg.MaxStateNodes,
	}
	if s.nodeLimit <= 0 {
		s.nodeLimit = 200000
	}
	if err := s.precheck(); err != nil {
		return nil, err
	}

	key := stateKey{t: cfg.StartAt}
	if len(fast) > 0 {
		key.frem = fast[0].Length
	} else {
		key.fi = 0
	}
	if len(normal) > 0 {
		key.nrem = normal[0].Length
	}

	res, err := s.best(key)
	if err != nil {
		return nil, err
	}
	return s.buildPlan(res.steps), nil
}

func (s *solver) precheck() error {
	oh := s.cfg.Overhead
	for _, f := range append(append([]Frame{}, s.fast...), s.normal...) {
		capacity := s.cfg.Table.MaxPayload(f.Class, oh)
		if f.Length <= capacity {
			continue
		}
		if capacity < 1 {
			return &UnsendableError{FrameID: f.ID, Class: f.Class,
				Reason: fmt.Sprintf("no window leaves room for one data byte after overhead/guard (max payload %d)", capacity)}
		}
		if capacity < s.cfg.MinNonLast {
			return &UnsendableError{FrameID: f.ID, Class: f.Class,
				Reason: fmt.Sprintf("frame does not fit whole and no window fits the minimum non-last payload %d (max payload %d)", s.cfg.MinNonLast, capacity)}
		}
	}
	return nil
}

func (s *solver) frameFor(class Class, idx int) *Frame {
	if class == Fast {
		if idx >= 0 && idx < len(s.fast) {
			return &s.fast[idx]
		}
	} else {
		if idx >= 0 && idx < len(s.normal) {
			return &s.normal[idx]
		}
	}
	return nil
}

func (s *solver) best(k stateKey) (*dpResult, error) {
	if r, ok := s.memo[k]; ok {
		return r, nil
	}
	s.nodes++
	if s.nodes > s.nodeLimit {
		return nil, &StateLimitError{Limit: s.nodeLimit}
	}

	doneFast := k.fi >= len(s.fast)
	doneNormal := k.ni >= len(s.normal)
	if doneFast && doneNormal {
		r := &dpResult{committedMet: true}
		s.memo[k] = r
		return r, nil
	}

	var best *dpResult
	consider := func(class Class) error {
		idx, rem := k.fi, k.frem
		if class == Normal {
			idx, rem = k.ni, k.nrem
		}
		f := s.frameFor(class, idx)
		if f == nil || rem <= 0 {
			return nil
		}
		low := k.t
		if f.Arrival > low {
			low = f.Arrival
		}
		capacity := s.cfg.Table.MaxPayload(class, s.cfg.Overhead)

		var sizes []int
		if rem <= capacity {
			sizes = append(sizes, rem) // final fragment
		}
		upper := rem - 1
		if capacity < upper {
			upper = capacity
		}
		for x := s.cfg.MinNonLast; x <= upper; x++ {
			sizes = append(sizes, x)
		}

		for _, x := range sizes {
			start, ok := s.cfg.Table.EarliestFit(low, x, s.cfg.Overhead, class)
			if !ok {
				continue
			}
			end := start + s.cfg.Overhead + x
			final := x == rem
			edge := Fragment{
				FrameID:  f.ID,
				Class:    class,
				Start:    start,
				Overhead: s.cfg.Overhead,
				Data:     x,
				End:      end,
				Bytes:    s.cfg.Overhead + x,
				Final:    final,
			}
			nk := k
			nk.t = end
			if class == Fast {
				if final {
					nk.fi++
					if nk.fi < len(s.fast) {
						nk.frem = s.fast[nk.fi].Length
					} else {
						nk.frem = 0
					}
				} else {
					nk.frem -= x
				}
			} else {
				if final {
					nk.ni++
					if nk.ni < len(s.normal) {
						nk.nrem = s.normal[nk.ni].Length
					} else {
						nk.nrem = 0
					}
				} else {
					nk.nrem -= x
				}
			}

			child, err := s.best(nk)
			if err != nil {
				return err
			}
			cand := &dpResult{
				fastOT:       child.fastOT,
				normalOT:     child.normalOT,
				overhead:     s.cfg.Overhead + child.overhead,
				finishSum:    child.finishSum,
				committedMet: child.committedMet,
				steps:        make([]Fragment, 0, 1+len(child.steps)),
			}
			if final && end <= f.Deadline {
				if class == Fast {
					cand.fastOT++
				} else {
					cand.normalOT++
				}
			}
			if final && s.commit != nil && s.commit[f.ID] && end > f.Deadline {
				cand.committedMet = false
			}
			if final {
				cand.finishSum += end
			}
			cand.steps = append(cand.steps, edge)
			cand.steps = append(cand.steps, child.steps...)
			if best == nil || s.better(cand, best) {
				best = cand
			}
		}
		return nil
	}

	// Fast first only fixes candidate enumeration order; comparison is global.
	if err := consider(Fast); err != nil {
		return nil, err
	}
	if err := consider(Normal); err != nil {
		return nil, err
	}
	if best == nil {
		// Precheck should make this unreachable for periodic tables, but keep a
		// explicit error rather than returning an incomplete plan.
		return nil, fmt.Errorf("no legal fragment from state %+v", k)
	}
	s.memo[k] = best
	return best, nil
}

func (s *solver) better(a, b *dpResult) bool {
	if s.online {
		if a.committedMet != b.committedMet {
			return a.committedMet
		}
		if a.normalOT != b.normalOT {
			return a.normalOT > b.normalOT
		}
	}
	return a.betterThan(b)
}

func (a *dpResult) betterThan(b *dpResult) bool {
	if a.fastOT != b.fastOT {
		return a.fastOT > b.fastOT
	}
	if a.normalOT != b.normalOT {
		return a.normalOT > b.normalOT
	}
	if a.overhead != b.overhead {
		return a.overhead < b.overhead
	}
	if a.finishSum != b.finishSum {
		return a.finishSum < b.finishSum
	}
	return lexFragments(a.steps, b.steps) < 0
}

func lexFragments(a, b []Fragment) int {
	n := len(a)
	if len(b) < n {
		n = len(b)
	}
	for i := 0; i < n; i++ {
		switch {
		case a[i].Start != b[i].Start:
			if a[i].Start < b[i].Start {
				return -1
			}
			return 1
		case a[i].Class != b[i].Class:
			if a[i].Class < b[i].Class {
				return -1
			}
			return 1
		case a[i].FrameID != b[i].FrameID:
			if a[i].FrameID < b[i].FrameID {
				return -1
			}
			return 1
		case a[i].Data != b[i].Data:
			if a[i].Data < b[i].Data {
				return -1
			}
			return 1
		case a[i].End != b[i].End:
			if a[i].End < b[i].End {
				return -1
			}
			return 1
		}
	}
	switch {
	case len(a) < len(b):
		return -1
	case len(a) > len(b):
		return 1
	default:
		return 0
	}
}

func (s *solver) buildPlan(steps []Fragment) *Plan {
	ordered := make([]Fragment, len(steps))
	copy(ordered, steps)
	counts := map[string]int{}
	for i := range ordered {
		counts[ordered[i].FrameID]++
		ordered[i].Index = counts[ordered[i].FrameID]
	}

	byID := map[string]FrameResult{}
	orig := s.cfg.Frames
	for _, f := range orig {
		byID[f.ID] = FrameResult{
			ID:       f.ID,
			Class:    f.Class,
			Arrival:  f.Arrival,
			Length:   f.Length,
			Deadline: f.Deadline,
		}
	}
	for _, fr := range ordered {
		r := byID[fr.FrameID]
		r.FragmentCount++
		r.DataBytes += fr.Data
		r.OverheadBytes += fr.Overhead
		r.OccupiedBytes += fr.Bytes
		if fr.Final {
			r.Completion = fr.End
			r.OnTime = fr.End <= r.Deadline
		}
		byID[fr.FrameID] = r
	}

	results := make([]FrameResult, 0, len(orig))
	fastOT, normalOT := 0, 0
	dataBytes, overheadBytes, occupied := 0, 0, 0
	makespan := s.cfg.StartAt
	for _, f := range orig {
		r := byID[f.ID]
		results = append(results, r)
		if r.Class == Fast && r.OnTime {
			fastOT++
		}
		if r.Class == Normal && r.OnTime {
			normalOT++
		}
		dataBytes += r.DataBytes
		overheadBytes += r.OverheadBytes
		occupied += r.OccupiedBytes
		if r.Completion > makespan {
			makespan = r.Completion
		}
	}
	// Stable frame order in the slice as well as the map.
	sort.SliceStable(results, func(i, j int) bool { return results[i].Arrival < results[j].Arrival })

	return &Plan{
		Fragments:     ordered,
		Frames:        results,
		FrameByID:     byID,
		FastOnTime:    fastOT,
		NormalOnTime:  normalOT,
		OverheadBytes: overheadBytes,
		DataBytes:     dataBytes,
		OccupiedBytes: occupied,
		Makespan:      makespan,
	}
}

// CompletionTick is a small accessor used by callers that do not want to hold
// the full FrameResult.
func (p *Plan) CompletionTick(id string) (int, bool) {
	r, ok := p.FrameByID[id]
	return r.Completion, ok
}

// IsOnTime reports whether id met its deadline.
func (p *Plan) IsOnTime(id string) bool {
	r, ok := p.FrameByID[id]
	return ok && r.OnTime
}
