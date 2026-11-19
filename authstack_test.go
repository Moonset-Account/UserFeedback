package authstack_test

import (
	"errors"
	"fmt"
	"testing"

	"example.com/authstack"
)

// fake is a configurable provider used across the tests.
type fake struct {
	name  string
	fn    func(req authstack.Request, ask authstack.PromptAsker) error
	err   error
	calls int
}

func (f *fake) Name() string { return f.name }

func (f *fake) Authenticate(req authstack.Request, ask authstack.PromptAsker) error {
	f.calls++
	if f.fn != nil {
		return f.fn(req, ask)
	}
	return f.err
}

func ok(name string) *fake { return &fake{name: name} }

func bad(name string) *fake {
	return &fake{name: name, err: errors.New(name + " rejected the credentials")}
}

func req() authstack.Request {
	return authstack.Request{Username: "alice", Secret: "s3cret"}
}

func mustItem(t *testing.T, lt *authstack.LayerTrace, name string) authstack.ItemTrace {
	t.Helper()
	if lt == nil {
		t.Fatalf("trace is nil, want item %q", name)
	}
	it := lt.ItemTraceFor(name)
	if it == nil {
		t.Fatalf("trace has no item %q", name)
	}
	return *it
}

// TestPassWhenRequiredSucceedsAndNoFailureRemembered covers the acceptable
// outcome: one success, no required failure => layer passes.
func TestPassWhenRequiredSucceedsAndNoFailureRemembered(t *testing.T) {
	local := ok("local-password")
	org := ok("org-sso")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Required, local),
		authstack.ProviderItem(authstack.Required, org),
	)

	res := stack.Run(req(), nil)

	if !res.Passed {
		t.Fatalf("Passed = false, want true; err=%v", res.Err)
	}
	if res.Err != nil {
		t.Fatalf("Err = %v, want nil", res.Err)
	}
	if local.calls != 1 || org.calls != 1 {
		t.Fatalf("calls = %d/%d, want 1/1", local.calls, org.calls)
	}
	if res.Trace.Successes != 2 {
		t.Fatalf("Successes = %d, want 2", res.Trace.Successes)
	}
	if res.Trace.RequiredFailed {
		t.Fatal("RequiredFailed = true, want false")
	}
}

// TestRequiredFailureIsRememberedAndEvaluationContinues covers the blocked
// outcome plus the "remember and continue" rule: the required failure is
// recorded, later items still run, and the layer is denied at the end.
func TestRequiredFailureIsRememberedAndEvaluationContinues(t *testing.T) {
	local := bad("local-password")
	org := ok("org-sso")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Required, local),
		authstack.ProviderItem(authstack.Required, org),
	)

	res := stack.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if !errors.Is(res.Err, authstack.ErrRequiredFailure) {
		t.Fatalf("Err = %v, want ErrRequiredFailure", res.Err)
	}
	if org.calls != 1 {
		t.Fatalf("later provider calls = %d, want 1 (required failure must not stop the layer)", org.calls)
	}
	if !res.Trace.RequiredFailed {
		t.Fatal("RequiredFailed = false, want true")
	}
	it := mustItem(t, res.Trace, "local-password")
	if it.Outcome != authstack.Failed {
		t.Fatalf("outcome = %v, want failure", it.Outcome)
	}
}

// TestRequisiteFailureEndsLayerImmediately covers the immediate-failure rule.
func TestRequisiteFailureEndsLayerImmediately(t *testing.T) {
	gate := bad("device-trust")
	local := ok("local-password")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Requisite, gate),
		authstack.ProviderItem(authstack.Required, local),
	)

	res := stack.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if !errors.Is(res.Err, authstack.ErrRequisiteFailure) {
		t.Fatalf("Err = %v, want ErrRequisiteFailure", res.Err)
	}
	if local.calls != 0 {
		t.Fatalf("later provider calls = %d, want 0 (layer must end immediately)", local.calls)
	}
	if got := mustItem(t, res.Trace, "local-password").Outcome; got != authstack.NotRun {
		t.Fatalf("skipped item outcome = %v, want not-run", got)
	}
}

// TestSufficientSuccessEndsLayerWhenClean covers early success.
func TestSufficientSuccessEndsLayerWhenClean(t *testing.T) {
	org := ok("org-sso")
	fallback := ok("local-password")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Sufficient, org),
		authstack.ProviderItem(authstack.Required, fallback),
	)

	res := stack.Run(req(), nil)

	if !res.Passed {
		t.Fatalf("Passed = false, want true; err=%v", res.Err)
	}
	if fallback.calls != 0 {
		t.Fatalf("fallback calls = %d, want 0 (sufficient success ends the layer)", fallback.calls)
	}
	if got := mustItem(t, res.Trace, "local-password").Outcome; got != authstack.NotRun {
		t.Fatalf("skipped item outcome = %v, want not-run", got)
	}
}

// TestSufficientSuccessDoesNotEndLayerAfterRememberedFailure is the boundary
// between the two rules: the same sufficient success ends a clean layer but
// must not end a layer that already remembered a required failure. The layer
// still runs subsequent items and is denied at the end.
func TestSufficientSuccessDoesNotEndLayerAfterRememberedFailure(t *testing.T) {
	local := bad("local-password")
	org := ok("org-sso")
	trailer := ok("audit-marker")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Required, local),
		authstack.ProviderItem(authstack.Sufficient, org),
		authstack.ProviderItem(authstack.Required, trailer),
	)

	res := stack.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if !errors.Is(res.Err, authstack.ErrRequiredFailure) {
		t.Fatalf("Err = %v, want ErrRequiredFailure", res.Err)
	}
	if trailer.calls != 1 {
		t.Fatalf("trailer calls = %d, want 1 (sufficient must not short-circuit after remembered failure)", trailer.calls)
	}
	orgTrace := mustItem(t, res.Trace, "org-sso")
	if orgTrace.Outcome != authstack.Succeeded {
		t.Fatalf("sufficient outcome = %v, want success", orgTrace.Outcome)
	}
	if orgTrace.Halt == "" {
		t.Fatal("expected the trace to explain why the sufficient success was not used")
	}
}

// TestNoSuccessDenies even when no required item failed.
func TestNoSuccessDenies(t *testing.T) {
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Optional, bad("optional-mfa")),
	)

	res := stack.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if !errors.Is(res.Err, authstack.ErrNoSuccess) {
		t.Fatalf("Err = %v, want ErrNoSuccess", res.Err)
	}
}

// TestNestedRequisiteFailureDoesNotEscapeSubStack is the central cross-layer
// constraint: the sub-stack halts on its requisite failure, but that halt is
// confined. The parent continues, still records the required failure the
// sub-stack reported, and denies at the end because of it.
func TestNestedRequisiteFailureDoesNotEscapeSubStack(t *testing.T) {
	subGate := bad("sub-gate")
	subAfter := ok("sub-after")
	sub := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Requisite, subGate),
		authstack.ProviderItem(authstack.Required, subAfter),
	)

	after := ok("parent-after")
	top := authstack.NewStack("admin",
		authstack.StackItem(authstack.Required, sub),
		authstack.ProviderItem(authstack.Required, after),
	)

	res := top.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if subAfter.calls != 0 {
		t.Fatalf("sub-after calls = %d, want 0 (sub short-circuit holds inside the sub-stack)", subAfter.calls)
	}
	if after.calls != 1 {
		t.Fatalf("parent-after calls = %d, want 1 (sub short-circuit must not cross into the parent)", after.calls)
	}
	subTrace := mustItem(t, res.Trace, "institution")
	if subTrace.Outcome != authstack.Failed {
		t.Fatalf("sub-stack outcome = %v, want failure", subTrace.Outcome)
	}
	if subTrace.Sub == nil || subTrace.Sub.Passed {
		t.Fatal("expected a failing sub-stack trace")
	}
	if !errors.Is(res.Err, authstack.ErrRequiredFailure) {
		t.Fatalf("Err = %v, want ErrRequiredFailure (parent's own bookkeeping)", res.Err)
	}
	if errors.Is(res.Err, authstack.ErrRequisiteFailure) {
		t.Fatal("sub-stack's requisite sentinel leaked into the parent")
	}
}

// TestNestedSufficientSuccessIsJustAnItemSuccess shows the other half: a
// sub-stack that passes early is a single success for the parent, and the
// parent keeps going.
func TestNestedSufficientSuccessIsJustAnItemSuccess(t *testing.T) {
	subSSO := ok("inst-sso")
	subUnused := ok("inst-fallback")
	sub := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Sufficient, subSSO),
		authstack.ProviderItem(authstack.Required, subUnused),
	)

	parentNext := ok("local-password")
	top := authstack.NewStack("admin",
		authstack.StackItem(authstack.Required, sub),
		authstack.ProviderItem(authstack.Required, parentNext),
	)

	res := top.Run(req(), nil)

	if !res.Passed {
		t.Fatalf("Passed = false, want true; err=%v", res.Err)
	}
	if subUnused.calls != 0 {
		t.Fatalf("inst-fallback calls = %d, want 0", subUnused.calls)
	}
	if parentNext.calls != 1 {
		t.Fatalf("parent-next calls = %d, want 1 (sub success must not end the parent)", parentNext.calls)
	}
}

// TestNestedStackAsSufficientFailureIsNotRemembered checks that a failing
// sub-stack used as "sufficient" behaves like any other sufficient failure:
// it is not remembered, so a later sufficient success can still end the layer.
func TestNestedStackAsSufficientFailureIsNotRemembered(t *testing.T) {
	sub := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Required, bad("inst-bad")),
	)
	later := ok("org-sso")
	top := authstack.NewStack("admin",
		authstack.StackItem(authstack.Sufficient, sub),
		authstack.ProviderItem(authstack.Sufficient, later),
	)

	res := top.Run(req(), nil)

	if !res.Passed {
		t.Fatalf("Passed = false, want true; err=%v", res.Err)
	}
	if later.calls != 1 {
		t.Fatalf("later calls = %d, want 1", later.calls)
	}
}

// TestProviderUnavailableCountsAsFailure covers the "unavailable is a failure"
// rule for both a required and a requisite item.
func TestProviderUnavailableCountsAsFailure(t *testing.T) {
	t.Run("required", func(t *testing.T) {
		down := &fake{name: "ldap", err: fmt.Errorf("dial: %w", authstack.ErrProviderUnavailable)}
		other := ok("org-sso")
		stack := authstack.NewStack("admin",
			authstack.ProviderItem(authstack.Required, down),
			authstack.ProviderItem(authstack.Required, other),
		)
		res := stack.Run(req(), nil)
		if res.Passed {
			t.Fatal("Passed = true, want false")
		}
		if !errors.Is(res.Err, authstack.ErrProviderUnavailable) {
			t.Fatalf("Err = %v, want the unavailable cause to be retained", res.Err)
		}
		if other.calls != 1 {
			t.Fatalf("other calls = %d, want 1", other.calls)
		}
	})

	t.Run("requisite", func(t *testing.T) {
		down := &fake{name: "ldap", err: authstack.ErrProviderUnavailable}
		other := ok("org-sso")
		stack := authstack.NewStack("admin",
			authstack.ProviderItem(authstack.Requisite, down),
			authstack.ProviderItem(authstack.Required, other),
		)
		res := stack.Run(req(), nil)
		if res.Passed {
			t.Fatal("Passed = true, want false")
		}
		if other.calls != 0 {
			t.Fatalf("other calls = %d, want 0", other.calls)
		}
	})
}

// TestPromptsAreTracedInOrderAcrossNesting verifies that interactions can be
// audited: every prompt that actually appeared is recorded, in the order it
// appeared, including prompts raised inside a sub-stack.
func TestPromptsAreTracedInOrderAcrossNesting(t *testing.T) {
	askPassword := func(name string) *fake {
		return &fake{name: name, fn: func(_ authstack.Request, ask authstack.PromptAsker) error {
			_, err := ask.Ask(authstack.Prompt{Text: name + ": password", Secret: true})
			return err
		}}
	}
	sub := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Required, askPassword("inst-password")),
		authstack.ProviderItem(authstack.Required, askPassword("inst-otp")),
	)
	top := authstack.NewStack("admin",
		authstack.StackItem(authstack.Required, sub),
		authstack.ProviderItem(authstack.Required, askPassword("local")),
	)

	asked := []string{}
	responder := authstack.ResponderFunc(func(p authstack.Prompt) (string, error) {
		asked = append(asked, p.Text)
		return "answer", nil
	})

	res := top.Run(req(), responder)

	if !res.Passed {
		t.Fatalf("Passed = false, want true; err=%v", res.Err)
	}
	want := []string{"inst-password: password", "inst-otp: password", "local: password"}
	got := res.PromptTexts()
	if len(got) != len(want) {
		t.Fatalf("prompt texts = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("prompt[%d] = %q, want %q", i, got[i], want[i])
		}
	}
	if len(asked) != len(want) {
		t.Fatalf("responder saw %d prompts, want %d", len(asked), len(want))
	}
	for i, ev := range res.Prompts {
		if !ev.Answered {
			t.Fatalf("prompt %q not marked answered", ev.Text)
		}
		if ev.Provider != "inst-password" && ev.Provider != "inst-otp" && ev.Provider != "local" {
			t.Fatalf("prompt[%d] provider = %q, want one of inst-password/inst-otp/local", i, ev.Provider)
		}
	}
	// The sub-stack prompts must also be visible under the sub-stack item.
	subItem := mustItem(t, res.Trace, "institution")
	if len(subItem.Prompts) != 2 {
		t.Fatalf("sub item prompts = %d, want 2", len(subItem.Prompts))
	}
}

// TestPromptWithoutResponderFailsTheItem checks that a provider that needs
// interaction cannot silently succeed when no responder is wired up.
func TestPromptWithoutResponderFailsTheItem(t *testing.T) {
	needsInput := &fake{name: "otp", fn: func(_ authstack.Request, ask authstack.PromptAsker) error {
		_, err := ask.Ask(authstack.Prompt{Text: "one-time code"})
		return err
	}}
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Required, needsInput),
	)

	res := stack.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if !errors.Is(res.Err, authstack.ErrPromptUnsupported) {
		t.Fatalf("Err = %v, want ErrPromptUnsupported", res.Err)
	}
	if len(res.Prompts) != 1 {
		t.Fatalf("prompts = %d, want 1 recorded attempt", len(res.Prompts))
	}
	if res.Prompts[0].Answered {
		t.Fatal("prompt marked answered, want unanswered")
	}
	if res.Prompts[0].Err == "" {
		t.Fatal("prompt error not recorded")
	}
}

// TestResponderErrorFailsTheItem checks that a refused interaction is a failure
// and is visible in the trace.
func TestResponderErrorFailsTheItem(t *testing.T) {
	needsInput := &fake{name: "otp", fn: func(_ authstack.Request, ask authstack.PromptAsker) error {
		_, err := ask.Ask(authstack.Prompt{Text: "one-time code"})
		return err
	}}
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Required, needsInput),
	)
	responder := authstack.ResponderFunc(func(authstack.Prompt) (string, error) {
		return "", errors.New("user cancelled")
	})

	res := stack.Run(req(), responder)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if len(res.Prompts) != 1 || res.Prompts[0].Answered {
		t.Fatalf("prompt trace = %+v, want one unanswered prompt", res.Prompts)
	}
	if res.Prompts[0].Err != "user cancelled" {
		t.Fatalf("prompt err = %q, want %q", res.Prompts[0].Err, "user cancelled")
	}
}

// TestPanickingProviderIsContained ensures a broken provider cannot abort the
// whole evaluation.
func TestPanickingProviderIsContained(t *testing.T) {
	boom := &fake{name: "boom", fn: func(authstack.Request, authstack.PromptAsker) error {
		panic("kaboom")
	}}
	other := ok("org-sso")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Required, boom),
		authstack.ProviderItem(authstack.Required, other),
	)

	res := stack.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if other.calls != 1 {
		t.Fatalf("other calls = %d, want 1", other.calls)
	}
}

// TestValidateRejectsCyclesAndMalformedItems covers guardrails on the stack
// definition itself.
func TestValidateRejectsCyclesAndMalformedItems(t *testing.T) {
	self := authstack.NewStack("loop")
	self.Items = []authstack.Item{authstack.StackItem(authstack.Required, self)}
	if err := self.Validate(); !errors.Is(err, authstack.ErrInvalidStack) {
		t.Fatalf("Validate error = %v, want ErrInvalidStack", err)
	}
	res := self.Run(req(), nil)
	if res.Passed {
		t.Fatal("cyclic stack passed, want failure")
	}

	both := authstack.Item{Control: authstack.Required, Provider: ok("p"), Stack: authstack.NewStack("s")}
	if err := authstack.NewStack("bad", both).Validate(); !errors.Is(err, authstack.ErrInvalidStack) {
		t.Fatalf("Validate error = %v, want ErrInvalidStack", err)
	}

	neither := authstack.Item{Control: authstack.Required}
	if err := authstack.NewStack("bad", neither).Validate(); !errors.Is(err, authstack.ErrInvalidStack) {
		t.Fatalf("Validate error = %v, want ErrInvalidStack", err)
	}
}

// TestEmptyStackDenies makes sure the "at least one success" rule holds for a
// vacuous stack too.
func TestEmptyStackDenies(t *testing.T) {
	res := authstack.NewStack("empty").Run(req(), nil)
	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if !errors.Is(res.Err, authstack.ErrNoSuccess) {
		t.Fatalf("Err = %v, want ErrNoSuccess", res.Err)
	}
}

// TestInvalidStackRunsNothing checks that a malformed layer is rejected before
// any provider is consulted, so a broken definition cannot half-authenticate.
func TestInvalidStackRunsNothing(t *testing.T) {
	probe := ok("probe")
	stack := authstack.NewStack("admin",
		authstack.Item{Control: authstack.Required},
		authstack.ProviderItem(authstack.Required, probe),
	)

	res := stack.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if !errors.Is(res.Err, authstack.ErrInvalidStack) {
		t.Fatalf("Err = %v, want ErrInvalidStack", res.Err)
	}
	if probe.calls != 0 {
		t.Fatalf("probe calls = %d, want 0 (invalid stack must not run anything)", probe.calls)
	}
}

// TestTypedNilProviderIsAFailure makes sure a typed-nil provider cannot be
// mistaken for a success; the panic it causes is contained per item.
func TestTypedNilProviderIsAFailure(t *testing.T) {
	other := ok("org-sso")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Required, (*fake)(nil)),
		authstack.ProviderItem(authstack.Required, other),
	)

	res := stack.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if other.calls != 1 {
		t.Fatalf("other calls = %d, want 1", other.calls)
	}
	if !errors.Is(res.Err, authstack.ErrRequiredFailure) {
		t.Fatalf("Err = %v, want ErrRequiredFailure", res.Err)
	}
}
