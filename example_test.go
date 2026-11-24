package flowctl_test

import (
	"fmt"

	"flowctl"
)

func ExampleController_Step() {
	c, err := flowctl.New(flowctl.Config{
		Capacity:        12,
		PauseThreshold:  6,
		ResumeThreshold: 2,
		InFlight:        2,
	})
	if err != nil {
		panic(err)
	}

	// A frame reaching the high watermark is accepted and triggers pause.
	step, err := c.Step(flowctl.Round{
		Arrivals: []flowctl.Frame{{Bytes: 6, Port: 1, Priority: 0}},
	})
	if err != nil {
		panic(err)
	}
	fmt.Println("after pause:", step.Snapshot.Used, step.Snapshot.Reserved, len(c.Pauses()))

	// The promised two in-flight bytes arrive; they write the reservation off.
	arrival, err := c.Arrive(flowctl.Frame{Bytes: 2, Port: 1, Priority: 0})
	if err != nil {
		panic(err)
	}
	fmt.Println("write-off:", arrival.WrittenOff, c.Snapshot().Reserved)

	// Send six bytes, taking the flow from eight to the resume boundary at two.
	key := flowctl.Key{Port: 1, Priority: 0}
	if _, err := c.Depart(flowctl.Departure{Key: key, Bytes: 6}); err != nil {
		panic(err)
	}
	resume, ok := c.ResumeIfEligible(key)
	fmt.Println("resume:", ok, resume.Occupancy, resume.SharedMargin)

	// Output:
	// after pause: 6 2 1
	// write-off: 2 0
	// resume: true 2 10
}
