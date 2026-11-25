package reqreplybridge_test

import (
	"sync"
	"testing"

	reqreplybridge "reqreplybridge"
)

// TestConcurrentCallersInterleaving stresses correlation isolation: many
// ingress sessions each number from 1, all forwarded concurrently, and every
// reply must return to the actual originating caller with its own corr 1.
func TestConcurrentCallersInterleaving(t *testing.T) {
	top := newSingleBridge(t)
	defer top.bridge.Stop()

	const callers = 25
	var wg sync.WaitGroup
	errCh := make(chan error, callers)
	recorders := make([]*recorder, callers)
	sessions := make([]*reqreplybridge.Session, callers)
	clients := make([]*reqreplybridge.Endpoint, callers)

	for i := 0; i < callers; i++ {
		ep, err := top.left.Attach(reqreplybridge.EndpointID("client-" + itoa(i)))
		if err != nil {
			t.Fatal(err)
		}
		rec := newRecorder()
		s, err := ep.Connect(rec.handler)
		if err != nil {
			t.Fatal(err)
		}
		clients[i] = ep
		recorders[i] = rec
		sessions[i] = s
	}

	// Every caller sends correlation 1 concurrently.
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			corr, err := sessions[i].AllocateCorrelation()
			if err != nil {
				errCh <- err
				return
			}
			if err := top.left.SendRequest(sessions[i], reqreplybridge.RequestMsg{
				Target: "br-in", Correlation: corr,
			}); err != nil {
				errCh <- err
			}
		}(i)
	}
	wg.Wait()
	close(errCh)
	for err := range errCh {
		t.Fatalf("concurrent send: %v", err)
	}

	reqs := top.svc.snapshot()
	if len(reqs) != callers {
		t.Fatalf("service got %d requests, want %d", len(reqs), callers)
	}

	// Each caller gets one single-branch reply; dispatch them in reverse to
	// prove arrival order does not affect mapping.
	for i := len(reqs) - 1; i >= 0; i-- {
		sendReply(t, top.right, reqs[i].ReplyTo, 0, 0, u64p(1), u64p(0), "done")
	}

	for i := 0; i < callers; i++ {
		got := recorders[i].snapshot()
		if len(got) != 1 {
			t.Fatalf("caller %d got %d replies, want 1", i, len(got))
		}
		if got[0].Addr.Endpoint != clients[i].ID() || got[0].Addr.Correlation != 1 {
			t.Fatalf("caller %d received reply addressed to %+v, want own corr 1", i, got[0].Addr)
		}
	}
	if st := top.bridge.Stats(reqreplybridge.LeftToRight); st.FinishedParents != callers {
		t.Fatalf("FinishedParents = %d, want %d", st.FinishedParents, callers)
	}
}

// TestEndpointCloseRejectsUse covers the closed-endpoint error paths.
func TestEndpointCloseRejectsUse(t *testing.T) {
	bus := reqreplybridge.NewBus("B")
	ep, err := bus.Attach("ep")
	if err != nil {
		t.Fatal(err)
	}
	s, err := ep.Connect(nil)
	if err != nil {
		t.Fatal(err)
	}
	ep.Close()
	if _, err := s.AllocateCorrelation(); err != reqreplybridge.ErrEndpointClosed {
		t.Fatalf("alloc after close err = %v, want ErrEndpointClosed", err)
	}
	if _, err := ep.Connect(nil); err != reqreplybridge.ErrEndpointClosed {
		t.Fatalf("connect after close err = %v, want ErrEndpointClosed", err)
	}

	// Duplicate attachment is rejected.
	if _, err := bus.Attach("ep"); err == nil {
		t.Fatal("duplicate Attach should fail")
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var buf [20]byte
	pos := len(buf)
	for i > 0 {
		pos--
		buf[pos] = byte('0' + i%10)
		i /= 10
	}
	return string(buf[pos:])
}
