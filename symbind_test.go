package symbind

import (
	"reflect"
	"testing"
)

// mustTable builds a table and fails the test on any registration error.
func mustTable(t *testing.T, mods ...Module) *Table {
	t.Helper()
	tb := NewTable()
	for _, m := range mods {
		if err := tb.AddModule(m); err != nil {
			t.Fatalf("AddModule(%q): %v", m.Name, err)
		}
	}
	return tb
}

func mustResolve(t *testing.T, tb *Table, from string, req Request) Resolution {
	t.Helper()
	res, err := tb.Resolve(from, req)
	if err != nil {
		t.Fatalf("Resolve(%q, %+v): %v", from, req, err)
	}
	return res
}

// Strong definitions win over weak ones even when the weak one is seen
// earlier in view order (strong sits two layers deeper here).
func TestStrongBeatsWeakAcrossLayers(t *testing.T) {
	tb := mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"A"},
			Exports: []Export{{Symbol: "f", Version: "1", Strong: false, Default: true}}},
		Module{Name: "A", Namespace: "app", Deps: []string{"B"},
			Exports: []Export{{Symbol: "f", Version: "1", Strong: false, Default: true}}},
		Module{Name: "B", Namespace: "app",
			Exports: []Export{{Symbol: "f", Version: "1", Strong: true, Default: true}}},
	)
	res := mustResolve(t, tb, "R", Request{Symbol: "f"})
	if !res.Found {
		t.Fatal("expected a binding")
	}
	if res.Module != "B" || !res.Export.Strong {
		t.Fatalf("expected strong def from B, got module=%q export=%+v", res.Module, res.Export)
	}
	if want := []string{"R", "A", "B"}; !reflect.DeepEqual(res.Path, want) {
		t.Fatalf("path = %v, want %v", res.Path, want)
	}
	if want := []string{"R", "A", "B"}; !reflect.DeepEqual(res.Scanned, want) {
		t.Fatalf("scanned = %v, want %v", res.Scanned, want)
	}
}

// Ties inside the same strength class are decided by view order: first
// dependency declaration order within a layer, then shallower layers first.
func TestViewOrderBreaksTies(t *testing.T) {
	tb := mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"A", "C"}},
		Module{Name: "A", Namespace: "app", Deps: []string{"B"}},
		Module{Name: "B", Namespace: "app",
			Exports: []Export{{Symbol: "f", Version: "1", Strong: true, Default: true}}},
		Module{Name: "C", Namespace: "app",
			Exports: []Export{{Symbol: "f", Version: "1", Strong: true, Default: true}}},
	)
	res := mustResolve(t, tb, "R", Request{Symbol: "f"})
	// View order is R, A, C, B: C (layer 1) beats B (layer 2).
	if res.Module != "C" {
		t.Fatalf("expected C by layer order, got %q", res.Module)
	}
	if want := []string{"R", "A", "C", "B"}; !reflect.DeepEqual(res.Scanned, want) {
		t.Fatalf("scanned = %v, want %v", res.Scanned, want)
	}

	// Same layer: declaration order A before C decides.
	tb2 := mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"A", "C"}},
		Module{Name: "A", Namespace: "app",
			Exports: []Export{{Symbol: "f", Version: "1", Strong: true, Default: true}}},
		Module{Name: "C", Namespace: "app",
			Exports: []Export{{Symbol: "f", Version: "1", Strong: true, Default: true}}},
	)
	res2 := mustResolve(t, tb2, "R", Request{Symbol: "f"})
	if res2.Module != "A" {
		t.Fatalf("expected A by declaration order, got %q", res2.Module)
	}
}

// An explicit version accepts only the same version, regardless of the
// default flag or strength of other versions.
func TestExplicitVersionMatchesSameVersionOnly(t *testing.T) {
	tb := mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"A"}},
		Module{Name: "A", Namespace: "app", Exports: []Export{
			{Symbol: "f", Version: "1", Strong: true, Default: true},
			{Symbol: "f", Version: "2", Strong: false, Default: false},
		}},
	)
	res := mustResolve(t, tb, "R", Request{Symbol: "f", Version: "2"})
	if !res.Found || res.Export.Version != "2" || res.Export.Strong {
		t.Fatalf("expected weak v2, got %+v", res)
	}

	res = mustResolve(t, tb, "R", Request{Symbol: "f", Version: "3"})
	if res.Found {
		t.Fatalf("v3 must not bind, got %+v", res)
	}
}

// An unspecified version binds to the export marked default; non-default
// exports are invisible to it.
func TestUnspecifiedVersionUsesDefaultExport(t *testing.T) {
	tb := mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"A", "B"}},
		Module{Name: "A", Namespace: "app", Exports: []Export{
			{Symbol: "f", Version: "9", Strong: true, Default: false}, // not default: skipped
		}},
		Module{Name: "B", Namespace: "app", Exports: []Export{
			{Symbol: "f", Version: "1", Strong: false, Default: true},
		}},
	)
	res := mustResolve(t, tb, "R", Request{Symbol: "f"})
	if !res.Found || res.Module != "B" || res.Export.Version != "1" {
		t.Fatalf("expected default export from B, got %+v", res)
	}

	// No default-marked export anywhere -> not found.
	tb2 := mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"A"}},
		Module{Name: "A", Namespace: "app", Exports: []Export{
			{Symbol: "f", Version: "9", Strong: true, Default: false},
		}},
	)
	if res := mustResolve(t, tb2, "R", Request{Symbol: "f"}); res.Found {
		t.Fatalf("no default export: must not bind, got %+v", res)
	}
}

// Weak definitions are selected when no strong definition matches.
func TestWeakOnlySelected(t *testing.T) {
	tb := mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"A"}},
		Module{Name: "A", Namespace: "app", Exports: []Export{
			{Symbol: "f", Version: "1", Strong: false, Default: true},
		}},
	)
	res := mustResolve(t, tb, "R", Request{Symbol: "f"})
	if !res.Found || res.Export.Strong {
		t.Fatalf("expected weak binding, got %+v", res)
	}
}

// Dependency cycles (mutual and self) must terminate, both when a binding
// is found and when none exists.
func TestDependencyCyclesTerminate(t *testing.T) {
	tb := mustTable(t,
		Module{Name: "A", Namespace: "app", Deps: []string{"B"}},
		Module{Name: "B", Namespace: "app", Deps: []string{"A", "C"}},
		Module{Name: "C", Namespace: "app", Deps: []string{"C"}, // self loop
			Exports: []Export{{Symbol: "f", Version: "1", Strong: true, Default: true}}},
	)
	res := mustResolve(t, tb, "A", Request{Symbol: "f"})
	if !res.Found || res.Module != "C" {
		t.Fatalf("expected C, got %+v", res)
	}
	if want := []string{"A", "B", "C"}; !reflect.DeepEqual(res.Path, want) {
		t.Fatalf("path = %v, want %v", res.Path, want)
	}
	if want := []string{"A", "B", "C"}; !reflect.DeepEqual(res.Scanned, want) {
		t.Fatalf("scanned = %v, want %v", res.Scanned, want)
	}

	// Cycle with no matching symbol anywhere: query still ends.
	res = mustResolve(t, tb, "A", Request{Symbol: "missing"})
	if res.Found {
		t.Fatalf("unexpected binding: %+v", res)
	}
}

// Cross-namespace references are closed unless the depending module's
// manifest lists the foreign module, and then only the listed symbols open.
func TestCrossNamespaceManifest(t *testing.T) {
	foreign := Module{Name: "F", Namespace: "lib", Exports: []Export{
		{Symbol: "f", Version: "1", Strong: true, Default: true},
		{Symbol: "g", Version: "1", Strong: true, Default: true},
	}}

	// No manifest entry: the edge is rejected and reported as blocked.
	tb := mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"F"}},
		foreign,
	)
	res := mustResolve(t, tb, "R", Request{Symbol: "f"})
	if res.Found {
		t.Fatalf("cross-namespace without manifest must not bind: %+v", res)
	}
	if want := []string{"R->F"}; !reflect.DeepEqual(res.Blocked, want) {
		t.Fatalf("blocked = %v, want %v", res.Blocked, want)
	}

	// Manifest opens only the listed symbol: f binds, g stays closed.
	tb = mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"F"},
			Allow: map[string][]string{"F": {"f"}}},
		foreign,
	)
	res = mustResolve(t, tb, "R", Request{Symbol: "f"})
	if !res.Found || res.Module != "F" {
		t.Fatalf("expected f from F, got %+v", res)
	}
	if want := []string{"R", "F"}; !reflect.DeepEqual(res.Path, want) {
		t.Fatalf("path = %v, want %v", res.Path, want)
	}
	res = mustResolve(t, tb, "R", Request{Symbol: "g"})
	if res.Found {
		t.Fatalf("g is not in the manifest and must stay closed: %+v", res)
	}
}

// After crossing into a foreign module, further dependencies are judged by
// that module's own namespace rules: same-namespace deps are fully open,
// foreign deps need the foreign module's own manifest entry.
func TestCrossNamespaceTransitiveRules(t *testing.T) {
	mk := func(allowF map[string][]string) *Table {
		return mustTable(t,
			Module{Name: "R", Namespace: "app", Deps: []string{"F"},
				Allow: map[string][]string{"F": {"f", "h"}}},
			Module{Name: "F", Namespace: "lib", Deps: []string{"G", "H"},
				Allow:   allowF,
				Exports: []Export{{Symbol: "f", Version: "1", Strong: true, Default: true}}},
			// Same namespace as F: fully visible once F is reached.
			Module{Name: "G", Namespace: "lib", Exports: []Export{
				{Symbol: "g", Version: "1", Strong: true, Default: true}}},
			// Different namespace from F: needs F's own manifest entry.
			Module{Name: "H", Namespace: "ext", Exports: []Export{
				{Symbol: "h", Version: "1", Strong: true, Default: true}}},
		)
	}

	// F has no manifest entry for H: g binds through F, h is blocked at F->H.
	tb := mk(nil)
	res := mustResolve(t, tb, "R", Request{Symbol: "g"})
	if !res.Found || res.Module != "G" {
		t.Fatalf("expected g from G, got %+v", res)
	}
	if want := []string{"R", "F", "G"}; !reflect.DeepEqual(res.Path, want) {
		t.Fatalf("path = %v, want %v", res.Path, want)
	}
	res = mustResolve(t, tb, "R", Request{Symbol: "h"})
	if res.Found {
		t.Fatalf("h must stay closed without F's manifest entry: %+v", res)
	}
	if want := []string{"F->H"}; !reflect.DeepEqual(res.Blocked, want) {
		t.Fatalf("blocked = %v, want %v", res.Blocked, want)
	}

	// F lists H: h binds, and only because F (not R) allowed it.
	tb = mk(map[string][]string{"H": {"h"}})
	res = mustResolve(t, tb, "R", Request{Symbol: "h"})
	if !res.Found || res.Module != "H" {
		t.Fatalf("expected h from H, got %+v", res)
	}
	if want := []string{"R", "F", "H"}; !reflect.DeepEqual(res.Path, want) {
		t.Fatalf("path = %v, want %v", res.Path, want)
	}
}

// A module blocked on a cross-namespace edge stays reachable through a
// legitimate edge later in view order.
func TestBlockedModuleReachableViaLegitimateEdge(t *testing.T) {
	// R->X is blocked (no manifest entry for X). A is also foreign to R but
	// is opened via the manifest, and A->X is a legitimate internal edge.
	tb := mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"X", "A"},
			Allow: map[string][]string{"A": {"f"}}},
		Module{Name: "X", Namespace: "other", Exports: []Export{
			{Symbol: "f", Version: "1", Strong: true, Default: true}}},
		Module{Name: "A", Namespace: "other", Deps: []string{"X"}},
	)
	res := mustResolve(t, tb, "R", Request{Symbol: "f"})
	if !res.Found || res.Module != "X" {
		t.Fatalf("expected f from X via A, got %+v", res)
	}
	if want := []string{"R", "A", "X"}; !reflect.DeepEqual(res.Path, want) {
		t.Fatalf("path = %v, want %v", res.Path, want)
	}
	if want := []string{"R->X"}; !reflect.DeepEqual(res.Blocked, want) {
		t.Fatalf("blocked = %v, want %v", res.Blocked, want)
	}
}

// Interleaved case: cross-namespace restriction + version selection +
// strong/weak priority all at once.
func TestInterleavedCrossNamespaceVersionStrength(t *testing.T) {
	tb := mustTable(t,
		Module{Name: "R", Namespace: "app", Deps: []string{"A", "F"},
			Allow: map[string][]string{"F": {"f"}}},
		Module{Name: "A", Namespace: "app", Exports: []Export{
			{Symbol: "f", Version: "1", Strong: false, Default: true},
			{Symbol: "f", Version: "2", Strong: true, Default: false},
		}},
		Module{Name: "F", Namespace: "lib", Exports: []Export{
			{Symbol: "f", Version: "1", Strong: true, Default: true},
			{Symbol: "f", Version: "2", Strong: false, Default: true},
			{Symbol: "g", Version: "1", Strong: true, Default: true}, // not in manifest
		}},
	)

	// Unspecified version: default exports only; F's strong v1 beats A's
	// weak v1 even though A is scanned first.
	res := mustResolve(t, tb, "R", Request{Symbol: "f"})
	if !res.Found || res.Module != "F" || res.Export.Version != "1" || !res.Export.Strong {
		t.Fatalf("expected strong v1 from F, got %+v", res)
	}
	if want := []string{"R", "A", "F"}; !reflect.DeepEqual(res.Scanned, want) {
		t.Fatalf("scanned = %v, want %v", res.Scanned, want)
	}

	// Explicit v2: A's strong v2 matches exactly; F's weak v2 also matches
	// but loses on strength.
	res = mustResolve(t, tb, "R", Request{Symbol: "f", Version: "2"})
	if !res.Found || res.Module != "A" || !res.Export.Strong {
		t.Fatalf("expected strong v2 from A, got %+v", res)
	}

	// g exists only in F and is not in the manifest: closed.
	res = mustResolve(t, tb, "R", Request{Symbol: "g"})
	if res.Found {
		t.Fatalf("g must stay closed: %+v", res)
	}
}

// Unknown requester modules and dangling dependencies are reported.
func TestUnknownModules(t *testing.T) {
	tb := mustTable(t, Module{Name: "R", Namespace: "app"})
	if _, err := tb.Resolve("nope", Request{Symbol: "f"}); err == nil {
		t.Fatal("expected error for unknown requester")
	}

	tb = mustTable(t, Module{Name: "R", Namespace: "app", Deps: []string{"ghost"}})
	if _, err := tb.Resolve("R", Request{Symbol: "f"}); err == nil {
		t.Fatal("expected error for unknown dependency")
	}
}

// Table registration rejects empty and duplicate module names.
func TestAddModuleValidation(t *testing.T) {
	tb := NewTable()
	if err := tb.AddModule(Module{}); err == nil {
		t.Fatal("expected error for empty name")
	}
	if err := tb.AddModule(Module{Name: "R"}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if err := tb.AddModule(Module{Name: "R"}); err == nil {
		t.Fatal("expected error for duplicate name")
	}
}
