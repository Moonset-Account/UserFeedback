package delegationguard

import (
	"testing"
	"time"
)

// Fixed clock so temporal assertions are exact.
var (
	t0  = time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	t5  = t0.Add(5 * time.Hour)
	t9  = t0.Add(9 * time.Hour)
	t10 = t0.Add(10 * time.Hour)
	t11 = t0.Add(11 * time.Hour)
)

// fixture builds two independent delegation chains ending at "runner":
//
//	A(root) -> B -> C   C carries billing:read only
//	D(root) -> E        E carries storage:read only
//
// The two chains share no ancestor, so their scopes must never be combined.
func fixture(t *testing.T) []Delegation {
	t.Helper()
	return []Delegation{
		{
			ID: "A", Delegator: "root", Delegatee: "svc-a",
			Scopes:    []Scope{{Resource: "billing", Verb: VerbAdmin}, {Resource: "storage", Verb: VerbWrite}},
			NotBefore: t0, ExpiresAt: t10,
		},
		{
			ID: "B", Delegator: "svc-a", Delegatee: "worker", ParentID: "A",
			Scopes:    []Scope{{Resource: "billing", Verb: VerbWrite}, {Resource: "storage", Verb: VerbRead}},
			NotBefore: t0, ExpiresAt: t10,
		},
		{
			ID: "C", Delegator: "worker", Delegatee: "runner", ParentID: "B",
			Scopes:    []Scope{{Resource: "billing", Verb: VerbRead}},
			NotBefore: t0, ExpiresAt: t10,
		},
		{
			ID: "D", Delegator: "other-root", Delegatee: "worker",
			Scopes:    []Scope{{Resource: "storage", Verb: VerbWrite}},
			NotBefore: t0, ExpiresAt: t10,
		},
		{
			ID: "E", Delegator: "worker", Delegatee: "runner", ParentID: "D",
			Scopes:    []Scope{{Resource: "storage", Verb: VerbRead}},
			NotBefore: t0, ExpiresAt: t10,
		},
	}
}

func scopeSet(p *PathEvaluation) map[string]Verb {
	m := make(map[string]Verb, len(p.EffectiveScopes))
	for _, s := range p.EffectiveScopes {
		m[s.Resource] = s.Verb
	}
	return m
}

// 1. Acceptable result: a single intact chain independently covers the action,
// and effective scopes are computed as the root-to-leaf intersection.
func TestDecide_AllowedBySingleChain(t *testing.T) {
	eng, err := NewEngine(fixture(t), nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}

	dec := eng.Decide(Action{
		Delegatee: "runner",
		Scopes:    []Scope{{Resource: "billing", Verb: VerbRead}},
		At:        t9,
	})
	if !dec.Allowed || dec.ValidPath == nil {
		t.Fatalf("expected allow, got %+v", dec)
	}
	wantChain := []string{"A", "B", "C"}
	if len(dec.ValidPath.Chain) != 3 {
		t.Fatalf("valid chain = %v, want %v", dec.ValidPath.Chain, wantChain)
	}
	for i, id := range wantChain {
		if dec.ValidPath.Chain[i] != id {
			t.Fatalf("valid chain = %v, want %v", dec.ValidPath.Chain, wantChain)
		}
	}
	got := scopeSet(dec.ValidPath)
	if got["billing"] != VerbRead {
		t.Fatalf("effective scopes = %v, want billing:read", got)
	}
	if _, ok := got["storage"]; ok {
		t.Fatalf("storage must have been narrowed away, got %v", got)
	}
	if len(dec.BlockedChains) != 0 {
		t.Fatalf("no blocked chain expected, got %+v", dec.BlockedChains)
	}
}

// Weaker verb implied by a stronger grant is covered by the same chain.
func TestDecide_VerbImplication(t *testing.T) {
	eng, err := NewEngine(fixture(t), nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec := eng.Decide(Action{
		Delegatee: "worker",
		Scopes:    []Scope{{Resource: "storage", Verb: VerbRead}},
		At:        t9,
	})
	if !dec.Allowed {
		t.Fatalf("read should be implied by write grant: %+v", dec.Paths)
	}

	dec = eng.Decide(Action{
		Delegatee: "worker",
		Scopes:    []Scope{{Resource: "billing", Verb: VerbAdmin}},
		At:        t9,
	})
	if dec.Allowed {
		t.Fatal("admin must not be implied by a write grant")
	}
}

// 2. Rejected result: local permissions on different delegation chains must not
// be stitched into one authorization.
func TestDecide_NoCombiningAcrossChains(t *testing.T) {
	eng, err := NewEngine(fixture(t), nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec := eng.Decide(Action{
		Delegatee: "runner",
		Scopes: []Scope{
			{Resource: "billing", Verb: VerbRead},
			{Resource: "storage", Verb: VerbRead},
		},
		At: t9,
	})
	if dec.Allowed {
		t.Fatal("cross-chain scope combination must be denied")
	}
	if len(dec.Paths) != 2 {
		t.Fatalf("expected both chains evaluated, got %d", len(dec.Paths))
	}
	for _, p := range dec.Paths {
		if p.Status != PathInsufficient || p.Reason != ReasonScopeNotCovered {
			t.Fatalf("path %v should be insufficient, got status=%s reason=%s",
				p.Chain, p.Status, p.Reason)
		}
		if len(p.MissingScopes) != 1 {
			t.Fatalf("path %v missing = %v, want exactly one scope", p.Chain, p.MissingScopes)
		}
	}
	// Blocking position must pinpoint where each chain loses the scope.
	pos := map[string]string{}
	for _, p := range dec.Paths {
		pos[p.Chain[len(p.Chain)-1]] = p.BlockedAt
	}
	if pos["C"] != "C" { // storage dropped at C
		t.Fatalf("chain A-B-C should report drop at C, got %q", pos["C"])
	}
	if pos["E"] != "D" { // billing never existed on chain D-E
		t.Fatalf("chain D-E should report drop at root D, got %q", pos["E"])
	}
}

// 3a. Boundary: the exact expiry instant is still valid; one second later the
// chain is blocked and the expired node is reported.
func TestDecide_ExpiryBoundary(t *testing.T) {
	eng, err := NewEngine(fixture(t), nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	atExpiry := eng.Decide(Action{
		Delegatee: "runner",
		Scopes:    []Scope{{Resource: "billing", Verb: VerbRead}},
		At:        t10,
	})
	if !atExpiry.Allowed {
		t.Fatalf("action at exact expiry instant must be allowed: %+v", atExpiry.Paths)
	}

	after := eng.Decide(Action{
		Delegatee: "runner",
		Scopes:    []Scope{{Resource: "billing", Verb: VerbRead}},
		At:        t10.Add(time.Second),
	})
	if after.Allowed {
		t.Fatal("action after expiry must be denied")
	}
	// Every fixture node expires at t10, so the shallowest node on each
	// chain is the reported blocking position.
	wantBlock := map[string]Reason{"A": ReasonExpired, "D": ReasonExpired}
	gotBlock := map[string]Reason{}
	for _, bc := range after.BlockedChains {
		gotBlock[bc.BlockedAt] = bc.Reason
	}
	if len(gotBlock) != len(wantBlock) {
		t.Fatalf("blocked chains = %+v, want %+v", gotBlock, wantBlock)
	}
	for id, reason := range wantBlock {
		if gotBlock[id] != reason {
			t.Fatalf("block at %s/%s missing, got %+v", id, reason, gotBlock)
		}
	}

	// When only the leaf expires while ancestors stay live, the leaf itself
	// is the reported blocking position.
	leafOnly, err := NewEngine([]Delegation{
		{ID: "P2", Delegator: "root", Delegatee: "mid",
			Scopes: []Scope{{Resource: "db", Verb: VerbRead}}, NotBefore: t0, ExpiresAt: t11},
		{ID: "L2", Delegator: "mid", Delegatee: "runner", ParentID: "P2",
			Scopes: []Scope{{Resource: "db", Verb: VerbRead}}, NotBefore: t0, ExpiresAt: t10},
	}, nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine leafOnly: %v", err)
	}
	leafDec := leafOnly.Decide(Action{
		Delegatee: "runner",
		Scopes:    []Scope{{Resource: "db", Verb: VerbRead}},
		At:        t10.Add(time.Second),
	})
	if leafDec.Allowed || len(leafDec.BlockedChains) != 1 {
		t.Fatalf("expected one blocked chain, got %+v", leafDec)
	}
	if bc := leafDec.BlockedChains[0]; bc.BlockedAt != "L2" || bc.Reason != ReasonExpired {
		t.Fatalf("expired leaf L2 must be the blocking position, got %s/%s", bc.BlockedAt, bc.Reason)
	}
}

// 3b. Interleaving: ancestor revocation immediately changes a fresh decision,
// the blocked position is the revoked ancestor, and an independent chain keeps
// serving narrower legal actions.
func TestDecide_AncestorRevocationAndIndependentPath(t *testing.T) {
	rev := []Revocation{{DelegationID: "A", At: t5, Reason: "incident"}}
	eng, err := NewEngine(fixture(t), rev, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}

	// runner's billing action used to pass via A-B-C; revocation of A now
	// blocks that chain at A while D-E is merely insufficient.
	dec := eng.Decide(Action{
		Delegatee: "runner",
		Scopes:    []Scope{{Resource: "billing", Verb: VerbRead}},
		At:        t9,
	})
	if dec.Allowed {
		t.Fatal("revoked ancestor chain must not authorize")
	}
	var blockedHere bool
	for _, bc := range dec.BlockedChains {
		if bc.BlockedAt == "A" && bc.Reason == ReasonRevoked {
			if len(bc.Chain) != 3 || bc.Chain[0] != "A" {
				t.Fatalf("blocked chain path wrong: %v", bc.Chain)
			}
			blockedHere = true
		}
	}
	if !blockedHere {
		t.Fatalf("revocation position A not reported: %+v", dec.BlockedChains)
	}

	// worker can still choose the independent D chain for storage access.
	alt := eng.Decide(Action{
		Delegatee: "worker",
		Scopes:    []Scope{{Resource: "storage", Verb: VerbRead}},
		At:        t9,
	})
	if !alt.Allowed || alt.ValidPath == nil {
		t.Fatalf("independent chain D must remain usable: %+v", alt.Paths)
	}
	if alt.ValidPath.Chain[0] != "D" {
		t.Fatalf("valid path should be D, got %v", alt.ValidPath.Chain)
	}
}

// A revocation scheduled in the future does not affect decisions before it.
func TestDecide_FutureRevocationDoesNotBlock(t *testing.T) {
	rev := []Revocation{{DelegationID: "A", At: t11}}
	eng, err := NewEngine(fixture(t), rev, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec := eng.Decide(Action{
		Delegatee: "runner",
		Scopes:    []Scope{{Resource: "billing", Verb: VerbRead}},
		At:        t9,
	})
	if !dec.Allowed {
		t.Fatalf("future revocation must not block: %+v", dec.Paths)
	}
}

// 3c. Interleaving lifetimes: a child outliving its parent is blocked at the
// parent's position as soon as the parent expires, even though the child record
// itself remains inside its own validity window.
func TestDecide_ParentExpiryBlocksLiveChild(t *testing.T) {
	dels := []Delegation{
		{ID: "P", Delegator: "root", Delegatee: "mid",
			Scopes: []Scope{{Resource: "db", Verb: VerbWrite}}, NotBefore: t0, ExpiresAt: t5},
		{ID: "K", Delegator: "mid", Delegatee: "leaf", ParentID: "P",
			Scopes: []Scope{{Resource: "db", Verb: VerbRead}}, NotBefore: t0, ExpiresAt: t10},
	}
	eng, err := NewEngine(dels, nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec := eng.Decide(Action{
		Delegatee: "leaf",
		Scopes:    []Scope{{Resource: "db", Verb: VerbRead}},
		At:        t9,
	})
	if dec.Allowed {
		t.Fatal("parent expiry must block the still-live child")
	}
	if len(dec.BlockedChains) != 1 {
		t.Fatalf("want one blocked chain, got %+v", dec.BlockedChains)
	}
	bc := dec.BlockedChains[0]
	if bc.BlockedAt != "P" || bc.Reason != ReasonExpired {
		t.Fatalf("block at parent P expected, got %s/%s", bc.BlockedAt, bc.Reason)
	}
}

// NotBefore boundary: acting one instant before validity starts is blocked.
func TestDecide_NotBeforeBoundary(t *testing.T) {
	dels := []Delegation{
		{ID: "R", Delegator: "root", Delegatee: "leaf",
			Scopes: []Scope{{Resource: "db", Verb: VerbRead}}, NotBefore: t5, ExpiresAt: t10},
	}
	eng, err := NewEngine(dels, nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	before := eng.Decide(Action{
		Delegatee: "leaf",
		Scopes:    []Scope{{Resource: "db", Verb: VerbRead}},
		At:        t5.Add(-time.Nanosecond),
	})
	if before.Allowed || before.Paths[0].Reason != ReasonNotYetValid {
		t.Fatalf("not-before boundary violated: %+v", before.Paths)
	}
	at := eng.Decide(Action{
		Delegatee: "leaf",
		Scopes:    []Scope{{Resource: "db", Verb: VerbRead}},
		At:        t5,
	})
	if !at.Allowed {
		t.Fatalf("action at not-before instant must be allowed: %+v", at.Paths)
	}
}

// Narrowing violations: a child may neither add a resource nor escalate a verb.
func TestDecide_NarrowingViolationsBlockedAtChild(t *testing.T) {
	base := []Delegation{
		{ID: "R", Delegator: "root", Delegatee: "mid",
			Scopes: []Scope{{Resource: "db", Verb: VerbWrite}}, NotBefore: t0, ExpiresAt: t10},
	}

	escalation := append(base, Delegation{
		ID: "X", Delegator: "mid", Delegatee: "leaf", ParentID: "R",
		Scopes: []Scope{{Resource: "db", Verb: VerbAdmin}}, NotBefore: t0, ExpiresAt: t10,
	})
	eng, err := NewEngine(escalation, nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec := eng.Decide(Action{Delegatee: "leaf", At: t9})
	if dec.Allowed || dec.Paths[0].BlockedAt != "X" || dec.Paths[0].Reason != ReasonVerbEscalation {
		t.Fatalf("verb escalation must block at X: %+v", dec.Paths)
	}

	added := []Delegation{
		base[0],
		{ID: "Y", Delegator: "mid", Delegatee: "leaf", ParentID: "R",
			Scopes: []Scope{{Resource: "secret", Verb: VerbRead}}, NotBefore: t0, ExpiresAt: t10},
	}
	eng, err = NewEngine(added, nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec = eng.Decide(Action{Delegatee: "leaf", At: t9})
	if dec.Allowed || dec.Paths[0].BlockedAt != "Y" || dec.Paths[0].Reason != ReasonScopeNarrowing {
		t.Fatalf("added resource must block at Y: %+v", dec.Paths)
	}
}

// Static depth rule blocks at the node that exceeds the limit.
func TestDecide_MaxChainDepth(t *testing.T) {
	eng, err := NewEngine(fixture(t), nil, Rules{MaxChainDepth: 2})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec := eng.Decide(Action{
		Delegatee: "runner",
		Scopes:    []Scope{{Resource: "billing", Verb: VerbRead}},
		At:        t9,
	})
	if dec.Allowed {
		t.Fatal("depth limit must deny the 3-delegation chain")
	}
	for _, bc := range dec.BlockedChains {
		if bc.Reason != ReasonDepthExceeded || bc.BlockedAt != "C" {
			t.Fatalf("depth block should point at C, got %s/%s", bc.BlockedAt, bc.Reason)
		}
	}
}

// Static scope allow-list blocks a chain introducing a disallowed resource.
func TestDecide_AllowedScopesRule(t *testing.T) {
	dels := append(fixture(t), Delegation{
		ID: "F", Delegator: "root", Delegatee: "curious",
		Scopes: []Scope{{Resource: "forbidden", Verb: VerbRead}}, NotBefore: t0, ExpiresAt: t10,
	})
	eng, err := NewEngine(dels, nil, Rules{
		AllowedScopes: map[string]bool{"billing": true, "storage": true},
	})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec := eng.Decide(Action{
		Delegatee: "curious",
		Scopes:    []Scope{{Resource: "forbidden", Verb: VerbRead}},
		At:        t9,
	})
	if dec.Allowed || dec.Paths[0].BlockedAt != "F" || dec.Paths[0].Reason != ReasonUnknownScope {
		t.Fatalf("allow-list violation must block at F: %+v", dec.Paths)
	}
}

// A delegatee with no delegation gets an explicit, inspectable rejection.
func TestDecide_NoDelegation(t *testing.T) {
	eng, err := NewEngine(fixture(t), nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec := eng.Decide(Action{Delegatee: "nobody", At: t9})
	if dec.Allowed || len(dec.Paths) != 1 || dec.Paths[0].Reason != ReasonNoDelegation {
		t.Fatalf("expected no-delegation rejection, got %+v", dec)
	}
}

// Cyclic parent links are detected per chain instead of looping forever.
func TestDecide_ChainCycle(t *testing.T) {
	dels := []Delegation{
		{ID: "X", Delegator: "a", Delegatee: "b", ParentID: "Y",
			Scopes: []Scope{{Resource: "db", Verb: VerbRead}}, NotBefore: t0, ExpiresAt: t10},
		{ID: "Y", Delegator: "b", Delegatee: "c", ParentID: "X",
			Scopes: []Scope{{Resource: "db", Verb: VerbRead}}, NotBefore: t0, ExpiresAt: t10},
	}
	eng, err := NewEngine(dels, nil, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec := eng.Decide(Action{Delegatee: "c", At: t9})
	if dec.Allowed || dec.Paths[0].Reason != ReasonCycle {
		t.Fatalf("cycle must be reported, got %+v", dec.Paths)
	}
}

// Construction-time validation rejects malformed inputs before any decision.
func TestNewEngine_ValidationErrors(t *testing.T) {
	if _, err := NewEngine([]Delegation{
		{ID: "S", Delegator: "r", Delegatee: "d"},
		{ID: "S", Delegator: "r", Delegatee: "e"},
	}, nil, Rules{}); err == nil {
		t.Fatal("duplicate delegation id must be rejected")
	}
	if _, err := NewEngine([]Delegation{
		{ID: "S", Delegator: "r", Delegatee: "d", ParentID: "ghost"},
	}, nil, Rules{}); err == nil {
		t.Fatal("broken parent link must be rejected")
	}
	if _, err := NewEngine([]Delegation{
		{ID: "S", Delegator: "r", Delegatee: "d"},
	}, []Revocation{{DelegationID: "ghost"}}, Rules{}); err == nil {
		t.Fatal("revocation of unknown delegation must be rejected")
	}
	if _, err := NewEngine([]Delegation{
		{ID: "S", Delegator: "r", Delegatee: "d", NotBefore: t10, ExpiresAt: t0},
	}, nil, Rules{}); err == nil {
		t.Fatal("inverted validity window must be rejected")
	}
}

// Every path of a multi-chain delegatee is evaluated independently, including
// one valid, one insufficient and one blocked chain in the same decision.
func TestDecide_MixedPathOutcomesInOneDecision(t *testing.T) {
	dels := fixture(t)
	// Third independent chain for runner: blocked by revocation.
	dels = append(dels,
		Delegation{ID: "G", Delegator: "third-root", Delegatee: "runner",
			Scopes:    []Scope{{Resource: "billing", Verb: VerbRead}, {Resource: "storage", Verb: VerbRead}},
			NotBefore: t0, ExpiresAt: t10},
	)
	eng, err := NewEngine(dels, []Revocation{{DelegationID: "G", At: t0}}, Rules{})
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	dec := eng.Decide(Action{
		Delegatee: "runner",
		Scopes:    []Scope{{Resource: "billing", Verb: VerbRead}},
		At:        t9,
	})
	if !dec.Allowed {
		t.Fatal("intact A-B-C chain should still authorize despite other chains")
	}
	statuses := map[string]PathStatus{}
	for _, p := range dec.Paths {
		statuses[p.Chain[len(p.Chain)-1]] = p.Status
	}
	if statuses["C"] != PathValid || statuses["E"] != PathInsufficient || statuses["G"] != PathBlocked {
		t.Fatalf("mixed outcomes wrong: %+v", statuses)
	}
	if len(dec.BlockedChains) != 1 || dec.BlockedChains[0].BlockedAt != "G" {
		t.Fatalf("only G should appear as blocked: %+v", dec.BlockedChains)
	}
}
