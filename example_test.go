package rcu_test

import (
	"fmt"

	rcu "rcugrace"
)

// Example drives the requested scenario — a reader that migrates, another
// thread that has already crossed its grace period, and an old thread that is
// still nested inside its section — and prints, per callback, when it may take
// effect and which threads it is still waiting on.
func Example() {
	c := rcu.New()
	events := []rcu.Event{
		// "quick" reads alone and fully crosses its grace period first.
		rcu.EnterRead("quick"),
		rcu.RegisterCallback("reclaim-quick", "alloc"),
		rcu.ExitRead("quick"),
		// "mover" begins reading, then migrates while still inside.
		rcu.Migrate("mover", 0),
		rcu.EnterRead("mover"),
		// An old thread nests three deep.
		rcu.EnterRead("old"),
		rcu.EnterRead("old"),
		rcu.EnterRead("old"),
		// A callback registered now waits on mover and old, not on quick.
		rcu.RegisterCallback("reclaim-main", "alloc"),
		rcu.Migrate("mover", 2),
		rcu.CPUOffline(0),
		rcu.CPUQuiescent(0),
	}

	for _, e := range events {
		oc, err := c.Apply(e)
		if err != nil {
			panic(err)
		}
		for _, id := range oc.BecameEffective {
			r, _ := c.CallbackReport(id)
			fmt.Printf("seq %2d %-18s -> %s can take effect; waiting on %v\n",
				oc.Seq, e.String(), r.ID, r.Waiters)
		}
	}

	fmt.Println("-- after migration and CPU announcements --")
	for _, r := range c.Reports() {
		state := "pending"
		if r.Effective() {
			state = fmt.Sprintf("effective@%d", r.EffectiveAt)
		}
		fmt.Printf("%-15s %-9s waiters=%v\n", r.ID, state, r.Waiters)
	}

	// The mover leaves its section (it was a waiter too), then the old thread
	// unwinds its nesting; only its outermost exit lets reclaim-main run.
	print := func(e rcu.Event) {
		oc, err := c.Apply(e)
		if err != nil {
			panic(err)
		}
		for _, id := range oc.BecameEffective {
			r, _ := c.CallbackReport(id)
			fmt.Printf("seq %2d %-18s -> %s can take effect; waiting on %v\n",
				oc.Seq, e.String(), r.ID, r.Waiters)
		}
	}
	print(rcu.ExitRead("mover"))
	print(rcu.ExitRead("old")) // still nested
	print(rcu.ExitRead("old")) // still nested
	print(rcu.ExitRead("old")) // outermost: releases
	fmt.Println("-- final --")
	for _, r := range c.Reports() {
		state := "pending"
		if r.Effective() {
			state = fmt.Sprintf("effective@%d", r.EffectiveAt)
		}
		fmt.Printf("%-15s %-13s waiters=%v\n", r.ID, state, r.Waiters)
	}

	// Output:
	// seq  3 exit_read(quick)   -> reclaim-quick can take effect; waiting on []
	// -- after migration and CPU announcements --
	// reclaim-quick   effective@3 waiters=[]
	// reclaim-main    pending   waiters=[mover old]
	// seq 16 exit_read(old)     -> reclaim-main can take effect; waiting on []
	// -- final --
	// reclaim-quick   effective@3   waiters=[]
	// reclaim-main    effective@16  waiters=[]
}
