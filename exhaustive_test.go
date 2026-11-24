package graphwalk

import (
	"fmt"
	"math/rand"
	"testing"
)

func oracleMatch(e Expr, labels []string) bool {
	switch x := e.(type) {
	case *litExpr:
		return len(labels) == 1 && labels[0] == x.Label
	case *epsExpr:
		return len(labels) == 0
	case *seqExpr:
		return oracleSeq(x.Items, labels)
	case *altExpr:
		for _, branch := range x.Items {
			if oracleMatch(branch, labels) {
				return true
			}
		}
		return false
	case *starExpr:
		if len(labels) == 0 {
			return true
		}
		for n := 1; n <= len(labels); n++ {
			if oracleMatch(x.X, labels[:n]) && oracleMatch(&starExpr{X: x.X}, labels[n:]) {
				return true
			}
		}
		return false
	case *repeatExpr:
		if x.expanded != nil {
			return oracleMatch(x.expanded, labels)
		}
		return oracleMatch(&starExpr{X: x.X}, labels)
	default:
		return false
	}
}

func oracleSeq(items []Expr, labels []string) bool {
	if len(items) == 0 {
		return len(labels) == 0
	}
	if len(items) == 1 {
		return oracleMatch(items[0], labels)
	}
	first := items[0]
	rest := items[1:]
	for n := 0; n <= len(labels); n++ {
		if oracleMatch(first, labels[:n]) && oracleSeq(rest, labels[n:]) {
			return true
		}
	}
	return false
}

func TestExhaustiveSmallGraphsTopK(t *testing.T) {
	expressions := []string{
		"a(b|c)*",
		"(a|b)*c?",
		"a(a|b){0,3}",
		"(ab|ac|b)*",
		"ε|a(b|c)",
	}
	for seed := int64(1); seed <= 40; seed++ {
		t.Run(fmt.Sprintf("seed-%d", seed), func(t *testing.T) {
			rng := rand.New(rand.NewSource(seed))
			vertexCount := 3 + rng.Intn(2)
			vertices := make([]Vertex, vertexCount)
			for i := range vertices {
				vertices[i] = Vertex(fmt.Sprintf("v%d", i))
			}
			source := vertices[0]
			target := vertices[rng.Intn(vertexCount)]
			edgeCount := 7 + rng.Intn(3)
			edges := make([]Edge, 0, edgeCount)
			out := map[Vertex][]Edge{}
			labels := []string{"a", "b", "c"}
			for id := EdgeID(1); id <= EdgeID(edgeCount); id++ {
				e := Edge{
					ID:    id,
					From:  vertices[rng.Intn(vertexCount)],
					To:    vertices[rng.Intn(vertexCount)],
					Label: labels[rng.Intn(len(labels))],
					Cost:  int64(rng.Intn(4)),
				}
				// Force some cheap reachable structure and occasional zero cycles.
				if id == 1 {
					e.From = source
					e.Label = "a"
					e.Cost = int64(rng.Intn(2))
				}
				if id == 2 && seed%3 == 0 {
					e.From = source
					e.To = source
					e.Label = "a"
					e.Cost = 0
				}
				edges = append(edges, e)
				out[e.From] = append(out[e.From], e)
			}

			exprText := expressions[int(seed)%len(expressions)]
			expr := mustParse(t, exprText)
			const maxHops = 5
			var expected []Walk
			var dfs func(v Vertex, es []Edge, ids []EdgeID, ls []string, cost int64)
			dfs = func(v Vertex, es []Edge, ids []EdgeID, ls []string, cost int64) {
				if v == target && oracleMatch(expr, ls) {
					expected = append(expected, Walk{
						Source: source, Target: target,
						Edges: append([]Edge(nil), es...), IDs: append([]EdgeID(nil), ids...),
						Labels: append([]string(nil), ls...), Cost: cost, Hops: len(ids),
					})
				}
				if len(ids) == maxHops {
					return
				}
				for _, e := range out[v] {
					dfs(e.To, append(es, e), append(ids, e.ID), append(ls, e.Label), cost+e.Cost)
				}
			}
			dfs(source, nil, nil, nil, 0)
			expected = SortWalks(expected)

			reader := NewMemoryReader()
			bySource := map[Vertex][]Edge{}
			for _, e := range edges {
				bySource[e.From] = append(bySource[e.From], e)
			}
			var pages []PageInfo
			for v, es := range bySource {
				minCost := es[0].Cost
				minLabel, maxLabel := es[0].Label, es[0].Label
				for _, e := range es {
					if e.Cost < minCost {
						minCost = e.Cost
					}
					if e.Label < minLabel {
						minLabel = e.Label
					}
					if e.Label > maxLabel {
						maxLabel = e.Label
					}
				}
				info := PageInfo{
					ID:       PageID("page-" + string(v)),
					Version:  "v1",
					From:     v,
					MinLabel: minLabel,
					MaxLabel: maxLabel,
					MinCost:  minCost,
				}
				pages = append(pages, info)
				reader.Put(info, es)
			}
			cat := NewCatalog("exhaustive-v1", pages...)
			k := 1 + int(seed%12)
			q := Query{Source: source, Target: target, Expr: expr, MaxHops: maxHops}
			c, err := NewCursor(q, k, cat, reader.ReadPage)
			if err != nil {
				t.Fatal(err)
			}
			got := collect(t, c, k)
			wantCount := k
			if len(expected) < wantCount {
				wantCount = len(expected)
			}
			if len(got) != wantCount {
				t.Fatalf("expr=%q %s->%s got %d walks %v, want %d; expected prefix %v",
					exprText, source, target, len(got), idSequences(got), wantCount, idSequences(expected[:wantCount]))
			}
			for i := 0; i < wantCount; i++ {
				if !walksEqual(got[i], expected[i]) {
					t.Fatalf("rank %d got %v (cost=%d,hops=%d), want %v (cost=%d,hops=%d)",
						i+1, got[i].IDs, got[i].Cost, got[i].Hops,
						expected[i].IDs, expected[i].Cost, expected[i].Hops)
				}
				if got[i].Cost != expected[i].Cost || got[i].Hops != expected[i].Hops {
					t.Fatalf("rank %d key fields mismatch", i+1)
				}
			}

			// Every emitted frontier certificate must have a lower bound no
			// greater than the following result, when a following result exists.
			proof := c.Proof()
			for _, cert := range proof.Certificates {
				if cert.Rank < len(got) && cert.HasFrontierBound && cert.Rank < wantCount {
					next := expected[cert.Rank] // rank cert.Rank is zero-based next rank
					if CompareKey(*cert.FrontierLowerBound, next.Key()) > 0 {
						t.Fatalf("rank %d frontier %v exceeds next %v", cert.Rank, *cert.FrontierLowerBound, next.Key())
					}
				}
			}
		})
	}
}

func idSequences(ws []Walk) [][]EdgeID {
	out := make([][]EdgeID, len(ws))
	for i, w := range ws {
		out[i] = append([]EdgeID(nil), w.IDs...)
	}
	return out
}

func TestExpressionParserRoundTrip(t *testing.T) {
	for _, text := range []string{"", "ε", "a", "a(b|c)*", `("x"|"y"){2,3}`, "a+", "a?"} {
		e, err := ParseExpr(text)
		if err != nil {
			t.Fatalf("ParseExpr(%q): %v", text, err)
		}
		reparsed, err := ParseExpr(e.String())
		if err != nil {
			t.Fatalf("canonical %q from %q did not parse: %v", e.String(), text, err)
		}
		for _, labels := range [][]string{{}, {"a"}, {"a", "b"}, {"x", "y", "y"}} {
			if MatchLabels(e, labels) != MatchLabels(reparsed, labels) {
				t.Fatalf("roundtrip mismatch for %q -> %q on %v", text, e.String(), labels)
			}
		}
	}
}
