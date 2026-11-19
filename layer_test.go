package authstack_test

import (
	"errors"
	"testing"

	"example.com/authstack"
)

// TestSufficientRuleAppliesInsideSubStack shows the "clean layer" condition is
// evaluated per layer, not globally: the sub-stack has its own remembered
// required failure, so its sufficient success must not end the sub-stack, even
// though the parent layer is itself clean.
func TestSufficientRuleAppliesInsideSubStack(t *testing.T) {
	instBad := bad("inst-required")
	instSSO := ok("inst-sso")
	instTail := ok("inst-tail")
	sub := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Required, instBad),
		authstack.ProviderItem(authstack.Sufficient, instSSO),
		authstack.ProviderItem(authstack.Required, instTail),
	)

	adminTail := ok("admin-tail")
	top := authstack.NewStack("admin",
		authstack.StackItem(authstack.Optional, sub),
		authstack.ProviderItem(authstack.Required, adminTail),
	)

	res := top.Run(req(), nil)

	if !res.Passed {
		// admin only has the optional sub-stack plus a required success, so
		// it must pass; the sub-stack's own denial is confined to it.
		t.Fatalf("Passed = false (%v), want true", res.Err)
	}
	if instTail.calls != 1 {
		t.Fatalf("inst-tail calls = %d, want 1 (inner sufficient must not short-circuit after inner required failure)", instTail.calls)
	}
	if adminTail.calls != 1 {
		t.Fatalf("admin-tail calls = %d, want 1", adminTail.calls)
	}

	subTrace := mustItem(t, res.Trace, "institution")
	if subTrace.Outcome != authstack.Failed {
		t.Fatalf("sub-stack outcome = %v, want failure (its own required item failed)", subTrace.Outcome)
	}
	if subTrace.Sub.Passed {
		t.Fatal("sub-stack passed, want denied")
	}
	if subTrace.Sub.Successes != 2 {
		t.Fatalf("sub-stack successes = %d, want 2 (inst-sso and inst-tail)", subTrace.Sub.Successes)
	}
}

// TestFuncAdapterRuns shows the function adapter is a first-class provider.
func TestFuncAdapterRuns(t *testing.T) {
	called := false
	p := authstack.Func{
		ProviderName: "adapter",
		Fn: func(_ authstack.Request, ask authstack.PromptAsker) error {
			called = true
			if _, err := ask.Ask(authstack.Prompt{Text: "code"}); err != nil {
				return err
			}
			return nil
		},
	}
	stack := authstack.NewStack("admin", authstack.ProviderItem(authstack.Required, p))

	res := stack.Run(req(), authstack.ResponderFunc(func(authstack.Prompt) (string, error) {
		return "123456", nil
	}))

	if !res.Passed {
		t.Fatalf("Passed = false (%v), want true", res.Err)
	}
	if !called {
		t.Fatal("adapter function was not called")
	}
	if got := res.PromptTexts(); len(got) != 1 || got[0] != "code" {
		t.Fatalf("prompts = %v, want [code]", got)
	}
	if res.Prompts[0].Provider != "adapter" {
		t.Fatalf("prompt provider = %q, want adapter", res.Prompts[0].Provider)
	}
}

// TestFuncWithoutImplementationFails ensures a zero Func is a failing item, not
// a silent success.
func TestFuncWithoutImplementationFails(t *testing.T) {
	res := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Required, authstack.Func{ProviderName: "empty"}),
	).Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if !errors.Is(res.Err, authstack.ErrRequiredFailure) {
		t.Fatalf("Err = %v, want ErrRequiredFailure", res.Err)
	}
}

// TestPanickingNameIsContained shows a provider whose Name() panics cannot take
// down the evaluation; the item still runs and is traced under a placeholder.
func TestPanickingNameIsContained(t *testing.T) {
	other := ok("org-sso")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Required, panickyName{}),
		authstack.ProviderItem(authstack.Required, other),
	)

	res := stack.Run(req(), nil)

	if !res.Passed {
		t.Fatalf("Passed = false (%v), want true", res.Err)
	}
	if other.calls != 1 {
		t.Fatalf("other calls = %d, want 1", other.calls)
	}
	if res.Trace.Items[0].Name != "<panicked-name>" {
		t.Fatalf("item name = %q, want <panicked-name>", res.Trace.Items[0].Name)
	}
	if res.Trace.Items[0].Outcome != authstack.Succeeded {
		t.Fatalf("panicky-name item outcome = %v, want success", res.Trace.Items[0].Outcome)
	}
}

type panickyName struct{}

func (panickyName) Name() string { panic("name exploded") }

func (panickyName) Authenticate(authstack.Request, authstack.PromptAsker) error { return nil }

// TestPromptsFromDeniedSubStackStillRecorded checks that prompts raised before a
// sub-stack's denial remain auditable at the top level.
func TestPromptsFromDeniedSubStackStillRecorded(t *testing.T) {
	sub := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Required, &fake{name: "inst-ask", fn: func(_ authstack.Request, ask authstack.PromptAsker) error {
			_, err := ask.Ask(authstack.Prompt{Text: "inst question"})
			return err
		}}),
		authstack.ProviderItem(authstack.Requisite, bad("inst-gate")),
	)
	top := authstack.NewStack("admin",
		authstack.StackItem(authstack.Optional, sub),
		authstack.ProviderItem(authstack.Required, ok("local-password")),
	)

	res := top.Run(req(), authstack.ResponderFunc(func(authstack.Prompt) (string, error) {
		return "x", nil
	}))

	got := res.PromptTexts()
	if len(got) != 1 || got[0] != "inst question" {
		t.Fatalf("prompts = %v, want [inst question]", got)
	}
	if !res.Passed {
		t.Fatalf("Passed = false (%v), want true (optional sub-stack failure is not remembered)", res.Err)
	}
	if res.Trace.RequiredFailed {
		t.Fatal("optional sub-stack failure was remembered as a required failure")
	}
}
