package admit

import (
	"fmt"
	"sort"
)

const maxRequests = 8
const maxWorlds = 1024

type bits [16]uint64

func (b bits) has(i int) bool {
	return i >= 0 && i < maxWorlds && b[i>>6]&(uint64(1)<<uint(i&63)) != 0
}

func (b bits) with(i int) bits {
	b[i>>6] |= uint64(1) << uint(i&63)
	return b
}

func (b bits) each(fn func(int)) {
	for w, x := range b {
		for x != 0 {
			shift := trailingZeros64(x)
			i := w*64 + shift
			if i >= maxWorlds {
				return
			}
			fn(i)
			x &= x - 1
		}
	}
}

func trailingZeros64(x uint64) int {
	// bits.TrailingZeros64 is trivial, but keeping this wrapper makes the
	// iteration block easy to read.
	n := 0
	for x&1 == 0 {
		n++
		x >>= 1
	}
	return n
}

func allBits(n int) bits {
	var b bits
	for i := 0; i < n; i++ {
		b = b.with(i)
	}
	return b
}

type compiledCancellation struct {
	req int
	at  int
}

type compiledScenario struct {
	name         string
	continueMask uint8
	cancels      []compiledCancellation
	shadow       bool
	original     int
}

type engine struct {
	problem       Problem
	requests      []Request
	indexByID     map[string]int
	scenarios     []compiledScenario
	strictBarrier bool
	admissionTime int
	memo          map[memoKey]*solveResult
}

// Validate checks structural input constraints. Infeasible deadlines are not
// validation errors; they produce rejection witnesses.
func Validate(p Problem) error {
	if len(p.Requests) > maxRequests {
		return fmt.Errorf("admit: at most %d requests are supported, got %d", maxRequests, len(p.Requests))
	}
	ids := map[string]bool{}
	shapes := map[string]bool{}
	for i, r := range p.Requests {
		if r.ID == "" {
			return fmt.Errorf("request[%d]: empty id", i)
		}
		if ids[r.ID] {
			return fmt.Errorf("duplicate request id %q", r.ID)
		}
		ids[r.ID] = true
		if r.Shape == "" {
			return fmt.Errorf("request %q: empty shape", r.ID)
		}
		shapes[r.Shape] = true
		if r.Release < 0 {
			return fmt.Errorf("request %q: negative release %d", r.ID, r.Release)
		}
		if r.Deadline < 0 {
			return fmt.Errorf("request %q: negative deadline %d", r.ID, r.Deadline)
		}
		if r.Profit < 0 {
			return fmt.Errorf("request %q: negative profit %d", r.ID, r.Profit)
		}
	}
	for k, d := range p.Durations {
		if k.Stage != Stage1 && k.Stage != Stage2 {
			return fmt.Errorf("duration %+v: unsupported stage", k)
		}
		if k.Size < 1 || k.Size > maxRequests {
			return fmt.Errorf("duration %+v: batch size out of range", k)
		}
		if d <= 0 {
			return fmt.Errorf("duration %+v: duration must be positive", k)
		}
	}
	scenarios := p.Scenarios
	if len(scenarios) == 0 {
		// The conservative default keeps every request through stage two.
		all := map[string]bool{}
		for _, r := range p.Requests {
			all[r.ID] = true
		}
		scenarios = []Scenario{{Name: "default-all-continue", Continue: all}}
	}
	if len(scenarios) > 256 {
		return fmt.Errorf("admit: at most 256 public scenarios are supported")
	}
	names := map[string]bool{}
	for si, s := range scenarios {
		name := s.Name
		if name == "" {
			name = fmt.Sprintf("scenario-%d", si)
		}
		if names[name] {
			return fmt.Errorf("duplicate scenario name %q", name)
		}
		names[name] = true
		cancelIDs := map[string]bool{}
		for id, ok := range s.Continue {
			if !ok {
				continue
			}
			if !ids[id] {
				return fmt.Errorf("scenario %q: continue references unknown request %q", name, id)
			}
		}
		for _, c := range s.Cancellations {
			if !ids[c.RequestID] {
				return fmt.Errorf("scenario %q: cancellation references unknown request %q", name, c.RequestID)
			}
			if cancelIDs[c.RequestID] {
				return fmt.Errorf("scenario %q: duplicate cancellation for request %q", name, c.RequestID)
			}
			cancelIDs[c.RequestID] = true
			if c.RevealAt < 0 {
				return fmt.Errorf("scenario %q: negative reveal time for %q", name, c.RequestID)
			}
		}
	}
	// Every request needs a legal singleton stage-one duration.
	for _, r := range p.Requests {
		if _, ok := lookupDuration(p.Durations, Stage1, r.Shape, 1); !ok {
			return fmt.Errorf("request %q: missing stage-1 singleton duration for shape %q", r.ID, r.Shape)
		}
	}
	return nil
}

func newEngine(p Problem, opts Options) (*engine, error) {
	if err := Validate(p); err != nil {
		return nil, err
	}
	idx := map[string]int{}
	for i, r := range p.Requests {
		idx[r.ID] = i
	}
	e := &engine{
		problem:       p,
		requests:      p.Requests,
		indexByID:     idx,
		strictBarrier: !opts.DisableCancellationBarrier,
		admissionTime: opts.AdmissionTime,
		memo:          map[memoKey]*solveResult{},
	}
	if e.admissionTime < 0 {
		e.admissionTime = 0
	}
	for si, s := range p.Scenarios {
		name := s.Name
		if name == "" {
			name = fmt.Sprintf("scenario-%d", si)
		}
		cs := compiledScenario{name: name, original: si}
		for id, on := range s.Continue {
			if on {
				cs.continueMask |= uint8(1) << uint(idx[id])
			}
		}
		for _, c := range s.Cancellations {
			ci := idx[c.RequestID]
			cs.cancels = append(cs.cancels, compiledCancellation{req: ci, at: c.RevealAt})
		}
		sort.Slice(cs.cancels, func(i, j int) bool {
			if cs.cancels[i].at != cs.cancels[j].at {
				return cs.cancels[i].at < cs.cancels[j].at
			}
			return cs.cancels[i].req < cs.cancels[j].req
		})
		e.scenarios = append(e.scenarios, cs)
	}
	if len(p.Scenarios) == 0 {
		// Validate's default was local; compile the same conservative world.
		mask := uint8((1 << len(p.Requests)) - 1)
		e.scenarios = []compiledScenario{{name: "default-all-continue", continueMask: mask, original: 0}}
	}
	if e.strictBarrier {
		origLen := len(e.scenarios)
		for i := 0; i < origLen; i++ {
			base := e.scenarios[i]
			var futureTimes []int
			seenTime := map[int]bool{}
			for _, c := range base.cancels {
				if c.at > e.admissionTime && !seenTime[c.at] {
					seenTime[c.at] = true
					futureTimes = append(futureTimes, c.at)
				}
			}
			if len(futureTimes) == 0 {
				continue
			}
			lastTime := futureTimes[len(futureTimes)-1]

			addPrefixShadow := func(cutoff int, label string) {
				sh := base
				sh.shadow = true
				sh.original = i
				sh.name = fmt.Sprintf("shadow:cancellation-prefix-%s:%s", label, base.name)
				var filtered []compiledCancellation
				for _, c := range base.cancels {
					if c.at <= cutoff {
						filtered = append(filtered, c)
					}
				}
				sh.cancels = filtered
				e.scenarios = append(e.scenarios, sh)
			}

			// Before the first future boundary, prepare as if no future
			// cancellation exists.
			addPrefixShadow(e.admissionTime, fmt.Sprintf("le-%d", e.admissionTime))
			// After an intermediate boundary, retain what was revealed but
			// remain robust to the next hidden event not occurring. The final
			// prefix is the original world itself.
			for _, t := range futureTimes {
				if t < lastTime {
					addPrefixShadow(t, fmt.Sprintf("le-%d", t))
				}
			}
		}
	}
	if len(e.scenarios) > maxWorlds {
		return nil, fmt.Errorf("admit: epistemic scenario count %d exceeds %d", len(e.scenarios), maxWorlds)
	}
	return e, nil
}

func lookupDuration(m map[BatchCost]int, stage Stage, shape string, size int) (int, bool) {
	if d, ok := m[BatchCost{Stage: stage, Shape: shape, Size: size}]; ok {
		return d, true
	}
	// A stage/batch-size table may be shared across shape families.
	for _, wildcard := range []string{"*", ""} {
		if shape == wildcard {
			continue
		}
		if d, ok := m[BatchCost{Stage: stage, Shape: wildcard, Size: size}]; ok {
			return d, true
		}
	}
	return 0, false
}

func (e *engine) duration(stage Stage, shape string, size int) (int, bool) {
	return lookupDuration(e.problem.Durations, stage, shape, size)
}

func (e *engine) idMask(ids []string) (uint8, error) {
	var m uint8
	for _, id := range ids {
		i, ok := e.indexByID[id]
		if !ok {
			return 0, fmt.Errorf("unknown request id %q", id)
		}
		m |= uint8(1) << uint(i)
	}
	return m, nil
}

func maskIDs(mask uint8, reqs []Request) []string {
	var out []string
	for i := 0; i < len(reqs); i++ {
		if mask&(uint8(1)<<uint(i)) != 0 {
			out = append(out, reqs[i].ID)
		}
	}
	return out
}
