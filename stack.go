package authstack

import (
	"fmt"
	"strings"
)

// Stack is an ordered list of items evaluated as one authentication layer.
//
// A stack may be used as an item of another stack. The nested stack then
// behaves like a single provider: it reports success or failure, and the
// parent's control flag decides what that report means. A stack's own
// short-circuit (requisite failure or sufficient success) is confined to that
// stack and never stops its parent.
type Stack struct {
	// Name identifies the layer in traces, prompts and errors.
	Name string
	// Items are evaluated in declaration order until the layer ends.
	Items []Item
}

// Item is one entry of a Stack. Exactly one of Provider and Stack must be set.
type Item struct {
	// Control decides how this item's result affects the layer.
	Control Control
	// Provider is the authentication provider for a plain item.
	Provider Provider
	// Stack is the nested stack for a composed item.
	Stack *Stack
	// Label overrides the name shown for a nested stack in traces. It has no
	// effect on plain providers.
	Label string
}

// ProviderItem builds a plain provider item.
func ProviderItem(control Control, p Provider) Item {
	return Item{Control: control, Provider: p}
}

// StackItem builds a nested-stack item.
func StackItem(control Control, s *Stack) Item {
	return Item{Control: control, Stack: s}
}

// NewStack builds a stack from its name and items.
func NewStack(name string, items ...Item) *Stack {
	return &Stack{Name: name, Items: items}
}

// ValidationError lists every problem found in a stack definition.
type ValidationError struct {
	Problems []string
}

// Error implements error.
func (e *ValidationError) Error() string {
	return fmt.Sprintf("authstack: invalid stack: %s", strings.Join(e.Problems, "; "))
}

// Unwrap lets errors.Is(err, ErrInvalidStack) match.
func (e *ValidationError) Unwrap() error { return ErrInvalidStack }

// Validate checks the whole stack tree before anything runs. It rejects
// unknown control flags, items that set both or neither of Provider/Stack,
// nil nested stacks, and cycles.
func (s *Stack) Validate() error {
	if s == nil {
		return &ValidationError{Problems: []string{"stack is nil"}}
	}
	v := &validator{active: make(map[*Stack]bool)}
	v.walk(s, "root")
	if len(v.problems) > 0 {
		return &ValidationError{Problems: v.problems}
	}
	return nil
}

type validator struct {
	active   map[*Stack]bool
	problems []string
}

func (v *validator) walk(s *Stack, path string) {
	if s == nil {
		v.problems = append(v.problems, path+": stack is nil")
		return
	}
	if v.active[s] {
		v.problems = append(v.problems, path+": cycle detected via stack "+s.Name)
		return
	}
	v.active[s] = true
	defer delete(v.active, s)

	for i, it := range s.Items {
		where := fmt.Sprintf("%s/items[%d]", path, i)
		if !it.Control.valid() {
			v.problems = append(v.problems, fmt.Sprintf("%s: unknown control flag %d", where, int(it.Control)))
		}
		hasProvider := it.Provider != nil
		hasStack := it.Stack != nil
		switch {
		case hasProvider && hasStack:
			v.problems = append(v.problems, where+": item sets both Provider and Stack")
		case !hasProvider && !hasStack:
			v.problems = append(v.problems, where+": item sets neither Provider nor Stack")
		case hasStack:
			v.walk(it.Stack, where)
		}
	}
}
