package rebalancer

import "math/big"

// All load comparisons in this package use exact rational numbers; floating
// point is never used for objective comparisons.

func intRat(n int64) *big.Rat { return big.NewRat(n, 1) }

// loadFrac returns load / capacity as an exact fraction.
func loadFrac(load, capacity int) *big.Rat {
	return new(big.Rat).SetFrac(big.NewInt(int64(load)), big.NewInt(int64(capacity)))
}

// maxLoad computes max_c load[c]/capacity[c] as an exact fraction.
func maxLoad(loads []int, capacities []int) *big.Rat {
	m := intRat(0)
	for c := range loads {
		if loads[c] == 0 {
			continue
		}
		r := loadFrac(loads[c], capacities[c])
		if r.Cmp(m) > 0 {
			m = r
		}
	}
	return m
}
