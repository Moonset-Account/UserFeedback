package capacityreview_test

import (
	"fmt"

	"capacityreview"
)

// ExampleSolve shows the review for a site where two entries share one small
// backup zone. Spare capacity in the other zones does not help a displaced
// entry that cannot reach them.
func ExampleSolve() {
	p := capacityreview.Problem{
		BaseDemand:   []int64{1, 1},
		ZoneCapacity: []int64{100, 100, 1},
		TransferLimit: [][]int64{
			{10, 0, 10},
			{0, 10, 10},
		},
	}
	res, err := capacityreview.Solve(p)
	if err != nil {
		panic(err)
	}
	fmt.Printf("max multiplier: %d\n", res.MaxMultiplier)
	for _, sc := range res.Scenarios {
		fmt.Printf("zone %d fails: entry totals %v, zone loads %v\n",
			sc.FailedZone, sc.Allocation.EntryTotal, sc.Allocation.ZoneLoad)
	}
	f := res.Failure
	fmt.Printf("at multiplier %d zone %d needs %d, max flow %d, cut capacity %d\n",
		f.Multiplier, f.FailedZone, f.Required, f.MaxFlow, f.Cut.Capacity)
	for _, e := range f.Cut.Edges {
		fmt.Printf("  cut: %v -> %v capacity %d\n", e.From, e.To, e.Capacity)
	}
	// Output:
	// max multiplier: 1
	// zone 0 fails: entry totals [1 1], zone loads [0 1 1]
	// zone 1 fails: entry totals [1 1], zone loads [1 0 1]
	// zone 2 fails: entry totals [1 1], zone loads [1 1 0]
	// at multiplier 2 zone 0 needs 4, max flow 3, cut capacity 3
	//   cut: source -> entry[1] capacity 2
	//   cut: zone[2] -> sink capacity 1
}
