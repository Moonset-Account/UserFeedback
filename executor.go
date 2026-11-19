package authstack

import (
	"errors"
	"fmt"
)

// maxDepth bounds runtime recursion as a safety net. Cycles are rejected by
// Validate before anything runs, so a legitimate stack can never get close.
const maxDepth = 1024

// Run evaluates a stack for one authentication request.
//
// The returned Result is never nil. When the stack definition is invalid, Run
// performs no authentication at all and returns a failed Result carrying the
// validation error; the error is not a *DenialError because no layer decision
// was made.
func (s *Stack) Run(req Request, responder Responder) *Result {
	if s == nil {
		return &Result{
			Passed: false,
			Err:    &ValidationError{Problems: []string{"stack is nil"}},
			Trace:  &LayerTrace{Name: "", Reason: "invalid stack"},
		}
	}
	if err := s.Validate(); err != nil {
		return &Result{
			Stack:  s.Name,
			Passed: false,
			Err:    err,
			Trace:  &LayerTrace{Name: s.Name, Reason: "invalid stack: " + err.Error()},
		}
	}
	rt := &runState{req: req, responder: responder}
	trace, err := rt.runLayer(s.Name, s.Items, 0)
	res := &Result{
		Stack:   s.Name,
		Passed:  err == nil,
		Err:     err,
		Trace:   trace,
		Prompts: rt.prompts,
	}
	return res
}

// Run evaluates stack for req. It is the package-level form of Stack.Run.
func Run(stack *Stack, req Request, responder Responder) *Result {
	return stack.Run(req, responder)
}

type runState struct {
	req       Request
	responder Responder
	prompts   []*PromptEvent
}

// runLayer evaluates one layer. It returns its trace and, when the layer is
// denied, a *DenialError carrying the reason and the item errors that caused
// it. A nil error means the layer passed.
func (rt *runState) runLayer(name string, items []Item, depth int) (*LayerTrace, error) {
	lt := &LayerTrace{Name: name, Items: make([]ItemTrace, len(items))}

	requiredFailed := false
	successes := 0

	halted := false
	haltPassed := false
	haltReason := error(nil)
	haltIndex := -1
	causes := make([]error, 0, len(items))

	for i, item := range items {
		it := ItemTrace{
			Index:   i,
			Control: item.Control,
			Kind:    itemKind(item),
			Name:    itemName(item),
		}

		if halted {
			it.Outcome = NotRun
			it.Halt = fmt.Sprintf("skipped: layer already ended by item %d", haltIndex)
			lt.Items[i] = it
			continue
		}

		promptStart := len(rt.prompts)
		var err error
		if item.Stack != nil {
			it.Sub, err = rt.runItemStack(item.Stack, depth+1)
			if err != nil {
				// Only the nested stack's pass/fail verdict crosses this
				// boundary, carried as a SubStackError. The inner layer's
				// own sentinel reason stays behind, so it can never be
				// mistaken for this layer's decision.
				err = &SubStackError{Stack: it.Sub.Name, Cause: err}
			}
		} else {
			err = rt.runItemProvider(item.Provider)
		}
		it.Prompts = rt.prompts[promptStart:]

		if err != nil {
			it.Outcome = Failed
			it.Err = err.Error()
		} else {
			it.Outcome = Succeeded
		}

		switch item.Control {
		case Required:
			if err != nil {
				requiredFailed = true
				causes = append(causes, err)
			} else {
				successes++
			}
		case Requisite:
			if err != nil {
				halted = true
				haltPassed = false
				haltReason = fmt.Errorf("%w (%s)", ErrRequisiteFailure, it.Name)
				haltIndex = i
				causes = append(causes, err)
				it.Halt = "ended layer: requisite item failed"
			} else {
				successes++
			}
		case Sufficient:
			if err != nil {
				// A sufficient failure is noted but never remembered, so it
				// cannot block a later sufficient success.
				break
			}
			successes++
			if requiredFailed {
				it.Halt = "success not used: the layer already recorded a required failure"
				break
			}
			halted = true
			haltPassed = true
			haltIndex = i
			it.Halt = "ended layer: sufficient item succeeded"
		case Optional:
			if err == nil {
				successes++
			}
		}

		lt.Items[i] = it
	}

	lt.Successes = successes
	lt.RequiredFailed = requiredFailed

	switch {
	case halted && haltPassed:
		lt.Passed = true
		lt.Reason = "ended early by a sufficient success"
		return lt, nil
	case halted:
		lt.Passed = false
		lt.Reason = haltReason.Error()
		return lt, &DenialError{
			Stack:  name,
			User:   rt.req.Username,
			Reason: haltReason,
			Causes: causes,
		}
	case requiredFailed:
		lt.Passed = false
		lt.Reason = ErrRequiredFailure.Error()
		return lt, &DenialError{
			Stack:  name,
			User:   rt.req.Username,
			Reason: ErrRequiredFailure,
			Causes: causes,
		}
	case successes == 0:
		lt.Passed = false
		lt.Reason = ErrNoSuccess.Error()
		return lt, &DenialError{
			Stack:  name,
			User:   rt.req.Username,
			Reason: ErrNoSuccess,
		}
	default:
		lt.Passed = true
		lt.Reason = "at least one item succeeded and no required item failed"
		return lt, nil
	}
}

// runItemStack evaluates a nested stack and returns its trace. Depth is
// checked here so a stack built at runtime cannot recurse without bound.
func (rt *runState) runItemStack(sub *Stack, depth int) (*LayerTrace, error) {
	if sub == nil {
		return &LayerTrace{Name: "", Passed: false, Reason: "nil sub-stack"}, &DenialError{
			Reason: ErrInvalidStack,
			Causes: []error{errors.New("authstack: nil sub-stack")},
		}
	}
	name := sub.Name
	if depth > maxDepth {
		return &LayerTrace{Name: name, Passed: false, Reason: "nesting too deep"}, &DenialError{
			Stack:  name,
			Reason: ErrCycle,
			Causes: []error{fmt.Errorf("authstack: nesting exceeded %d levels", maxDepth)},
		}
	}
	return rt.runLayer(name, sub.Items, depth)
}

// runItemProvider runs one provider with panic containment.
func (rt *runState) runItemProvider(p Provider) (err error) {
	if p == nil {
		return errors.New("authstack: nil provider")
	}
	name := safeProviderName(p)
	ask := &asker{rt: rt, provider: name}
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("authstack: provider %q panicked: %v", name, r)
		}
	}()
	return p.Authenticate(rt.req, ask)
}

// asker delivers prompts to the caller's responder and records every attempt.
type asker struct {
	rt       *runState
	provider string
}

// Ask implements PromptAsker.
func (a *asker) Ask(p Prompt) (string, error) {
	p.Provider = a.provider
	ev := &PromptEvent{
		Provider: a.provider,
		Text:     p.Text,
		Secret:   p.Secret,
	}
	a.rt.prompts = append(a.rt.prompts, ev)

	if a.rt.responder == nil {
		ev.Err = ErrPromptUnsupported.Error()
		return "", ErrPromptUnsupported
	}

	answer, err := a.rt.responder.Respond(p)
	if err != nil {
		ev.Err = err.Error()
		return "", err
	}
	ev.Answered = true
	return answer, nil
}

// responderFunc adapts a function to Responder.
type responderFunc func(Prompt) (string, error)

func (f responderFunc) Respond(p Prompt) (string, error) { return f(p) }

// ResponderFunc builds a Responder from a function.
func ResponderFunc(f func(Prompt) (string, error)) Responder { return responderFunc(f) }

func safeProviderName(p Provider) (name string) {
	defer func() {
		if r := recover(); r != nil {
			name = "<panicked-name>"
		}
	}()
	name = p.Name()
	if name == "" {
		return "<unnamed>"
	}
	return name
}

func itemKind(it Item) string {
	switch {
	case it.Provider != nil && it.Stack != nil:
		return "invalid"
	case it.Stack != nil:
		return "stack"
	default:
		return "provider"
	}
}

func itemName(it Item) string {
	switch {
	case it.Stack != nil:
		if it.Label != "" {
			return it.Label
		}
		if it.Stack.Name == "" {
			return "<unnamed-stack>"
		}
		return it.Stack.Name
	case it.Provider != nil:
		return safeProviderName(it.Provider)
	default:
		return "<nil-item>"
	}
}
