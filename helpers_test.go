package reqreplybridge_test

import (
	"sync"
	"testing"

	reqreplybridge "reqreplybridge"
)

// recorder is a client-side reply sink.
type recorder struct {
	mu  sync.Mutex
	got []reqreplybridge.ReplyMsg
}

func newRecorder() *recorder { return &recorder{} }

func (r *recorder) handler(m reqreplybridge.ReplyMsg) {
	r.mu.Lock()
	r.got = append(r.got, m)
	r.mu.Unlock()
}

func (r *recorder) snapshot() []reqreplybridge.ReplyMsg {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]reqreplybridge.ReplyMsg, len(r.got))
	copy(out, r.got)
	return out
}

// service is a responder-side request sink used in the synthetic topology.
type service struct {
	mu   sync.Mutex
	reqs []reqreplybridge.RequestMsg
}

func newService() *service { return &service{} }

func (s *service) handler(m reqreplybridge.RequestMsg) error {
	s.mu.Lock()
	s.reqs = append(s.reqs, m)
	s.mu.Unlock()
	return nil
}

func (s *service) snapshot() []reqreplybridge.RequestMsg {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]reqreplybridge.RequestMsg, len(s.reqs))
	copy(out, s.reqs)
	return out
}

func u64p(v uint64) *uint64 { return &v }

func mustCorr(t *testing.T, s *reqreplybridge.Session) uint64 {
	t.Helper()
	c, err := s.AllocateCorrelation()
	if err != nil {
		t.Fatalf("AllocateCorrelation: %v", err)
	}
	return c
}

func mustSend(t *testing.T, bus *reqreplybridge.Bus, s *reqreplybridge.Session,
	target string, corr, branch, seq uint64) {
	t.Helper()
	err := bus.SendRequest(s, reqreplybridge.RequestMsg{
		Target:      target,
		Correlation: corr,
		Branch:      branch,
		Seq:         seq,
		Payload:     "request",
	})
	if err != nil {
		t.Fatalf("SendRequest: %v", err)
	}
}

func sendReply(t *testing.T, bus *reqreplybridge.Bus, to reqreplybridge.ReplyAddr,
	branch, seq uint64, branches, tail *uint64, payload string) {
	t.Helper()
	err := bus.SendReply(reqreplybridge.ReplyMsg{
		Addr:            to,
		Branch:          branch,
		Seq:             seq,
		DeclareBranches: branches,
		DeclareTail:     tail,
		Payload:         payload,
	})
	if err != nil {
		t.Fatalf("SendReply: %v", err)
	}
}
