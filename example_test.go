package logcursor_test

import (
	"fmt"
	"log"

	"logcursor"
)

// Example shows the intended reconnect flow: build a cursor from a page, store
// it, restore it later, and continue at the exact byte where you stopped while
// a slow step is still blocked on a hole.
func Example() {
	backend := logcursor.NewLog()

	// Two parallel steps. "compile" has already produced its first three bytes
	// and its last three bytes; [3,6) is still in flight.
	must(backend.AppendFragment("compile", 0, 0, []byte("abc")))
	must(backend.AppendFragment("compile", 0, 6, []byte("ghi")))
	must(backend.DeclareEnd("compile", 0, 9))

	// "test" finished cleanly.
	must(backend.AppendFragment("test", 0, 0, []byte("ok")))
	must(backend.DeclareEnd("test", 0, 2))

	page, err := backend.Read(logcursor.Cursor{}, 10)
	must(err)
	for _, sd := range page.Steps {
		fmt.Printf("%s: data=%q waiting=%v\n", sd.Step, sd.Data, sd.Waiting)
	}
	fmt.Printf("done=%v\n", page.Done)

	// The hole is declared permanently lost, so the step may now finish.
	must(backend.DeclareGap("compile", 0, 3, 6))
	cur, err := backend.Advance(page.Cursor)
	must(err)

	next, err := backend.Read(cur, 10)
	must(err)
	c, _ := next.Step("compile")
	fmt.Printf("compile: data=%q gaps=%v ended=%v\n", c.Data, c.Gaps, c.Ended)

	// Output:
	// compile: data="abc" waiting=true
	// test: data="ok" waiting=false
	// done=false
	// compile: data="ghi" gaps=[{compile 0 3 6}] ended=true
}

func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}
