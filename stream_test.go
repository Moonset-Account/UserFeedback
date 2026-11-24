package migrationplanner

import "testing"

// buildV1 is a bounded bidirectional stream: two request chunks, a half
// close, one response, then a resendable terminal tail frame. After the tail
// is confirmed the session lingers in a non-terminal "confirmed" state whose
// only enabled frame is tail re-delivery (a self loop).
func buildV1(t *testing.T) *StreamProtocol {
	t.Helper()
	p := &StreamProtocol{
		Name:   "stream-v1",
		States: []string{"s0", "s1", "s2", "s3", "s4", "confirmed", "done"},
		Start:  "s0",
		Transitions: []Transition{
			{From: "s0", Frame: Frame{DirRequest, KindData, 1, "chunk-1"}, To: "s1"},
			{From: "s1", Frame: Frame{DirRequest, KindData, 2, "chunk-2"}, To: "s2"},
			{From: "s2", Frame: Frame{DirRequest, KindHalfClose, 3, ""}, To: "s3"},
			{From: "s3", Frame: Frame{DirResponse, KindData, 4, "resp"}, To: "s4"},
			{From: "s4", Frame: Frame{DirRequest, KindTail, 5, "commit"}, To: "confirmed"},
			// Confirmed tail re-delivery is a self loop: the business effect is
			// already produced, so the proxy must not process it twice.
			{From: "confirmed", Frame: Frame{DirRequest, KindTail, 5, "commit"}, To: "confirmed"},
			// A separate finalize frame reaches the truly terminal state.
			{From: "confirmed", Frame: Frame{DirResponse, KindData, 6, "finalize"}, To: "done"},
		},
		Terminal: map[string]bool{"done": true},
	}
	if err := p.Build(); err != nil {
		t.Fatalf("Build v1: %v", err)
	}
	return p
}

// buildV2Equivalent has the same labeled behavior and effects as v1 but with
// extra internal states, exercising the bisimulation rather than state names.
func buildV2Equivalent(t *testing.T) *StreamProtocol {
	t.Helper()
	p := &StreamProtocol{
		Name:   "stream-v2",
		States: []string{"a0", "a1", "a1b", "a2", "a3", "ack", "finished"},
		Start:  "a0",
		Transitions: []Transition{
			{From: "a0", Frame: Frame{DirRequest, KindData, 1, "chunk-1"}, To: "a1"},
			{From: "a1", Frame: Frame{DirRequest, KindData, 2, "chunk-2"}, To: "a1b"},
			{From: "a1b", Frame: Frame{DirRequest, KindHalfClose, 3, ""}, To: "a2"},
			{From: "a2", Frame: Frame{DirResponse, KindData, 4, "resp"}, To: "a3"},
			{From: "a3", Frame: Frame{DirRequest, KindTail, 5, "commit"}, To: "ack"},
			{From: "ack", Frame: Frame{DirRequest, KindTail, 5, "commit"}, To: "ack"},
			{From: "ack", Frame: Frame{DirResponse, KindData, 6, "finalize"}, To: "finished"},
		},
		Terminal: map[string]bool{"finished": true},
	}
	if err := p.Build(); err != nil {
		t.Fatalf("Build v2: %v", err)
	}
	return p
}

// buildV2Divergent drops the half-close frame, so an in-flight v1 session
// hits a frame the new FSM rejects.
func buildV2Divergent(t *testing.T) *StreamProtocol {
	t.Helper()
	p := &StreamProtocol{
		Name:   "stream-v2-bad",
		States: []string{"b0", "b1", "b2", "b3", "confirmed", "done"},
		Start:  "b0",
		Transitions: []Transition{
			{From: "b0", Frame: Frame{DirRequest, KindData, 1, "chunk-1"}, To: "b1"},
			{From: "b1", Frame: Frame{DirRequest, KindData, 2, "chunk-2"}, To: "b2"},
			// No half-close: a v1 session that already sent it cannot continue.
			{From: "b2", Frame: Frame{DirResponse, KindData, 4, "resp"}, To: "b3"},
			{From: "b3", Frame: Frame{DirRequest, KindTail, 5, "commit"}, To: "confirmed"},
			{From: "confirmed", Frame: Frame{DirRequest, KindTail, 5, "commit"}, To: "confirmed"},
			{From: "confirmed", Frame: Frame{DirResponse, KindData, 6, "finalize"}, To: "done"},
		},
		Terminal: map[string]bool{"done": true},
	}
	if err := p.Build(); err != nil {
		t.Fatalf("Build v2-bad: %v", err)
	}
	return p
}

func TestEquivalentProtocolsHaveNoDivergence(t *testing.T) {
	v1 := buildV1(t)
	v2 := buildV2Equivalent(t)
	if d := v1.Compare(v2); d != nil {
		t.Fatalf("expected equivalent FSMs, got divergence: %v", d)
	}
}

func TestDivergentProtocolYieldsReplayableShortestDialogue(t *testing.T) {
	v1 := buildV1(t)
	bad := buildV2Divergent(t)
	d := v1.Compare(bad)
	if d == nil {
		t.Fatal("expected a divergence between v1 and the half-close-less v2")
	}
	if d.At.Kind != KindHalfClose {
		t.Fatalf("divergence should be the missing half-close, got %s", d.At.label())
	}
	// The replay prefix must be a real, replayable v1 sequence ending right
	// before the offending frame.
	st := v1.Initial()
	var effects []string
	for _, f := range d.Replay {
		to, effect, ok := v1.Step(st, f)
		if !ok {
			t.Fatalf("replay frame %s not allowed by v1", f.label())
		}
		st = to
		if effect != "" {
			effects = append(effects, effect)
		}
	}
	// The two data chunks precede the half-close, so replay length is 2.
	if len(d.Replay) != 2 {
		t.Fatalf("expected shortest replay of 2 frames, got %d", len(d.Replay))
	}
	// Replaying the offending half-close on the new FSM must indeed fail.
	if _, _, ok := bad.Step(st, d.At); ok {
		t.Fatal("the divergent frame must be rejected by the new FSM")
	}
}

func TestProxyPreservesContinuationsAndBusinessEffects(t *testing.T) {
	v1 := buildV1(t)
	v2 := buildV2Equivalent(t)
	// Old session is sitting after the two request chunks, before half-close.
	proxy, err := NewProxy(v1, v2, "s2")
	if err != nil {
		t.Fatalf("NewProxy: %v", err)
	}
	half := Frame{DirRequest, KindHalfClose, 3, ""}
	resp := Frame{DirResponse, KindData, 4, "resp"}
	tail := Frame{DirRequest, KindTail, 5, "commit"}
	for _, f := range []Frame{half, resp, tail} {
		if _, _, err := proxy.Accept(f); err != nil {
			t.Fatalf("Accept %s: %v", f.label(), err)
		}
	}
	got := proxy.AppliedEffects()
	want := []string{"resp", "commit"}
	if len(got) != len(want) {
		t.Fatalf("effects=%v want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("effects=%v want %v", got, want)
		}
	}
	if proxy.MappedState() != "ack" {
		t.Fatalf("mapped state=%s want ack", proxy.MappedState())
	}
	if proxy.RollbackState() != "confirmed" {
		t.Fatalf("rollback state=%s want confirmed", proxy.RollbackState())
	}
}

func TestProxyTailFrameIsProcessedExactlyOnce(t *testing.T) {
	v1 := buildV1(t)
	v2 := buildV2Equivalent(t)
	proxy, err := NewProxy(v1, v2, "s4")
	if err != nil {
		t.Fatalf("NewProxy: %v", err)
	}
	tail := Frame{DirRequest, KindTail, 5, "commit"}
	effect, replayed, err := proxy.Accept(tail)
	if err != nil || replayed || effect != "commit" {
		t.Fatalf("first tail: effect=%q replayed=%v err=%v", effect, replayed, err)
	}
	// A retransmitted confirmed tail returns the stored effect but must not be
	// processed again.
	effect2, replayed2, err := proxy.Accept(tail)
	if err != nil {
		t.Fatalf("retransmitted tail: %v", err)
	}
	if !replayed2 {
		t.Fatal("retransmitted confirmed tail must be flagged as replayed")
	}
	if effect2 != "commit" {
		t.Fatalf("replayed tail effect=%q want commit", effect2)
	}
	if len(proxy.AppliedEffects()) != 1 {
		t.Fatalf("tail processed more than once: effects=%v", proxy.AppliedEffects())
	}
}

func TestProxyRejectsWhenNoCoveringStateExists(t *testing.T) {
	v1 := buildV1(t)
	bad := buildV2Divergent(t)
	// At s2 v1 still needs to half-close; no state of the bad FSM offers that.
	if _, err := NewProxy(v1, bad, "s2"); err == nil {
		t.Fatal("expected mapping failure when the new FSM cannot cover continuations")
	}
}
