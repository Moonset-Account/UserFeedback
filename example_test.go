package incview_test

import (
	"fmt"

	"incview"
)

// Example exercises the public API end to end: it builds two rooms, then
// performs the "move a device and delete its minimum duration" transaction
// and prints the deltas a reader receives. Output is asserted by go test.
func Example() {
	v := incview.NewView()

	tx := v.Begin()
	_ = tx.SetDevice("dev-a", "east")
	_ = tx.SetDevice("dev-b", "east")
	_ = tx.SetDevice("dev-c", "west")
	_ = tx.PutFact(1, "dev-a", 4)
	_ = tx.PutFact(2, "dev-a", 9)
	_ = tx.PutFact(3, "dev-b", 4)
	_ = tx.PutFact(4, "dev-c", 7)
	tx.Commit()

	for _, g := range v.Groups() {
		fmt.Printf("%s count=%d total=%d min=%d\n", g.Room, g.Count, g.Total, g.Min)
	}

	res := v.Begin()
	_ = res.SetDevice("dev-a", "west")
	_ = res.DeleteFact(1) // dev-a's 4; dev-b still holds 4 in east
	out := res.Commit()

	for _, d := range out.Deltas {
		fmt.Printf("delta %s before=%v after=%v\n", d.Room, d.Before, d.After)
	}
	fmt.Printf("visited=%v\n", out.VisitedRows)

	// Output:
	// east count=3 total=17 min=4
	// west count=1 total=7 min=7
	// delta east before={east 3 17 4} after={east 1 4 4}
	// delta west before={west 1 7 7} after={west 2 16 7}
	// visited=[1 2]
}
