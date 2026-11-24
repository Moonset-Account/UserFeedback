// Package symbind implements symbol binding for a plugin runtime over an
// already-parsed module table.
//
// Each module carries a namespace, an ordered dependency list, and a set of
// versioned strong/weak exports. A module may also register a manifest of
// specific foreign modules (and their specific symbols) that it permits to
// be referenced across namespaces.
//
// Resolution walks the visible dependency closure in layers (BFS) following
// dependency declaration order. An explicit version request accepts only the
// same version; an unspecified version binds to the export marked default.
// Matching strong definitions win over weak ones; ties within the same
// strength class are decided by view order. Cross-namespace edges are open
// only for manifest-listed modules and only for manifest-listed symbols;
// every further hop is judged by the namespace rules of the module that
// declares the dependency. Dependency cycles terminate via a visited set.
package symbind

import "fmt"

// Export is a versioned strong/weak symbol definition offered by a module.
type Export struct {
	Symbol  string
	Version string
	Strong  bool
	// Default marks the export used when a request does not name a version.
	Default bool
}

// Module is one parsed module in the table.
type Module struct {
	Name      string
	Namespace string
	// Deps lists dependency module names in declaration order.
	Deps []string
	// Exports lists the module's symbol definitions.
	Exports []Export
	// Allow is the cross-namespace manifest: it maps a foreign dependency
	// module name to the symbols this module permits to be referenced
	// across the namespace boundary. Presence of the key opens the edge;
	// the value restricts which of that module's exports are visible.
	Allow map[string][]string
}

// Table is a parsed module table keyed by module name.
type Table struct {
	mods map[string]Module
}

// NewTable returns an empty module table.
func NewTable() *Table { return &Table{mods: make(map[string]Module)} }

// AddModule registers a module. Empty names and duplicates are rejected.
func (t *Table) AddModule(m Module) error {
	if m.Name == "" {
		return fmt.Errorf("symbind: module name must not be empty")
	}
	if _, ok := t.mods[m.Name]; ok {
		return fmt.Errorf("symbind: duplicate module %q", m.Name)
	}
	t.mods[m.Name] = m
	return nil
}

// Request describes one symbol binding query.
type Request struct {
	Symbol string
	// Version is the explicitly requested version. Empty means "no explicit
	// version", which binds to the export marked Default.
	Version string
}

// Resolution is the reviewable outcome of a binding query.
type Resolution struct {
	Found   bool
	Module  string   // name of the module providing the selected definition
	Export  Export   // the selected definition
	Path    []string // module chain from the requester to Module (visibility path)
	Scanned []string // every visible module, in the order it was examined
	Blocked []string // dependency edges rejected by namespace rules, "from->to"
}

// Resolve binds req.Symbol starting from module `from` and returns the
// selected definition together with the reviewable visibility path.
func (t *Table) Resolve(from string, req Request) (Resolution, error) {
	if _, ok := t.mods[from]; !ok {
		return Resolution{}, fmt.Errorf("symbind: unknown module %q", from)
	}

	// allowed == nil means every export of the module is open (same-namespace
	// reach); otherwise only the listed symbols are open.
	type item struct {
		name    string
		allowed map[string]bool
	}

	var res Resolution
	visited := map[string]bool{from: true}
	parent := map[string]string{}
	queue := []item{{name: from}}

	var best *Export
	bestMod := ""

	consider := func(mod string, e Export) {
		if best == nil || (e.Strong && !best.Strong) {
			cpy := e
			best = &cpy
			bestMod = mod
		}
	}

	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		m := t.mods[cur.name]
		res.Scanned = append(res.Scanned, cur.name)

		for _, e := range m.Exports {
			if e.Symbol != req.Symbol {
				continue
			}
			if cur.allowed != nil && !cur.allowed[e.Symbol] {
				continue
			}
			if req.Version != "" {
				if e.Version != req.Version {
					continue
				}
			} else if !e.Default {
				continue
			}
			consider(cur.name, e)
		}

		for _, depName := range m.Deps {
			if visited[depName] {
				continue
			}
			dep, ok := t.mods[depName]
			if !ok {
				return Resolution{}, fmt.Errorf("symbind: module %q depends on unknown module %q", m.Name, depName)
			}
			if dep.Namespace == m.Namespace {
				visited[depName] = true
				parent[depName] = cur.name
				queue = append(queue, item{name: depName})
				continue
			}
			// Cross-namespace edge: open only if this module's manifest
			// lists the dependency, and then only for the listed symbols.
			syms, ok := m.Allow[depName]
			if !ok {
				// Not marked visited: the module may still be reached
				// through a legitimate edge later in view order.
				res.Blocked = append(res.Blocked, m.Name+"->"+depName)
				continue
			}
			set := make(map[string]bool, len(syms))
			for _, s := range syms {
				set[s] = true
			}
			visited[depName] = true
			parent[depName] = cur.name
			queue = append(queue, item{name: depName, allowed: set})
		}
	}

	if best == nil {
		return res, nil
	}
	res.Found = true
	res.Module = bestMod
	res.Export = *best

	// Rebuild the visibility path from requester to the defining module.
	var rev []string
	for n := bestMod; ; n = parent[n] {
		rev = append(rev, n)
		if n == from {
			break
		}
	}
	for i, j := 0, len(rev)-1; i < j; i, j = i+1, j-1 {
		rev[i], rev[j] = rev[j], rev[i]
	}
	res.Path = rev
	return res, nil
}
