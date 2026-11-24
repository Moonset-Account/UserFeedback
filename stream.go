package migrationplanner

import (
	"fmt"
	"sort"
)

// FrameDir selects the direction of a stream frame relative to the caller.
type FrameDir int

const (
	// DirRequest flows caller -> callee (chunked request and tail frames).
	DirRequest FrameDir = iota
	// DirResponse flows callee -> caller (including half-close acks).
	DirResponse
)

// FrameKind classifies stream frames.
type FrameKind int

const (
	// KindData is a normal bounded chunk.
	KindData FrameKind = iota
	// KindHalfClose signals that no more request data will be sent while the
	// response direction stays open.
	KindHalfClose
	// KindTail is the resendable terminal request frame. A confirmed tail is
	// exactly-once at the business-effect level.
	KindTail
)

// Frame is one observable, replayable stream frame.
type Frame struct {
	Dir    FrameDir
	Kind   FrameKind
	Seq    int
	Effect string // business result produced when the frame is accepted
}

func (f Frame) label() string {
	return fmt.Sprintf("%d/%d#%d", f.Dir, f.Kind, f.Seq)
}

// Transition is one FSM edge. From == To is allowed for idempotent re-delivery
// of an already confirmed tail frame.
type Transition struct {
	From  string
	Frame Frame
	To    string
}

// StreamProtocol is a deterministic finite-state machine describing one
// bounded bidirectional stream attached to an ordered version pair.
type StreamProtocol struct {
	Name        string
	States      []string
	Start       string
	Transitions []Transition
	// Terminal states have no outgoing transitions.
	Terminal map[string]bool

	next  map[string]map[string]Transition // state -> frame label -> transition
	label map[string]Frame
}

// Build compiles the transition table and checks internal consistency.
func (p *StreamProtocol) Build() error {
	p.next = map[string]map[string]Transition{}
	p.label = map[string]Frame{}
	known := map[string]bool{}
	for _, s := range p.States {
		known[s] = true
	}
	if !known[p.Start] {
		return fmt.Errorf("stream %q: unknown start state %q", p.Name, p.Start)
	}
	for _, t := range p.Transitions {
		if !known[t.From] || !known[t.To] {
			return fmt.Errorf("stream %q: transition references unknown state", p.Name)
		}
		if p.Terminal[t.From] {
			return fmt.Errorf("stream %q: terminal state %q has an outgoing frame", p.Name, t.From)
		}
		l := t.Frame.label()
		if existing, ok := p.next[t.From][l]; ok && existing.To != t.To {
			return fmt.Errorf("stream %q: nondeterministic frame %s in state %q", p.Name, l, t.From)
		}
		if p.next[t.From] == nil {
			p.next[t.From] = map[string]Transition{}
		}
		p.next[t.From][l] = t
		p.label[l] = t.Frame
	}
	for st := range p.Terminal {
		if !known[st] {
			return fmt.Errorf("stream %q: unknown terminal state %q", p.Name, st)
		}
	}
	return nil
}

// Initial returns the start state.
func (p *StreamProtocol) Initial() string { return p.Start }

// Step applies one labeled frame. ok is false when the frame is not allowed.
func (p *StreamProtocol) Step(state string, f Frame) (to string, effect string, ok bool) {
	t, exists := p.next[state][f.label()]
	if !exists {
		return "", "", false
	}
	return t.To, t.Frame.Effect, true
}

// labelsAt returns the frame labels enabled in a state, sorted for determinism.
func (p *StreamProtocol) labelsAt(state string) []string {
	out := make([]string, 0, len(p.next[state]))
	for l := range p.next[state] {
		out = append(out, l)
	}
	sort.Strings(out)
	return out
}

// Divergence is a replayable shortest conversation on which two stream FSMs
// disagree. Replay reproduces identical behavior up to the last common frame;
// At is the first frame one side accepts and the other rejects, or whose
// business effect differs.
type Divergence struct {
	ProtocolA string
	ProtocolB string
	Replay    []Frame // common prefix, replayable frame-by-frame
	At        Frame
	Reason    string
}

func (d *Divergence) Error() string {
	return fmt.Sprintf("stream protocols %q and %q diverge at %s (%s); replay length %d",
		d.ProtocolA, d.ProtocolB, d.At.label(), d.Reason, len(d.Replay))
}

type pairState struct {
	a string
	b string
}

type bfsNode struct {
	ps     pairState
	parent *bfsNode
	frame  *Frame // frame used to reach this node from parent
}

// Compare performs a bounded strong labeled-bisimulation check. Equal labels
// must be enabled on both sides, lead to equivalent states and produce the
// same business effect. The first disagreement is returned with its shortest
// replayable prefix.
func (p *StreamProtocol) Compare(other *StreamProtocol) *Divergence {
	root := &bfsNode{ps: pairState{p.Start, other.Start}}
	visited := map[pairState]bool{root.ps: true}
	queue := []*bfsNode{root}

	build := func(cur *bfsNode, f Frame, reason string) *Divergence {
		var replay []Frame
		for n := cur; n != nil && n.frame != nil; n = n.parent {
			replay = append(replay, *n.frame)
		}
		// Reverse: the slice was collected leaf -> root.
		for i, j := 0, len(replay)-1; i < j; i, j = i+1, j-1 {
			replay[i], replay[j] = replay[j], replay[i]
		}
		return &Divergence{
			ProtocolA: p.Name,
			ProtocolB: other.Name,
			Replay:    replay,
			At:        f,
			Reason:    reason,
		}
	}

	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]

		la := p.labelsAt(cur.ps.a)
		lb := other.labelsAt(cur.ps.b)
		if len(la) != len(lb) {
			missing := firstMissingLabel(la, lb)
			f := p.label[missing]
			if f.label() != missing {
				f = other.label[missing]
			}
			return build(cur, f, "enabled frame set differs")
		}
		for i := range la {
			if la[i] != lb[i] {
				f := p.label[la[i]]
				if f.label() != la[i] {
					f = other.label[la[i]]
				}
				return build(cur, f, "enabled frame set differs")
			}
			ta := p.next[cur.ps.a][la[i]]
			tb := other.next[cur.ps.b][lb[i]]
			if ta.Frame.Effect != tb.Frame.Effect {
				f := p.label[la[i]]
				return build(cur, f,
					fmt.Sprintf("business effect differs: %q vs %q", ta.Frame.Effect, tb.Frame.Effect))
			}
			nps := pairState{ta.To, tb.To}
			if !visited[nps] {
				visited[nps] = true
				f := p.label[la[i]]
				fc := f
				queue = append(queue, &bfsNode{ps: nps, parent: cur, frame: &fc})
			}
		}
	}
	return nil
}

func firstMissingLabel(a, b []string) string {
	inB := map[string]bool{}
	for _, l := range b {
		inB[l] = true
	}
	for _, l := range a {
		if !inB[l] {
			return l
		}
	}
	// Every label of a exists in b; with equal lengths checked by callers this
	// branch is defensive only.
	if len(b) > 0 {
		return b[len(b)-1]
	}
	return ""
}
