package main

import (
	"bytes"
	"sync"
	"testing"

	"recordlayer"
)

// TestDemoFlow exercises the exact scenario shown by the demo command through
// the public API, so `go test` provides executable evidence even when running
// a separate binary is not permitted in the environment.
func TestDemoFlow(t *testing.T) {
	trA, trB := recordlayer.NewPipePair(16)
	ca, err := recordlayer.NewDemoCipher([]byte("demo-shared-secret"))
	if err != nil {
		t.Fatal(err)
	}
	cb, err := recordlayer.NewDemoCipher([]byte("demo-shared-secret"))
	if err != nil {
		t.Fatal(err)
	}
	countA := recordlayer.NewCountingCipher(ca)
	countB := recordlayer.NewCountingCipher(cb)
	connA := recordlayer.NewConn(trA, countA)
	connB := recordlayer.NewConn(trB, countB)

	var serveWG sync.WaitGroup
	serveWG.Add(2)
	go func() { defer serveWG.Done(); _ = connA.Serve() }()
	go func() { defer serveWG.Done(); _ = connB.Serve() }()

	var rot sync.WaitGroup
	rot.Add(2)
	go func() { defer rot.Done(); _ = connA.Rotate() }()
	go func() { defer rot.Done(); _ = connB.Rotate() }()
	rot.Wait()

	if err := connA.WriteApplication([]byte("app-data-from-A")); err != nil {
		t.Fatal(err)
	}
	if err := connB.WriteApplication([]byte("app-data-from-B")); err != nil {
		t.Fatal(err)
	}

	msgB, err := connB.ReadApplication()
	if err != nil || !bytes.Equal(msgB, []byte("app-data-from-A")) {
		t.Fatalf("B read: %q, %v", msgB, err)
	}
	msgA, err := connA.ReadApplication()
	if err != nil || !bytes.Equal(msgA, []byte("app-data-from-B")) {
		t.Fatalf("A read: %q, %v", msgA, err)
	}

	for name, c := range map[string]*recordlayer.Conn{"A": connA, "B": connB} {
		s := c.Snapshot()
		if s.ReadGeneration != 1 || s.WriteGeneration != 1 {
			t.Fatalf("%s generations: %+v", name, s)
		}
	}
	// A second application write flushes any response whose request arrived
	// after the previous write, and then the pending flag must be clear.
	if err := connA.WriteApplication([]byte("second-A")); err != nil {
		t.Fatal(err)
	}
	if err := connB.WriteApplication([]byte("second-B")); err != nil {
		t.Fatal(err)
	}
	if connA.Snapshot().PendingResponse || connB.Snapshot().PendingResponse {
		t.Fatal("pending response flag remained after a follow-up write")
	}
	if got, err := connB.ReadApplication(); err != nil || !bytes.Equal(got, []byte("second-A")) {
		t.Fatalf("B read second-A: %q, %v", got, err)
	}
	if got, err := connA.ReadApplication(); err != nil || !bytes.Equal(got, []byte("second-B")) {
		t.Fatalf("A read second-B: %q, %v", got, err)
	}
	for name, cc := range map[string]*recordlayer.CountingCipher{"A": countA, "B": countB} {
		// Generation 0: only the request. Generation 1: response + two app
		// records, each opened exactly once.
		if got := cc.OpenCount(0); got != 1 {
			t.Fatalf("%s gen-0 open count: got %d want 1", name, got)
		}
		if got := cc.OpenCount(1); got != 3 {
			t.Fatalf("%s gen-1 open count: got %d want 3", name, got)
		}
	}

	if err := connA.Close(); err != nil {
		t.Fatal(err)
	}
	if err := connB.Close(); err != nil {
		t.Fatal(err)
	}
	serveWG.Wait()
}
