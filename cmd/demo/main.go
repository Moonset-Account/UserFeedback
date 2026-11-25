// Command demo shows two record-layer endpoints connected through a bounded
// in-memory transport that performs short writes. Both endpoints request a key
// rotation at the same time; the output shows the resulting frame ordering,
// per-generation sequence numbers and exact-once decryption counts.
package main

import (
	"fmt"
	"sync"

	"recordlayer"
)

func main() {
	const bufferLimit = 16 // every frame leaves in pieces of at most 16 bytes
	trA, trB := recordlayer.NewPipePair(bufferLimit)

	key := []byte("demo-shared-secret")
	ca, err := recordlayer.NewDemoCipher(key)
	if err != nil {
		panic(err)
	}
	cb, err := recordlayer.NewDemoCipher(key)
	if err != nil {
		panic(err)
	}
	countA := recordlayer.NewCountingCipher(ca)
	countB := recordlayer.NewCountingCipher(cb)
	connA := recordlayer.NewConn(trA, countA)
	connB := recordlayer.NewConn(trB, countB)

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); panicOnErr(connA.Serve()) }()
	go func() { defer wg.Done(); panicOnErr(connB.Serve()) }()

	// Both sides request a rotation simultaneously.
	var rot sync.WaitGroup
	rot.Add(2)
	go func() { defer rot.Done(); panicOnErr(connA.Rotate()) }()
	go func() { defer rot.Done(); panicOnErr(connB.Rotate()) }()
	rot.Wait()

	// Each side sends application data; the pending response is flushed first.
	panicOnErr(connA.WriteApplication([]byte("app-data-from-A")))
	panicOnErr(connB.WriteApplication([]byte("app-data-from-B")))

	msgB, err := connB.ReadApplication()
	panicOnErr(err)
	msgA, err := connA.ReadApplication()
	panicOnErr(err)
	fmt.Printf("B decrypted: %q\n", msgB)
	fmt.Printf("A decrypted: %q\n", msgA)

	snap := func(name string, c *recordlayer.Conn, cc *recordlayer.CountingCipher) {
		s := c.Snapshot()
		fmt.Printf("%s state: readGen=%d readSeq=%d writeGen=%d writeSeq=%d pendingResponse=%v\n",
			name, s.ReadGeneration, s.ReadSequence, s.WriteGeneration, s.WriteSequence, s.PendingResponse)
		fmt.Printf("%s opens per generation: gen0=%d gen1=%d\n",
			name, cc.OpenCount(0), cc.OpenCount(1))
	}
	snap("A", connA, countA)
	snap("B", connB, countB)

	panicOnErr(connA.Close())
	panicOnErr(connB.Close())
	wg.Wait()
	fmt.Println("demo finished cleanly")
}

func panicOnErr(err error) {
	if err != nil {
		panic(err)
	}
}
