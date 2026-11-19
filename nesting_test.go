package authstack_test

import (
	"errors"
	"strings"
	"testing"

	"example.com/authstack"
)

// TestThreeLevelNestingConfinesEachShortCircuit builds admin -> device ->
// institution and checks that each layer's halt stays where it happened while
// the verdict still propagates upward as a single item result.
func TestThreeLevelNestingConfinesEachShortCircuit(t *testing.T) {
	instGate := bad("inst-gate")
	instAfter := ok("inst-after")
	institution := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Requisite, instGate),
		authstack.ProviderItem(authstack.Required, instAfter),
	)

	devicePin := ok("device-pin")
	deviceAfter := ok("device-after")
	device := authstack.NewStack("device",
		authstack.StackItem(authstack.Required, institution),
		authstack.ProviderItem(authstack.Required, devicePin),
		authstack.ProviderItem(authstack.Required, deviceAfter),
	)

	adminTail := ok("admin-tail")
	admin := authstack.NewStack("admin",
		authstack.StackItem(authstack.Required, device),
		authstack.ProviderItem(authstack.Required, adminTail),
	)

	res := admin.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if instAfter.calls != 0 {
		t.Fatalf("inst-after calls = %d, want 0", instAfter.calls)
	}
	if devicePin.calls != 1 || deviceAfter.calls != 1 {
		t.Fatalf("device items calls = %d/%d, want 1/1 (failure must not escape two levels up)", devicePin.calls, deviceAfter.calls)
	}
	if adminTail.calls != 1 {
		t.Fatalf("admin-tail calls = %d, want 1", adminTail.calls)
	}
	if !errors.Is(res.Err, authstack.ErrRequiredFailure) {
		t.Fatalf("Err = %v, want the admin layer's own ErrRequiredFailure", res.Err)
	}
	if errors.Is(res.Err, authstack.ErrRequisiteFailure) {
		t.Fatal("requisite sentinel from the innermost layer leaked all the way up")
	}

	deviceTrace := mustItem(t, res.Trace, "device")
	if deviceTrace.Sub == nil {
		t.Fatal("missing device sub-trace")
	}
	// device ran two more successful required items after the failing
	// sub-stack, but it remembered that failure, so it must deny.
	if deviceTrace.Sub.Passed {
		t.Fatal("device layer passed despite a remembered required sub-stack failure")
	}
	if !deviceTrace.Sub.RequiredFailed {
		t.Fatal("device layer did not remember the required sub-stack failure")
	}
	if deviceTrace.Sub.Successes != 2 {
		t.Fatalf("device successes = %d, want 2", deviceTrace.Sub.Successes)
	}
	instTrace := mustItem(t, deviceTrace.Sub, "institution")
	if instTrace.Sub == nil || instTrace.Sub.Passed {
		t.Fatal("expected institution sub-trace to fail")
	}
	if got := mustItem(t, instTrace.Sub, "inst-after").Outcome; got != authstack.NotRun {
		t.Fatalf("inst-after outcome = %v, want not-run", got)
	}
}

// TestRemainingItemsAfterHaltAreAllMarkedNotRun checks that the trace has one
// entry per declared item even after an early end, and that skipped items are
// distinguishable from failures.
func TestRemainingItemsAfterHaltAreAllMarkedNotRun(t *testing.T) {
	gate := bad("gate")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Requisite, gate),
		authstack.ProviderItem(authstack.Required, ok("a")),
		authstack.ProviderItem(authstack.Required, ok("b")),
		authstack.ProviderItem(authstack.Required, ok("c")),
	)

	res := stack.Run(req(), nil)

	if len(res.Trace.Items) != 4 {
		t.Fatalf("trace items = %d, want 4", len(res.Trace.Items))
	}
	for _, name := range []string{"a", "b", "c"} {
		it := mustItem(t, res.Trace, name)
		if it.Outcome != authstack.NotRun {
			t.Fatalf("%s outcome = %v, want not-run", name, it.Outcome)
		}
		if it.Halt == "" {
			t.Fatalf("%s has no explanation for being skipped", name)
		}
	}
	gateTrace := mustItem(t, res.Trace, "gate")
	if gateTrace.Halt == "" {
		t.Fatal("gate has no halt explanation")
	}
}

// TestSufficientAfterSufficientFailureStillShortCircuits pins down that a
// sufficient failure is not remembered, so it cannot block a later sufficient
// success in the same layer.
func TestSufficientAfterSufficientFailureStillShortCircuits(t *testing.T) {
	first := bad("sso-a")
	second := ok("sso-b")
	tail := ok("tail")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Sufficient, first),
		authstack.ProviderItem(authstack.Sufficient, second),
		authstack.ProviderItem(authstack.Required, tail),
	)

	res := stack.Run(req(), nil)

	if !res.Passed {
		t.Fatalf("Passed = false (%v), want true", res.Err)
	}
	if tail.calls != 0 {
		t.Fatalf("tail calls = %d, want 0", tail.calls)
	}
	if got := mustItem(t, res.Trace, "sso-a").Outcome; got != authstack.Failed {
		t.Fatalf("sso-a outcome = %v, want failure", got)
	}
	if got := mustItem(t, res.Trace, "sso-b").Outcome; got != authstack.Succeeded {
		t.Fatalf("sso-b outcome = %v, want success", got)
	}
}

// TestOptionalContributesSuccessButNeverHalts checks the optional control.
func TestOptionalContributesSuccessButNeverHalts(t *testing.T) {
	opt := ok("remember-device")
	tail := ok("tail")
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Optional, opt),
		authstack.ProviderItem(authstack.Required, tail),
	)

	res := stack.Run(req(), nil)

	if !res.Passed {
		t.Fatalf("Passed = false (%v), want true", res.Err)
	}
	if tail.calls != 1 {
		t.Fatalf("tail calls = %d, want 1 (optional success must not halt)", tail.calls)
	}
	if res.Trace.Successes != 2 {
		t.Fatalf("Successes = %d, want 2", res.Trace.Successes)
	}
}

// TestDenialErrorNamesTheLayerAndUser checks the denial carries enough context
// to be reported, and that the reason survives errors.Is.
func TestDenialErrorNamesTheLayerAndUser(t *testing.T) {
	stack := authstack.NewStack("admin",
		authstack.ProviderItem(authstack.Required, bad("local")),
	)

	res := stack.Run(authstack.Request{Username: "bob", Secret: "x"}, nil)

	var denial *authstack.DenialError
	if !errors.As(res.Err, &denial) {
		t.Fatalf("Err = %T, want *DenialError", res.Err)
	}
	if denial.Stack != "admin" || denial.User != "bob" {
		t.Fatalf("denial = %+v, want admin/bob", denial)
	}
	if !strings.Contains(res.Err.Error(), "admin") || !strings.Contains(res.Err.Error(), "bob") {
		t.Fatalf("error text = %q, want layer and user", res.Err.Error())
	}
	if !errors.Is(res.Err, authstack.ErrRequiredFailure) {
		t.Fatalf("Err = %v, want ErrRequiredFailure", res.Err)
	}
}

// TestValidationRejectsNestedCycle walks a two-stack cycle, which a
// single-stack check would miss.
func TestValidationRejectsNestedCycle(t *testing.T) {
	a := authstack.NewStack("a")
	b := authstack.NewStack("b", authstack.StackItem(authstack.Required, a))
	a.Items = []authstack.Item{authstack.StackItem(authstack.Required, b)}

	err := a.Validate()
	if !errors.Is(err, authstack.ErrInvalidStack) {
		t.Fatalf("Validate error = %v, want ErrInvalidStack", err)
	}
	if res := b.Run(req(), nil); res.Passed || !strings.Contains(res.Err.Error(), "cycle") {
		t.Fatalf("Run result = %+v, want a cycle report", res)
	}
}

// TestPromptOrderIsChronologicalAcrossPromptAndSuccess mixes a prompting
// sub-stack, a non-prompting failure and a prompting tail to show the flattened
// prompt trail reflects reality rather than declaration order guesses.
func TestPromptOrderIsChronologicalAcrossPromptAndSuccess(t *testing.T) {
	prompting := func(name string) *fake {
		return &fake{name: name, fn: func(_ authstack.Request, ask authstack.PromptAsker) error {
			_, err := ask.Ask(authstack.Prompt{Text: name})
			return err
		}}
	}
	sub := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Required, prompting("inst-1")),
		authstack.ProviderItem(authstack.Requisite, bad("inst-2")),
		authstack.ProviderItem(authstack.Required, prompting("inst-3")),
	)
	top := authstack.NewStack("admin",
		authstack.StackItem(authstack.Required, sub),
		authstack.ProviderItem(authstack.Required, prompting("admin-1")),
	)

	res := top.Run(req(), authstack.ResponderFunc(func(authstack.Prompt) (string, error) {
		return "x", nil
	}))

	got := res.PromptTexts()
	want := []string{"inst-1", "admin-1"}
	if len(got) != len(want) {
		t.Fatalf("prompts = %v, want %v (inst-3 never ran)", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("prompt[%d] = %q, want %q", i, got[i], want[i])
		}
	}
}

// TestSubStackSuccessSatisfiesAtLeastOneSuccess shows that a passing nested
// stack alone is enough for the parent's "at least one success" rule.
func TestSubStackSuccessSatisfiesAtLeastOneSuccess(t *testing.T) {
	sub := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Required, ok("inst")),
	)
	top := authstack.NewStack("admin",
		authstack.StackItem(authstack.Optional, sub),
	)

	res := top.Run(req(), nil)

	if !res.Passed {
		t.Fatalf("Passed = false (%v), want true", res.Err)
	}
	if res.Trace.Successes != 1 {
		t.Fatalf("Successes = %d, want 1", res.Trace.Successes)
	}
}

// TestSubStackFailureCarriesNoInnerSentinel is the boundary contract in the
// other direction: the parent can inspect why the sub-stack denied, but the
// inner sentinels are not errors.Is-matchable at the parent level, so they can
// never be confused with the parent's own reason.
func TestSubStackFailureCarriesNoInnerSentinel(t *testing.T) {
	sub := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Requisite, bad("inst-gate")),
	)
	top := authstack.NewStack("admin",
		authstack.StackItem(authstack.Required, sub),
	)

	res := top.Run(req(), nil)

	if !errors.Is(res.Err, authstack.ErrRequiredFailure) {
		t.Fatalf("Err = %v, want the admin layer's ErrRequiredFailure", res.Err)
	}
	if errors.Is(res.Err, authstack.ErrRequisiteFailure) {
		t.Fatal("inner requisite sentinel is visible at the parent level")
	}
	if errors.Is(res.Err, authstack.ErrProviderUnavailable) {
		t.Fatal("inner provider cause is visible at the parent level")
	}

	var subErr *authstack.SubStackError
	if !errors.As(res.Err, &subErr) {
		t.Fatalf("Err = %v, want a *SubStackError cause", res.Err)
	}
	if subErr.Stack != "institution" {
		t.Fatalf("SubStackError.Stack = %q, want institution", subErr.Stack)
	}
	inner, ok := subErr.Denial()
	if !ok {
		t.Fatalf("SubStackError.Cause = %T, want *DenialError", subErr.Cause)
	}
	if !errors.Is(inner, authstack.ErrRequisiteFailure) {
		t.Fatalf("inner denial = %v, want ErrRequisiteFailure", inner)
	}
}

// TestSubStackFailureAsRequisiteHaltsParent checks that a nested stack's
// failure, once promoted to an item result, obeys the parent's control flag.
func TestSubStackFailureAsRequisiteHaltsParent(t *testing.T) {
	sub := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Required, bad("inst")),
	)
	tail := ok("admin-tail")
	top := authstack.NewStack("admin",
		authstack.StackItem(authstack.Requisite, sub),
		authstack.ProviderItem(authstack.Required, tail),
	)

	res := top.Run(req(), nil)

	if res.Passed {
		t.Fatal("Passed = true, want false")
	}
	if tail.calls != 0 {
		t.Fatalf("admin-tail calls = %d, want 0 (parent requisite item failed)", tail.calls)
	}
	if !errors.Is(res.Err, authstack.ErrRequisiteFailure) {
		t.Fatalf("Err = %v, want the parent's ErrRequisiteFailure", res.Err)
	}
}
