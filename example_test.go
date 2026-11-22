package forwarder

import (
	"fmt"
	"sort"
)

// Example demonstrates the required event sequence: a weight reduction that
// has not converged, followed by a hard failure of another node, with the
// bucket map printed after every round and the exact set of reroutes.
func Example_downweightThenFailure() {
	r, err := New(
		Config{Buckets: 12, MaxMovesPerRound: 1},
		[]Node{
			{ID: "alpha", Weight: 6, Health: Ready},
			{ID: "bravo", Weight: 3, Health: Ready},
			{ID: "charlie", Weight: 3, Health: Ready},
		},
	)
	if err != nil {
		panic(err)
	}

	// One connection on each bucket owned by charlie, plus a few on alpha.
	used := map[string]bool{}
	connectAt := func(bucket int) *Conn {
		for i := 0; i < 100000; i++ {
			id := fmt.Sprintf("conn-%05d", i)
			if used[id] || r.BucketFor(id) != bucket {
				continue
			}
			used[id] = true
			c, err := r.Connect(id)
			if err != nil {
				panic(err)
			}
			return c
		}
		panic("no id found")
	}
	connectAt(0)
	connectAt(5)
	charlieConns := []*Conn{connectAt(9), connectAt(10), connectAt(11)}

	fmt.Println("bootstrap:", summarise(r.Snapshot().Buckets))

	_ = r.SetWeight("alpha", 1)
	r1 := r.Rebalance()
	fmt.Printf("round %d moves=%v buckets=%s deviation=%d\n",
		r1.Number, r1.Moves, summarise(r1.Buckets), r1.Deviation)

	// charlie hard-fails while alpha's downweight is still in flight.
	_ = r.SetHealth("charlie", Failed)
	r2 := r.Rebalance()
	ids := []string{charlieConns[0].ID(), charlieConns[1].ID(), charlieConns[2].ID()}
	sort.Strings(ids)
	fmt.Printf("round %d moves=%v buckets=%s deviation=%d\n",
		r2.Number, r2.Moves, summarise(r2.Buckets), r2.Deviation)
	fmt.Printf("rerouted=%d stranded=0 from=charlie to=%s bucket=%d\n",
		len(r2.Rerouted), r2.Rerouted[0].To, r2.Rerouted[0].Bucket)
	for _, id := range ids {
		live, _ := r.Conn(id)
		fmt.Printf("%s now on %s\n", id, live.Target())
	}

	for i := 0; i < 4; i++ {
		rnd := r.Rebalance()
		fmt.Printf("round %d moves=%v buckets=%s deviation=%d\n",
			rnd.Number, rnd.Moves, summarise(rnd.Buckets), rnd.Deviation)
	}

	// Closing a superseded handle for a rerouted connection must not change
	// the new target's connection count.
	bravoConns := func() int {
		for _, s := range r.Snapshot().Nodes {
			if s.ID == "bravo" {
				return s.Connections
			}
		}
		return -1
	}
	fmt.Println("bravo connections:", bravoConns())
	charlieConns[0].Close() // stale handle: no effect on bravo
	fmt.Println("bravo after stale close:", bravoConns())
	live, _ := r.Conn(ids[0])
	live.Close()
	fmt.Println("bravo after live close:", bravoConns())

	// Output:
	// bootstrap: [alpha alpha alpha alpha alpha alpha bravo bravo bravo charlie charlie charlie]
	// round 1 moves=[{0 alpha bravo}] buckets=[bravo alpha alpha alpha alpha alpha bravo bravo bravo charlie charlie charlie] deviation=6
	// round 2 moves=[{1 alpha bravo}] buckets=[bravo bravo alpha alpha alpha alpha bravo bravo bravo charlie charlie charlie] deviation=8
	// rerouted=3 stranded=0 from=charlie to=bravo bucket=0
	// conn-00000 now on bravo
	// conn-00007 now on bravo
	// conn-00016 now on bravo
	// round 3 moves=[{2 alpha bravo}] buckets=[bravo bravo bravo alpha alpha alpha bravo bravo bravo charlie charlie charlie] deviation=6
	// round 4 moves=[{9 charlie bravo}] buckets=[bravo bravo bravo alpha alpha alpha bravo bravo bravo bravo charlie charlie] deviation=4
	// round 5 moves=[{10 charlie bravo}] buckets=[bravo bravo bravo alpha alpha alpha bravo bravo bravo bravo bravo charlie] deviation=2
	// round 6 moves=[{11 charlie bravo}] buckets=[bravo bravo bravo alpha alpha alpha bravo bravo bravo bravo bravo bravo] deviation=0
	// bravo connections: 3
	// bravo after stale close: 3
	// bravo after live close: 2
}

func summarise(buckets []string) string {
	out := "["
	for i, b := range buckets {
		if i > 0 {
			out += " "
		}
		out += b
	}
	return out + "]"
}
