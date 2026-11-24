package graphwalk

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"unicode"
)

// Expr is a label expression. The zero value is not valid; use ParseExpr or
// the Lit/Seq/Alt/Star constructors.
type Expr interface {
	String() string
	exprNode()
}

type litExpr struct{ Label string }
type epsExpr struct{}
type seqExpr struct{ Items []Expr }
type altExpr struct{ Items []Expr }
type starExpr struct{ X Expr }
type repeatExpr struct {
	X        Expr
	Min      int
	Max      int // -1 means unbounded
	expanded Expr
}

func (*litExpr) exprNode()    {}
func (*epsExpr) exprNode()    {}
func (*seqExpr) exprNode()    {}
func (*altExpr) exprNode()    {}
func (*starExpr) exprNode()   {}
func (*repeatExpr) exprNode() {}

func (e *litExpr) String() string  { return strconv.Quote(e.Label) }
func (e *epsExpr) String() string  { return "ε" }
func (e *seqExpr) String() string  { return formatSeq(e.Items) }
func (e *altExpr) String() string  { return formatAlt(e.Items) }
func (e *starExpr) String() string { return "(" + e.X.String() + ")*" }
func (e *repeatExpr) String() string {
	if e.Max < 0 {
		return "(" + e.X.String() + "){" + strconv.Itoa(e.Min) + ",}"
	}
	return "(" + e.X.String() + "){" + strconv.Itoa(e.Min) + "," + strconv.Itoa(e.Max) + "}"
}

func formatSeq(items []Expr) string {
	if len(items) == 0 {
		return "ε"
	}
	parts := make([]string, len(items))
	for i, x := range items {
		parts[i] = x.String()
	}
	return "(" + strings.Join(parts, "") + ")"
}

func formatAlt(items []Expr) string {
	parts := make([]string, len(items))
	for i, x := range items {
		parts[i] = x.String()
	}
	return "(" + strings.Join(parts, "|") + ")"
}

// Lit matches exactly one edge label.
func Lit(label string) Expr {
	if label == "" {
		return Epsilon()
	}
	return &litExpr{Label: label}
}

// Epsilon matches the empty label sequence.
func Epsilon() Expr { return &epsExpr{} }

// Seq concatenates expressions.
func Seq(items ...Expr) Expr {
	flat := make([]Expr, 0, len(items))
	for _, item := range items {
		if item == nil {
			continue
		}
		if s, ok := item.(*seqExpr); ok {
			flat = append(flat, s.Items...)
		} else if _, ok := item.(*epsExpr); ok {
			continue
		} else {
			flat = append(flat, item)
		}
	}
	if len(flat) == 0 {
		return Epsilon()
	}
	if len(flat) == 1 {
		return flat[0]
	}
	return &seqExpr{Items: flat}
}

// Alt chooses one expression branch.
func Alt(items ...Expr) Expr {
	flat := make([]Expr, 0, len(items))
	for _, item := range items {
		if item == nil {
			continue
		}
		if a, ok := item.(*altExpr); ok {
			flat = append(flat, a.Items...)
		} else {
			flat = append(flat, item)
		}
	}
	if len(flat) == 0 {
		return Epsilon()
	}
	if len(flat) == 1 {
		return flat[0]
	}
	return &altExpr{Items: flat}
}

// Star repeats zero or more times.
func Star(x Expr) Expr {
	if x == nil {
		return Epsilon()
	}
	return &starExpr{X: x}
}

// Repeat repeats x at least min and at most max times. max < 0 is unbounded;
// query hop bounds still terminate enumeration.
func Repeat(x Expr, min, max int) Expr {
	if min < 0 {
		min = 0
	}
	if max >= 0 && max < min {
		max = min
	}
	r := &repeatExpr{X: x, Min: min, Max: max}
	r.expanded = expandRepeat(x, min, max)
	return r
}

func expandRepeat(x Expr, min, max int) Expr {
	required := make([]Expr, 0, min)
	for i := 0; i < min; i++ {
		required = append(required, x)
	}
	if max < 0 {
		required = append(required, Star(x))
		return Seq(required...)
	}
	optionalCount := max - min
	for i := 0; i < optionalCount; i++ {
		required = append(required, Alt(Epsilon(), x))
	}
	return Seq(required...)
}

// ParseExpr parses a small regex syntax. Unquoted non-metacharacter runes are
// individual labels. Multi-character labels can use "quoted Go strings",
// `backtick strings`, or {braced labels}. Supported operators are implicit
// concatenation, |, *, +, ?, and {n}/{m,n}/{m,}. ε or () means empty match.
func ParseExpr(s string) (Expr, error) {
	p := &exprParser{runes: []rune(s)}
	e, err := p.parseAlt()
	if err != nil {
		return nil, err
	}
	p.skipSpace()
	if p.pos != len(p.runes) {
		return nil, fmt.Errorf("graphwalk: unexpected %q at position %d", string(p.runes[p.pos]), p.pos)
	}
	if e == nil {
		e = Epsilon()
	}
	return e, nil
}

type exprParser struct {
	runes []rune
	pos   int
}

func (p *exprParser) skipSpace() {
	for p.pos < len(p.runes) && unicode.IsSpace(p.runes[p.pos]) {
		p.pos++
	}
}

func (p *exprParser) parseAlt() (Expr, error) {
	var alts []Expr
	for {
		x, err := p.parseSeq()
		if err != nil {
			return nil, err
		}
		alts = append(alts, x)
		p.skipSpace()
		if p.pos >= len(p.runes) || p.runes[p.pos] != '|' {
			break
		}
		p.pos++
	}
	return Alt(alts...), nil
}

func (p *exprParser) parseSeq() (Expr, error) {
	var items []Expr
	for {
		p.skipSpace()
		if p.pos >= len(p.runes) {
			break
		}
		r := p.runes[p.pos]
		if r == '|' || r == ')' {
			break
		}
		x, err := p.parseQuantified()
		if err != nil {
			return nil, err
		}
		if x != nil {
			items = append(items, x)
		}
	}
	return Seq(items...), nil
}

func (p *exprParser) parseQuantified() (Expr, error) {
	x, err := p.parseAtom()
	if err != nil {
		return nil, err
	}
	p.skipSpace()
	for {
		if p.pos >= len(p.runes) {
			break
		}
		switch p.runes[p.pos] {
		case '*':
			x = Star(x)
			p.pos++
		case '+':
			x = Repeat(x, 1, -1)
			p.pos++
		case '?':
			x = Repeat(x, 0, 1)
			p.pos++
		case '{':
			newX, consumed, err := p.parseBraceRepeat(x)
			if err != nil {
				return nil, err
			}
			if !consumed {
				return x, nil
			}
			x = newX
		default:
			return x, nil
		}
		p.skipSpace()
	}
	return x, nil
}

func (p *exprParser) parseAtom() (Expr, error) {
	p.skipSpace()
	if p.pos >= len(p.runes) {
		return Epsilon(), nil
	}
	r := p.runes[p.pos]
	switch {
	case r == '(':
		p.pos++
		x, err := p.parseAlt()
		if err != nil {
			return nil, err
		}
		p.skipSpace()
		if p.pos >= len(p.runes) || p.runes[p.pos] != ')' {
			return nil, fmt.Errorf("graphwalk: missing ) in expression")
		}
		p.pos++
		return x, nil
	case r == '"' || r == '`':
		return p.parseQuoted()
	case r == '{':
		return p.parseBracedLabel()
	case r == 'ε':
		p.pos++
		return Epsilon(), nil
	case unicode.IsSpace(r):
		p.pos++
		return p.parseAtom()
	case strings.ContainsRune(")|*+?{}", r):
		return nil, fmt.Errorf("graphwalk: unexpected operator %q", r)
	default:
		p.pos++
		return Lit(string(r)), nil
	}
}

func (p *exprParser) parseQuoted() (Expr, error) {
	quote := p.runes[p.pos]
	start := p.pos
	p.pos++
	var b strings.Builder
	for p.pos < len(p.runes) {
		r := p.runes[p.pos]
		if quote == '"' && r == '\\' && p.pos+1 < len(p.runes) {
			// Preserve Go escape sequences for Unquote by copying the source.
			b.WriteRune(r)
			b.WriteRune(p.runes[p.pos+1])
			p.pos += 2
			continue
		}
		if r == quote {
			literal := string(p.runes[start : p.pos+1])
			p.pos++
			if quote == '"' {
				s, err := strconv.Unquote(literal)
				if err != nil {
					return nil, fmt.Errorf("graphwalk: invalid quoted label: %w", err)
				}
				return Lit(s), nil
			}
			return Lit(string(p.runes[start+1 : p.pos-1])), nil
		}
		p.pos++
	}
	return nil, fmt.Errorf("graphwalk: unterminated quoted label")
}

func (p *exprParser) parseBracedLabel() (Expr, error) {
	// A brace at atom position is a label. A brace after an atom is handled
	// as repetition by parseBraceRepeat.
	p.pos++
	start := p.pos
	for p.pos < len(p.runes) && p.runes[p.pos] != '}' {
		p.pos++
	}
	if p.pos >= len(p.runes) {
		return nil, fmt.Errorf("graphwalk: unterminated braced label")
	}
	label := string(p.runes[start:p.pos])
	p.pos++
	return Lit(label), nil
}

func (p *exprParser) parseBraceRepeat(x Expr) (Expr, bool, error) {
	start := p.pos
	p.pos++ // skip {
	numStart := p.pos
	first := p.readInt()
	if p.pos == numStart || first < 0 {
		p.pos = start
		return x, false, nil
	}
	p.skipSpace()
	if p.pos >= len(p.runes) {
		return nil, false, fmt.Errorf("graphwalk: incomplete repetition")
	}
	if p.runes[p.pos] == '}' {
		p.pos++
		return Repeat(x, first, first), true, nil
	}
	if p.runes[p.pos] != ',' {
		return nil, false, fmt.Errorf("graphwalk: invalid repetition at %d", start)
	}
	p.pos++
	p.skipSpace()
	if p.pos < len(p.runes) && p.runes[p.pos] == '}' {
		p.pos++
		return Repeat(x, first, -1), true, nil
	}
	secondStart := p.pos
	second := p.readInt()
	if p.pos == secondStart || second < 0 {
		return nil, false, fmt.Errorf("graphwalk: invalid repetition maximum")
	}
	p.skipSpace()
	if p.pos >= len(p.runes) || p.runes[p.pos] != '}' {
		return nil, false, fmt.Errorf("graphwalk: incomplete repetition")
	}
	p.pos++
	return Repeat(x, first, second), true, nil
}

func (p *exprParser) readInt() int {
	start := p.pos
	n := 0
	for p.pos < len(p.runes) && unicode.IsDigit(p.runes[p.pos]) {
		n = n*10 + int(p.runes[p.pos]-'0')
		p.pos++
	}
	if p.pos == start {
		return -1
	}
	return n
}

// MatchLabels reports whether a complete label sequence matches e.
func MatchLabels(e Expr, labels []string) bool {
	n := buildNFA(e)
	set := n.startSet()
	return n.accepts(set, labels)
}

type nfaTransition struct {
	label string
	to    int
}

type nfaState struct {
	eps    []int
	on     []nfaTransition
	accept bool
}

type nfa struct {
	states []nfaState
	start  int
}

type fragment struct {
	start  int
	accept int
}

func buildNFA(e Expr) *nfa {
	b := &nfaBuilder{}
	f := b.build(e)
	b.states[f.accept].accept = true
	return &nfa{states: b.states, start: f.start}
}

type nfaBuilder struct {
	states []nfaState
}

func (b *nfaBuilder) newState() int {
	b.states = append(b.states, nfaState{})
	return len(b.states) - 1
}

func (b *nfaBuilder) build(e Expr) fragment {
	switch x := e.(type) {
	case *litExpr:
		s := b.newState()
		a := b.newState()
		b.states[s].on = append(b.states[s].on, nfaTransition{label: x.Label, to: a})
		return fragment{start: s, accept: a}
	case *epsExpr:
		s := b.newState()
		a := b.newState()
		b.states[s].eps = append(b.states[s].eps, a)
		return fragment{start: s, accept: a}
	case *seqExpr:
		if len(x.Items) == 0 {
			return b.build(Epsilon())
		}
		f := b.build(x.Items[0])
		for _, item := range x.Items[1:] {
			g := b.build(item)
			b.states[f.accept].eps = append(b.states[f.accept].eps, g.start)
			f.accept = g.accept
		}
		return f
	case *altExpr:
		s := b.newState()
		a := b.newState()
		for _, item := range x.Items {
			f := b.build(item)
			b.states[s].eps = append(b.states[s].eps, f.start)
			b.states[f.accept].eps = append(b.states[f.accept].eps, a)
		}
		return fragment{start: s, accept: a}
	case *starExpr:
		f := b.build(x.X)
		s := b.newState()
		a := b.newState()
		b.states[s].eps = append(b.states[s].eps, f.start, a)
		b.states[f.accept].eps = append(b.states[f.accept].eps, f.start, a)
		return fragment{start: s, accept: a}
	case *repeatExpr:
		if x.expanded != nil {
			return b.build(x.expanded)
		}
		return b.build(Star(x.X))
	default:
		return b.build(Epsilon())
	}
}

func (n *nfa) closure(initial []int) []int {
	in := make([]bool, len(n.states))
	stack := make([]int, 0, len(initial))
	for _, id := range initial {
		if id >= 0 && id < len(n.states) && !in[id] {
			in[id] = true
			stack = append(stack, id)
		}
	}
	for len(stack) > 0 {
		id := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		for _, to := range n.states[id].eps {
			if !in[to] {
				in[to] = true
				stack = append(stack, to)
			}
		}
	}
	out := make([]int, 0, len(n.states))
	for id, ok := range in {
		if ok {
			out = append(out, id)
		}
	}
	sort.Ints(out)
	return out
}

func (n *nfa) startSet() []int {
	return n.closure([]int{n.start})
}

func (n *nfa) move(set []int, label string) []int {
	var destinations []int
	seen := map[int]bool{}
	for _, id := range set {
		for _, tr := range n.states[id].on {
			if tr.label == label && !seen[tr.to] {
				seen[tr.to] = true
				destinations = append(destinations, tr.to)
			}
		}
	}
	return n.closure(destinations)
}

func (n *nfa) accepts(set []int, labels []string) bool {
	cur := set
	for _, label := range labels {
		cur = n.move(cur, label)
		if len(cur) == 0 {
			return false
		}
	}
	for _, id := range cur {
		if n.states[id].accept {
			return true
		}
	}
	return false
}

func (n *nfa) acceptsSet(set []int) bool {
	for _, id := range set {
		if n.states[id].accept {
			return true
		}
	}
	return false
}

func (n *nfa) possibleLabels(set []int) map[string]bool {
	out := map[string]bool{}
	for _, id := range set {
		for _, tr := range n.states[id].on {
			out[tr.label] = true
		}
	}
	return out
}
