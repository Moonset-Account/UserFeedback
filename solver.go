package capacityreview

import (
	"errors"
	"fmt"
	"math"
)

// Solve computes the largest non-negative integer multiplier k such that every
// single-zone-failure scenario is feasible, one integral allocation per
// scenario at that multiplier, and a failure certificate for k+1.
//
// Feasibility of a fixed scenario is monotone in k: if k*base can be routed,
// so can any smaller multiplier (scale the flow down; base demands and the
// multiplier are integers, and integral flows scale). Therefore each scenario
// has a largest feasible multiplier, and the answer is the minimum over
// scenarios. Each per-scenario maximum is found by binary search with a
// max-flow feasibility test on the scenario's transportation network.
func Solve(p Problem) (Result, error) {
	if err := p.validate(); err != nil {
		return Result{}, err
	}
	m := len(p.ZoneCapacity)

	var sumBase int64
	for _, b := range p.BaseDemand {
		if sumBase > math.MaxInt64-b {
			return Result{}, errors.New("capacityreview: total base demand overflows int64")
		}
		sumBase += b
	}
	var totalCap int64
	for _, c := range p.ZoneCapacity {
		if totalCap > math.MaxInt64-c {
			return Result{}, errors.New("capacityreview: total zone capacity overflows int64")
		}
		totalCap += c
	}
	// No scenario can carry more than totalCap, so k = totalCap/sumBase + 1 is
	// infeasible for every scenario.
	upper := totalCap / sumBase
	if upper == math.MaxInt64 {
		return Result{}, errors.New("capacityreview: instance too large for int64 arithmetic")
	}

	perScenario := make([]int64, m)
	for j := 0; j < m; j++ {
		k, err := maxFeasibleForScenario(p, j, upper)
		if err != nil {
			return Result{}, err
		}
		perScenario[j] = k
	}

	maxK := perScenario[0]
	failingZone := 0
	for j := 1; j < m; j++ {
		if perScenario[j] < maxK {
			maxK = perScenario[j]
			failingZone = j
		}
	}

	next := maxK + 1
	if next <= maxK {
		return Result{}, errors.New("capacityreview: multiplier overflow")
	}

	res := Result{
		MaxMultiplier:  maxK,
		Scenarios:      make([]ScenarioResult, 0, m),
		NextMultiplier: next,
	}
	for j := 0; j < m; j++ {
		alloc, flow, required, err := evaluateScenario(p, j, maxK)
		if err != nil {
			return Result{}, err
		}
		if flow != required {
			return Result{}, fmt.Errorf("capacityreview: internal error: scenario zone %d not feasible at k=%d", j, maxK)
		}
		res.Scenarios = append(res.Scenarios, ScenarioResult{
			FailedZone: j,
			Feasible:   true,
			Allocation: alloc,
		})
	}

	cert, err := failureCertificate(p, failingZone, next)
	if err != nil {
		return Result{}, err
	}
	res.Failure = cert
	return res, nil
}

// maxFeasibleForScenario returns the largest k in [0, upper] that the scenario
// can serve. Invariant: k=0 is feasible and k=upper+1 is infeasible.
func maxFeasibleForScenario(p Problem, failedZone int, upper int64) (int64, error) {
	lo, hi := int64(0), upper
	for lo < hi {
		mid := lo + (hi-lo+1)/2
		_, flow, required, err := evaluateScenario(p, failedZone, mid)
		if err != nil {
			return 0, err
		}
		if flow == required {
			lo = mid
		} else {
			hi = mid - 1
		}
	}
	return lo, nil
}

// evaluateScenario builds the scenario network at multiplier k, runs max flow,
// and returns the allocation obtained from it, the max flow value, and the
// required total demand k*sum(base).
func evaluateScenario(p Problem, failedZone int, k int64) (Allocation, int64, int64, error) {
	sn, err := buildScenarioNetwork(p, failedZone, k)
	if err != nil {
		return Allocation{}, 0, 0, err
	}
	required, err := sn.required(p, k)
	if err != nil {
		return Allocation{}, 0, 0, err
	}
	flow := sn.g.maxFlow(0, sn.sink)
	return sn.allocation(), flow, required, nil
}

// failureCertificate witnesses infeasibility of multiplier k for failedZone.
func failureCertificate(p Problem, failedZone int, k int64) (*FailureCertificate, error) {
	sn, err := buildScenarioNetwork(p, failedZone, k)
	if err != nil {
		return nil, err
	}
	required, err := sn.required(p, k)
	if err != nil {
		return nil, err
	}
	flow := sn.g.maxFlow(0, sn.sink)
	if flow >= required {
		return nil, fmt.Errorf("capacityreview: internal error: scenario zone %d unexpectedly feasible at k=%d", failedZone, k)
	}
	cut, err := sn.cut(flow)
	if err != nil {
		return nil, err
	}
	if cut.Capacity >= required {
		return nil, errors.New("capacityreview: internal error: cut is not the bottleneck")
	}
	return &FailureCertificate{
		FailedZone: failedZone,
		Multiplier: k,
		Required:   required,
		MaxFlow:    flow,
		Cut:        cut,
	}, nil
}
