// Package delegationguard implements service-identity delegation decisioning.
//
// A delegation record states that one service identity (Delegator) delegated
// part of its authority to another identity (Delegatee). Every child
// delegation must stay within the effective authority of its parent: narrower
// resources, no stronger verbs and no longer lifetime. Revocation of an
// ancestor immediately invalidates every chain passing through that ancestor.
//
// Permissions observed on different delegation chains are never combined: an
// action is authorized only when at least one complete chain independently
// covers every requested scope.
package delegationguard

import (
	"errors"
	"fmt"
	"sort"
	"time"
)

// Verb is the action class attached to a scope. Verbs are ordered: each verb
// implies the strictly weaker verbs below it (Admin implies Write and Read;
// Write implies Read).
type Verb string

const (
	VerbRead  Verb = "read"
	VerbWrite Verb = "write"
	VerbAdmin Verb = "admin"
)

// verbRank maps a verb to its strength. Unknown verbs have rank 0.
func verbRank(v Verb) int {
	switch v {
	case VerbRead:
		return 1
	case VerbWrite:
		return 2
	case VerbAdmin:
		return 3
	default:
		return 0
	}
}

// Scope is one resource permission: a resource paired with a verb.
type Scope struct {
	Resource string
	Verb     Verb
}

// String renders a scope for reports and test diagnostics.
func (s Scope) String() string {
	return s.Resource + ":" + string(s.Verb)
}

// Delegation is an already-authenticated delegation record.
type Delegation struct {
	ID        string
	Delegator string
	Delegatee string
	// ParentID links to the delegation whose authority this one narrows.
	// An empty ParentID marks a root delegation issued out of band.
	ParentID  string
	Scopes    []Scope
	NotBefore time.Time
	ExpiresAt time.Time
}

// Revocation invalidates a delegation (and, transitively, every descendant).
type Revocation struct {
	DelegationID string
	At           time.Time
	Reason       string
}

// Rules are the static scope rules applied while walking chains.
type Rules struct {
	// MaxChainDepth limits the number of delegations on a single chain
	// (root counts as depth 1). Zero means unlimited.
	MaxChainDepth int
	// AllowedScopes restricts which resource identifiers may appear on a
	// chain. A scope whose resource is not allowed breaks the chain at the
	// delegation that introduced it.
	AllowedScopes map[string]bool
}

// Action is the operation whose authorization is being decided.
type Action struct {
	Delegatee string
	Scopes    []Scope
	At        time.Time
}

// Reason identifies why a delegation chain cannot serve the action.
type Reason string

const (
	ReasonNone            Reason = ""
	ReasonDepthExceeded   Reason = "depth_exceeded"
	ReasonRevoked         Reason = "revoked"
	ReasonNotYetValid     Reason = "not_yet_valid"
	ReasonExpired         Reason = "expired"
	ReasonScopeNarrowing  Reason = "scope_narrowing_violation"
	ReasonVerbEscalation  Reason = "verb_escalation_violation"
	ReasonUnknownScope    Reason = "unknown_scope"
	ReasonCycle           Reason = "chain_cycle"
	ReasonBrokenParent    Reason = "broken_parent_link"
	ReasonScopeNotCovered Reason = "scope_not_covered"
	ReasonNoDelegation    Reason = "no_delegation_for_delegatee"
)

// PathStatus is the per-chain outcome.
type PathStatus string

const (
	// PathValid means the chain itself is intact at decision time.
	PathValid PathStatus = "valid"
	// PathBlocked means the chain is structurally or temporally invalid
	// (revoked, expired, rule violation, ...).
	PathBlocked PathStatus = "blocked"
	// PathInsufficient means the chain is intact but does not cover every
	// requested scope. It remains a legal path for narrower actions.
	PathInsufficient PathStatus = "insufficient"
)

// PathEvaluation reports what happened to one delegation chain.
type PathEvaluation struct {
	// Chain lists delegation IDs from root to leaf.
	Chain []string
	// EffectiveScopes is the intersection of scopes along the chain, i.e.
	// the authority this chain actually carries at decision time.
	EffectiveScopes []Scope
	Status          PathStatus
	// BlockedAt is the delegation ID where the chain broke, or the first
	// delegation at which a requested scope could not be satisfied.
	BlockedAt string
	Reason    Reason
	Detail    string
	// MissingScopes lists requested scopes the chain cannot satisfy.
	MissingScopes []Scope
}

// Decision is the result of evaluating an action.
type Decision struct {
	Allowed bool
	At      time.Time
	// Paths evaluates every delegation chain held by the target delegatee.
	Paths []PathEvaluation
	// ValidPath is the first intact chain covering all requested scopes,
	// present when Allowed is true.
	ValidPath *PathEvaluation
	// BlockedChains pinpoints, for every unusable chain, the delegation ID
	// that blocks it so callers can pick a still-legal independent path.
	BlockedChains []BlockedChain
}

// BlockedChain pairs a chain with the exact position that blocks it.
type BlockedChain struct {
	Chain     []string
	BlockedAt string
	Reason    Reason
	Detail    string
}

// Engine holds pre-indexed delegation data and answers authorization queries.
type Engine struct {
	delegations map[string]*Delegation
	children    map[string][]string
	revokedAt   map[string]time.Time
	rules       Rules
}

// NewEngine validates and indexes the inputs. Revocations are consulted at
// decision time, so a revocation created after the engine is built still takes
// effect on the next Decide call once supplied here at construction time; use
// NewEngine with the current revocation set for each decision batch.
func NewEngine(delegations []Delegation, revocations []Revocation, rules Rules) (*Engine, error) {
	e := &Engine{
		delegations: make(map[string]*Delegation),
		children:    make(map[string][]string),
		revokedAt:   make(map[string]time.Time),
		rules:       rules,
	}

	seen := make(map[string]bool, len(delegations))
	for i := range delegations {
		d := delegations[i]
		if d.ID == "" {
			return nil, errors.New("delegation with empty id")
		}
		if seen[d.ID] {
			return nil, fmt.Errorf("duplicate delegation id %q", d.ID)
		}
		seen[d.ID] = true
		if d.Delegatee == "" || d.Delegator == "" {
			return nil, fmt.Errorf("delegation %q missing delegator or delegatee", d.ID)
		}
		if !d.ExpiresAt.IsZero() && !d.NotBefore.IsZero() && d.ExpiresAt.Before(d.NotBefore) {
			return nil, fmt.Errorf("delegation %q expires before it becomes valid", d.ID)
		}
		dc := d
		e.delegations[d.ID] = &dc
	}

	// Parent links must resolve; roots use an empty parent.
	for _, d := range e.delegations {
		if d.ParentID != "" {
			if _, ok := e.delegations[d.ParentID]; !ok {
				return nil, fmt.Errorf("delegation %q references unknown parent %q", d.ID, d.ParentID)
			}
			e.children[d.ParentID] = append(e.children[d.ParentID], d.ID)
		}
	}

	// Child adjacency sorted for deterministic traversal.
	for id := range e.children {
		sort.Strings(e.children[id])
	}

	for _, r := range revocations {
		if r.DelegationID == "" {
			return nil, errors.New("revocation with empty delegation id")
		}
		if _, ok := e.delegations[r.DelegationID]; !ok {
			return nil, fmt.Errorf("revocation references unknown delegation %q", r.DelegationID)
		}
		// Multiple revocations of the same node: keep the earliest one.
		if prev, ok := e.revokedAt[r.DelegationID]; !ok || r.At.Before(prev) {
			e.revokedAt[r.DelegationID] = r.At
		}
	}

	return e, nil
}

// Decide evaluates the action against every independent delegation chain of
// the target delegatee. It never combines scopes from different chains.
func (e *Engine) Decide(act Action) Decision {
	dec := Decision{At: act.At}

	leaves := make([]string, 0)
	for _, d := range e.delegations {
		if d.Delegatee == act.Delegatee {
			leaves = append(leaves, d.ID)
		}
	}
	sort.Strings(leaves)

	if len(leaves) == 0 {
		dec.Paths = []PathEvaluation{{
			Status: PathBlocked,
			Reason: ReasonNoDelegation,
			Detail: fmt.Sprintf("delegatee %q holds no delegation", act.Delegatee),
		}}
		return dec
	}

	for _, leafID := range leaves {
		chain, chainOK := e.buildChain(leafID)
		if !chainOK {
			// Cycle or broken link detected while assembling the chain.
			dec.Paths = append(dec.Paths, PathEvaluation{
				Chain:  chain,
				Status: PathBlocked,
				Reason: ReasonCycle,
				Detail: "delegation graph contains a cycle reachable from " + leafID,
			})
			continue
		}
		dec.Paths = append(dec.Paths, e.evaluateChain(chain, act))
	}

	for i := range dec.Paths {
		p := &dec.Paths[i]
		switch p.Status {
		case PathBlocked:
			dec.BlockedChains = append(dec.BlockedChains, BlockedChain{
				Chain:     append([]string(nil), p.Chain...),
				BlockedAt: p.BlockedAt,
				Reason:    p.Reason,
				Detail:    p.Detail,
			})
		case PathValid:
			if dec.ValidPath == nil {
				cp := *p
				dec.ValidPath = &cp
				dec.Allowed = true
			}
		}
	}
	return dec
}

// buildChain walks parent links from a leaf to its root. The returned chain is
// ordered root -> leaf. A cycle yields chainOK == false.
func (e *Engine) buildChain(leafID string) ([]string, bool) {
	var reversed []string
	visited := map[string]bool{}
	cur := leafID
	for cur != "" {
		if visited[cur] {
			reversed = append(reversed, cur)
			// Reverse the visited prefix for a readable diagnostic chain.
			sort.Sort(sort.Reverse(sort.StringSlice(reversed)))
			return reversed, false
		}
		visited[cur] = true
		d, ok := e.delegations[cur]
		if !ok {
			return reversed, false
		}
		reversed = append(reversed, cur)
		cur = d.ParentID
	}
	chain := make([]string, len(reversed))
	for i, id := range reversed {
		chain[len(reversed)-1-i] = id
	}
	return chain, true
}

// evaluateChain walks one chain root -> leaf, accumulating the effective
// (intersected) authority and stopping at the first blocking position.
func (e *Engine) evaluateChain(chain []string, act Action) PathEvaluation {
	pe := PathEvaluation{Chain: append([]string(nil), chain...)}

	// parentEffective holds scopes granted by the ancestor immediately above
	// the current delegation.
	var parentEffective []Scope

	for depth, id := range chain {
		d := e.delegations[id]

		// 1. Static depth rule.
		if e.rules.MaxChainDepth > 0 && depth+1 > e.rules.MaxChainDepth {
			pe.BlockedAt = id
			pe.Reason = ReasonDepthExceeded
			pe.Detail = fmt.Sprintf("chain depth %d exceeds limit %d", depth+1, e.rules.MaxChainDepth)
			pe.Status = PathBlocked
			return pe
		}

		// 2. Revocation: an ancestor revocation blocks here immediately.
		if at, ok := e.revokedAt[id]; ok && !at.After(act.At) {
			pe.BlockedAt = id
			pe.Reason = ReasonRevoked
			pe.Detail = fmt.Sprintf("delegation %q revoked at %s", id, at.Format(time.RFC3339))
			pe.Status = PathBlocked
			return pe
		}

		// 3. Temporal validity.
		if !d.NotBefore.IsZero() && act.At.Before(d.NotBefore) {
			pe.BlockedAt = id
			pe.Reason = ReasonNotYetValid
			pe.Detail = fmt.Sprintf("delegation %q not valid before %s", id, d.NotBefore.Format(time.RFC3339))
			pe.Status = PathBlocked
			return pe
		}
		if !d.ExpiresAt.IsZero() && act.At.After(d.ExpiresAt) {
			pe.BlockedAt = id
			pe.Reason = ReasonExpired
			pe.Detail = fmt.Sprintf("delegation %q expired at %s", id, d.ExpiresAt.Format(time.RFC3339))
			pe.Status = PathBlocked
			return pe
		}

		// 4. Static resource allow-list.
		for _, s := range d.Scopes {
			if len(e.rules.AllowedScopes) > 0 && !e.rules.AllowedScopes[s.Resource] {
				pe.BlockedAt = id
				pe.Reason = ReasonUnknownScope
				pe.Detail = fmt.Sprintf("delegation %q introduces disallowed resource %q", id, s.Resource)
				pe.Status = PathBlocked
				return pe
			}
		}

		// 5. Narrowing: every child scope must be covered by the parent's
		// effective scopes with an equal-or-weaker verb. The root has no
		// parent and simply establishes authority.
		if depth > 0 {
			for _, s := range d.Scopes {
				grant, ok := findScope(parentEffective, s.Resource)
				if !ok {
					pe.BlockedAt = id
					pe.Reason = ReasonScopeNarrowing
					pe.Detail = fmt.Sprintf("delegation %q adds resource %q absent from parent authority", id, s.Resource)
					pe.Status = PathBlocked
					return pe
				}
				if verbRank(s.Verb) > verbRank(grant.Verb) {
					pe.BlockedAt = id
					pe.Reason = ReasonVerbEscalation
					pe.Detail = fmt.Sprintf("delegation %q escalates verb on %q from %s to %s",
						id, s.Resource, grant.Verb, s.Verb)
					pe.Status = PathBlocked
					return pe
				}
			}
		}

		// 6. Effective authority is the intersection with the parent.
		if depth == 0 {
			parentEffective = cloneScopes(d.Scopes)
		} else {
			parentEffective = intersectScopes(parentEffective, d.Scopes)
		}
	}

	pe.EffectiveScopes = parentEffective

	// 7. Request coverage: every requested scope must be covered by this
	// single chain; nothing is borrowed from sibling chains.
	var missing []Scope
	var firstDrop string
	for _, req := range act.Scopes {
		grant, ok := findScope(parentEffective, req.Resource)
		if !ok || verbRank(grant.Verb) < verbRank(req.Verb) {
			missing = append(missing, req)
			// Locate the shallowest delegation on which this scope is absent.
			drop := e.firstScopeDrop(chain, req)
			if drop != "" && (firstDrop == "" || chainIndex(chain, drop) < chainIndex(chain, firstDrop)) {
				firstDrop = drop
			}
		}
	}
	if len(missing) > 0 {
		pe.Status = PathInsufficient
		pe.Reason = ReasonScopeNotCovered
		pe.BlockedAt = firstDrop
		pe.MissingScopes = missing
		pe.Detail = fmt.Sprintf("chain covers %d of %d requested scopes", len(act.Scopes)-len(missing), len(act.Scopes))
		return pe
	}

	pe.Status = PathValid
	return pe
}

// firstScopeDrop returns the shallowest delegation ID on the chain whose
// effective authority no longer contains the requested scope (resource missing
// or verb too weak). It returns "" when the root never had the scope; in that
// case callers report the root as the blocking position.
func (e *Engine) firstScopeDrop(chain []string, req Scope) string {
	var had bool
	for _, id := range chain {
		d := e.delegations[id]
		grant, ok := findScope(d.Scopes, req.Resource)
		if !ok || verbRank(grant.Verb) < verbRank(req.Verb) {
			if had {
				return id
			}
			continue
		}
		had = true
	}
	if !had {
		return chain[0]
	}
	return ""
}

func chainIndex(chain []string, id string) int {
	for i, c := range chain {
		if c == id {
			return i
		}
	}
	return len(chain)
}

func findScope(scopes []Scope, resource string) (Scope, bool) {
	for _, s := range scopes {
		if s.Resource == resource {
			return s, true
		}
	}
	return Scope{}, false
}

func cloneScopes(in []Scope) []Scope {
	out := make([]Scope, len(in))
	copy(out, in)
	return out
}

// intersectScopes keeps only scopes present in both sets, downgrading the verb
// to the weaker of the two.
func intersectScopes(parent, child []Scope) []Scope {
	out := make([]Scope, 0, len(child))
	for _, cs := range child {
		ps, ok := findScope(parent, cs.Resource)
		if !ok {
			continue
		}
		v := cs.Verb
		if verbRank(ps.Verb) < verbRank(v) {
			v = ps.Verb
		}
		out = append(out, Scope{Resource: cs.Resource, Verb: v})
	}
	return out
}
