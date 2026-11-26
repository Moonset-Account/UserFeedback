package sharedscan_test

import (
	"context"
	"errors"
	"io"
	"reflect"
	"testing"

	ss "sharedscan"
)

func scalarInner(values ...ss.Datum) []ss.InnerRow {
	rows := make([]ss.InnerRow, 0, len(values))
	for _, v := range values {
		rows = append(rows, ss.InnerRow{Values: ss.Row{v}})
	}
	return rows
}

func evaluateOrFail(t *testing.T, reqs []ss.Request, corr ss.CorrelationFunc, chunks ...ss.Chunk) []ss.Result {
	t.Helper()

	var opts []ss.Option
	if corr != nil {
		opts = append(opts, ss.WithCorrelator(corr))
	}
	op, err := ss.NewOperator(reqs, opts...)
	if err != nil {
		t.Fatalf("NewOperator: %v", err)
	}
	for i, chunk := range chunks {
		if err := op.ConsumeChunk(chunk); err != nil {
			t.Fatalf("ConsumeChunk %d: %v", i, err)
		}
	}
	results, err := op.Finish()
	if err != nil {
		t.Fatalf("Finish: %v", err)
	}
	return results
}

func oneResult(t *testing.T, req ss.Request, inner ...ss.InnerRow) ss.Result {
	t.Helper()
	results := evaluateOrFail(t, []ss.Request{req}, nil, ss.Chunk{Rows: inner})
	return results[0]
}

func TestThreeValuedTruthTables(t *testing.T) {
	cases := []struct {
		name string
		got  ss.Truth
		want ss.Truth
	}{
		{"not true", ss.Not(ss.TruthTrue), ss.TruthFalse},
		{"not false", ss.Not(ss.TruthFalse), ss.TruthTrue},
		{"not unknown", ss.Not(ss.TruthUnknown), ss.TruthUnknown},
		{"and false dominates unknown", ss.And(ss.TruthUnknown, ss.TruthFalse), ss.TruthFalse},
		{"and unknown", ss.And(ss.TruthTrue, ss.TruthUnknown), ss.TruthUnknown},
		{"and true", ss.And(ss.TruthTrue, ss.TruthTrue), ss.TruthTrue},
		{"or true dominates unknown", ss.Or(ss.TruthUnknown, ss.TruthTrue), ss.TruthTrue},
		{"or unknown", ss.Or(ss.TruthFalse, ss.TruthUnknown), ss.TruthUnknown},
		{"or false", ss.Or(ss.TruthFalse, ss.TruthFalse), ss.TruthFalse},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.got != tc.want {
				t.Fatalf("got %s, want %s", tc.got, tc.want)
			}
		})
	}
}

func TestCompositeComparisonRulesAndNullPositions(t *testing.T) {
	cases := []struct {
		name string
		a, b ss.Row
		rule ss.Rule
		want ss.Truth
	}{
		{
			name: "SQL null field is unknown",
			a:    ss.Row{ss.Int(1), ss.NullDatum()},
			b:    ss.Row{ss.Int(1), ss.Int(2)},
			rule: ss.SQLRule,
			want: ss.TruthUnknown,
		},
		{
			name: "false field dominates unknown",
			a:    ss.Row{ss.NullDatum(), ss.Int(9)},
			b:    ss.Row{ss.Int(1), ss.Int(2)},
			rule: ss.SQLRule,
			want: ss.TruthFalse,
		},
		{
			name: "SQL null equals null is unknown",
			a:    ss.Row{ss.Int(1), ss.NullDatum()},
			b:    ss.Row{ss.Int(1), ss.NullDatum()},
			rule: ss.SQLRule,
			want: ss.TruthUnknown,
		},
		{
			name: "null-aware null equals null is true",
			a:    ss.Row{ss.Int(1), ss.NullDatum()},
			b:    ss.Row{ss.Int(1), ss.NullDatum()},
			rule: ss.NullAwareRule,
			want: ss.TruthTrue,
		},
		{
			name: "null-aware null versus value is false",
			a:    ss.Row{ss.NullDatum()},
			b:    ss.Row{ss.Int(1)},
			rule: ss.NullAwareRule,
			want: ss.TruthFalse,
		},
		{
			name: "case insensitive text match",
			a:    ss.Row{ss.StringDatum("A")},
			b:    ss.Row{ss.StringDatum("a")},
			rule: ss.CaseInsensitiveRule,
			want: ss.TruthTrue,
		},
		{
			name: "case sensitive text mismatch",
			a:    ss.Row{ss.StringDatum("A")},
			b:    ss.Row{ss.StringDatum("a")},
			rule: ss.SQLRule,
			want: ss.TruthFalse,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ss.CompareRows(tc.a, tc.b, tc.rule)
			if err != nil {
				t.Fatalf("CompareRows: %v", err)
			}
			if got != tc.want {
				t.Fatalf("got %s, want %s", got, tc.want)
			}
		})
	}

	if got := (ss.Row{ss.Int(1), ss.NullDatum()}).NullBitmap(); got != "01" {
		t.Fatalf("first null bitmap = %q, want 01", got)
	}
	if got := (ss.Row{ss.NullDatum(), ss.Int(1)}).NullBitmap(); got != "10" {
		t.Fatalf("second null bitmap = %q, want 10", got)
	}
}

func TestInAndNotInThreeValuedMatrix(t *testing.T) {
	cases := []struct {
		name        string
		expr        ss.Expr
		left        ss.Row
		rule        ss.Rule
		inner       []ss.InnerRow
		want        ss.Truth
		wantUnknown bool
	}{
		{"null left empty IN is false", ss.ExprIn, ss.Row{ss.NullDatum()}, ss.SQLRule, nil, ss.TruthFalse, false},
		{"null left empty NOT IN is true", ss.ExprNotIn, ss.Row{ss.NullDatum()}, ss.SQLRule, nil, ss.TruthTrue, false},
		{"value left empty IN is false", ss.ExprIn, ss.Row{ss.Int(1)}, ss.SQLRule, nil, ss.TruthFalse, false},
		{"value left empty NOT IN is true", ss.ExprNotIn, ss.Row{ss.Int(1)}, ss.SQLRule, nil, ss.TruthTrue, false},

		{"null left against values IN unknown", ss.ExprIn, ss.Row{ss.NullDatum()}, ss.SQLRule, scalarInner(ss.Int(1), ss.Int(2)), ss.TruthUnknown, true},
		{"null left against values NOT IN unknown", ss.ExprNotIn, ss.Row{ss.NullDatum()}, ss.SQLRule, scalarInner(ss.Int(1), ss.Int(2)), ss.TruthUnknown, true},
		{"null left against inner null IN unknown", ss.ExprIn, ss.Row{ss.NullDatum()}, ss.SQLRule, scalarInner(ss.NullDatum()), ss.TruthUnknown, true},

		{"nonmatching values IN false", ss.ExprIn, ss.Row{ss.Int(3)}, ss.SQLRule, scalarInner(ss.Int(1), ss.Int(2)), ss.TruthFalse, false},
		{"nonmatching values NOT IN true", ss.ExprNotIn, ss.Row{ss.Int(3)}, ss.SQLRule, scalarInner(ss.Int(1), ss.Int(2)), ss.TruthTrue, false},

		{"null plus nonmatch makes IN unknown", ss.ExprIn, ss.Row{ss.Int(3)}, ss.SQLRule, scalarInner(ss.NullDatum(), ss.Int(1)), ss.TruthUnknown, true},
		{"null plus nonmatch makes NOT IN unknown", ss.ExprNotIn, ss.Row{ss.Int(3)}, ss.SQLRule, scalarInner(ss.NullDatum(), ss.Int(1)), ss.TruthUnknown, true},

		{"exact match among nulls makes IN true", ss.ExprIn, ss.Row{ss.Int(1)}, ss.SQLRule, scalarInner(ss.NullDatum(), ss.Int(1)), ss.TruthTrue, true},
		{"exact match among nulls makes NOT IN false", ss.ExprNotIn, ss.Row{ss.Int(1)}, ss.SQLRule, scalarInner(ss.NullDatum(), ss.Int(1)), ss.TruthFalse, true},

		{"later match resolves earlier null for NOT IN", ss.ExprNotIn, ss.Row{ss.Int(1)}, ss.SQLRule, scalarInner(ss.NullDatum(), ss.Int(1)), ss.TruthFalse, true},

		{"null-aware null match IN true", ss.ExprIn, ss.Row{ss.NullDatum()}, ss.NullAwareRule, scalarInner(ss.NullDatum()), ss.TruthTrue, false},
		{"null-aware null match NOT IN false", ss.ExprNotIn, ss.Row{ss.NullDatum()}, ss.NullAwareRule, scalarInner(ss.NullDatum()), ss.TruthFalse, false},
		{"null-aware null versus value IN false", ss.ExprIn, ss.Row{ss.NullDatum()}, ss.NullAwareRule, scalarInner(ss.Int(1)), ss.TruthFalse, false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			res := oneResult(t, ss.Request{
				ID:   tc.name,
				Expr: tc.expr,
				Left: tc.left,
				Rule: tc.rule,
			}, tc.inner...)

			if res.Value != tc.want {
				t.Fatalf("value = %s, want %s", res.Value, tc.want)
			}
			if res.SawUnknownComparison != tc.wantUnknown {
				t.Fatalf("SawUnknownComparison = %v, want %v", res.SawUnknownComparison, tc.wantUnknown)
			}
		})
	}
}

func TestExistsIgnoresNullColumnsAfterCandidate(t *testing.T) {
	res := oneResult(t, ss.Request{ID: "exists", Expr: ss.ExprExists},
		ss.InnerRow{Values: ss.Row{ss.NullDatum()}},
		ss.InnerRow{Values: ss.Row{ss.Int(1)}},
	)
	if res.Value != ss.TruthTrue {
		t.Fatalf("EXISTS over rows with null columns = %s, want TRUE", res.Value)
	}
	// EXISTS terminates on the first candidate, so only the first row is
	// counted; its NULL projected column does not affect existence.
	if res.CandidateRows != 1 {
		t.Fatalf("CandidateRows = %d, want 1", res.CandidateRows)
	}
	if !res.EarlyTerminated {
		t.Fatal("EXISTS should early-terminate on its first candidate")
	}

	empty := oneResult(t, ss.Request{ID: "empty-exists", Expr: ss.ExprExists})
	if empty.Value != ss.TruthFalse {
		t.Fatalf("empty EXISTS = %s, want FALSE", empty.Value)
	}
}

func TestCorrelationNullSemantics(t *testing.T) {
	param := ss.RowOf(int64(1), nil)
	row := ss.InnerRow{
		Key:    ss.RowOf(int64(1), nil),
		Values: ss.RowOf(int64(99)),
	}
	reqs := []ss.Request{
		{ID: "sql-null-param", Expr: ss.ExprExists, Param: param, Rule: ss.SQLRule},
		{ID: "null-aware-param", Expr: ss.ExprExists, Param: param, Rule: ss.NullAwareRule},
	}
	results := evaluateOrFail(t, reqs, ss.MatchParameter, ss.Chunk{Rows: []ss.InnerRow{row}})

	if results[0].Value != ss.TruthFalse {
		t.Fatalf("SQL NULL correlation = %s, want FALSE (UNKNOWN WHERE predicate rejects row)", results[0].Value)
	}
	if results[1].Value != ss.TruthTrue {
		t.Fatalf("null-aware correlation = %s, want TRUE", results[1].Value)
	}
	if results[0].CorrelationChecks != 1 || results[1].CorrelationChecks != 1 {
		t.Fatalf("correlation checks = %d/%d, want 1/1", results[0].CorrelationChecks, results[1].CorrelationChecks)
	}
}

func TestParameterNullPositionsAreJudgedSeparately(t *testing.T) {
	const set = "param-null-positions"
	reqs := []ss.Request{
		{
			ID:           "null-second",
			Expr:         ss.ExprExists,
			Param:        ss.RowOf(int64(1), nil),
			Rule:         ss.NullAwareRule,
			CandidateSet: set,
		},
		{
			ID:           "null-first",
			Expr:         ss.ExprExists,
			Param:        ss.RowOf(nil, int64(1)),
			Rule:         ss.NullAwareRule,
			CandidateSet: set,
		},
	}
	chunk := ss.Chunk{
		Set: set,
		Rows: []ss.InnerRow{{
			Key: ss.RowOf(int64(1), nil),
		}},
	}

	results := evaluateOrFail(t, reqs, ss.MatchParameter, chunk)
	if results[0].Value != ss.TruthTrue || results[0].CandidateRows != 1 {
		t.Fatalf("null in second position = %s candidates=%d, want TRUE/1",
			results[0].Value, results[0].CandidateRows)
	}
	if results[1].Value != ss.TruthFalse || results[1].CandidateRows != 0 {
		t.Fatalf("null in first position = %s candidates=%d, want FALSE/0",
			results[1].Value, results[1].CandidateRows)
	}
	if results[0].Fingerprint == results[1].Fingerprint {
		t.Fatal("requests with different parameter NULL positions must not share a fingerprint")
	}
}

func TestFingerprintsSeparateRulesAndNullPositions(t *testing.T) {
	base := ss.Request{
		Expr:         ss.ExprIn,
		Left:         ss.RowOf(int64(1), nil),
		Param:        ss.RowOf(int64(7), nil),
		Rule:         ss.SQLRule,
		CandidateSet: "shared",
	}
	differentRule := base
	differentRule.Rule = ss.NullAwareRule

	differentLeftNulls := base
	differentLeftNulls.Left = ss.RowOf(nil, int64(1))

	differentParamNulls := base
	differentParamNulls.Param = ss.RowOf(nil, int64(7))

	fp1, err := base.Fingerprint()
	if err != nil {
		t.Fatal(err)
	}
	for name, req := range map[string]ss.Request{
		"rule":        differentRule,
		"left nulls":  differentLeftNulls,
		"param nulls": differentParamNulls,
	} {
		fp, err := req.Fingerprint()
		if err != nil {
			t.Fatalf("%s fingerprint: %v", name, err)
		}
		if fp == fp1 {
			t.Fatalf("%s fingerprint unexpectedly equals base: %s", name, fp)
		}
	}

	key1 := ss.DependencySetID(ss.RowOf(int64(1), nil))
	key2 := ss.DependencySetID(ss.RowOf(nil, int64(1)))
	if key1 == key2 {
		t.Fatal("physical dependency keys for different NULL positions must differ")
	}
}

func TestSharedCandidateSetButSeparateRulesAndStates(t *testing.T) {
	const shared = "candidate-set/shared"
	reqs := []ss.Request{
		{ID: "sql-null", Expr: ss.ExprIn, Left: ss.Row{ss.NullDatum()}, Rule: ss.SQLRule, CandidateSet: shared},
		{ID: "null-aware", Expr: ss.ExprIn, Left: ss.Row{ss.NullDatum()}, Rule: ss.NullAwareRule, CandidateSet: shared},
		{ID: "other-set", Expr: ss.ExprIn, Left: ss.Row{ss.NullDatum()}, Rule: ss.NullAwareRule, CandidateSet: "other"},
		{ID: "all-sets", Expr: ss.ExprIn, Left: ss.Row{ss.NullDatum()}, Rule: ss.NullAwareRule},
	}
	chunk := ss.Chunk{
		Set:  shared,
		Rows: scalarInner(ss.NullDatum()),
	}
	results := evaluateOrFail(t, reqs, nil, chunk)

	want := []struct {
		value     ss.Truth
		candidate int
	}{
		{ss.TruthUnknown, 1},
		{ss.TruthTrue, 1},
		{ss.TruthFalse, 0},
		{ss.TruthTrue, 1},
	}
	for i, w := range want {
		if results[i].Value != w.value || results[i].CandidateRows != w.candidate {
			t.Fatalf("request %d (%s): value=%s candidateRows=%d; want %s/%d",
				i, results[i].ID, results[i].Value, results[i].CandidateRows, w.value, w.candidate)
		}
	}

	compositeSet := "composite"
	compositeReqs := []ss.Request{
		{ID: "null-second", Expr: ss.ExprIn, Rule: ss.SQLRule, CandidateSet: compositeSet, Left: ss.Row{ss.Int(1), ss.NullDatum()}},
		{ID: "null-first-false-second", Expr: ss.ExprIn, Rule: ss.SQLRule, CandidateSet: compositeSet, Left: ss.Row{ss.NullDatum(), ss.Int(9)}},
	}
	compositeChunk := ss.Chunk{Set: compositeSet, Rows: []ss.InnerRow{{Values: ss.Row{ss.Int(1), ss.Int(2)}}}}
	compositeResults := evaluateOrFail(t, compositeReqs, nil, compositeChunk)
	if compositeResults[0].Value != ss.TruthUnknown {
		t.Fatalf("null-second composite = %s, want UNKNOWN", compositeResults[0].Value)
	}
	if compositeResults[1].Value != ss.TruthFalse {
		t.Fatalf("null-first with false second field = %s, want FALSE", compositeResults[1].Value)
	}
}

func TestEarlyMatchStopsOnlyOneOuterRequest(t *testing.T) {
	reqs := []ss.Request{
		{ID: "outer-a", Expr: ss.ExprIn, Left: ss.Row{ss.Int(1)}, Rule: ss.SQLRule},
		{ID: "outer-b", Expr: ss.ExprIn, Left: ss.Row{ss.Int(2)}, Rule: ss.SQLRule},
	}
	op, err := ss.NewOperator(reqs)
	if err != nil {
		t.Fatal(err)
	}

	chunks := []ss.Chunk{
		{Rows: scalarInner(ss.Int(1))},
		{Rows: scalarInner(ss.NullDatum())},
		{Rows: scalarInner(ss.Int(2))},
	}

	if err := op.ConsumeChunk(chunks[0]); err != nil {
		t.Fatal(err)
	}
	progress := op.Progress()
	if !progress[0].EarlyTerminated || progress[0].Current != ss.TruthTrue {
		t.Fatalf("outer-a after match = %+v, want early TRUE", progress[0])
	}
	if progress[1].EarlyTerminated || progress[1].Current != ss.TruthFalse {
		t.Fatalf("outer-b after first row = %+v, want pending FALSE", progress[1])
	}
	if op.PendingCount() != 1 || op.AllDecisive() {
		t.Fatalf("PendingCount=%d AllDecisive=%v; want 1/false", op.PendingCount(), op.AllDecisive())
	}

	if err := op.ConsumeChunk(chunks[1]); err != nil {
		t.Fatal(err)
	}
	progress = op.Progress()
	if progress[1].Current != ss.TruthUnknown || progress[1].UnknownComparisons != 1 {
		t.Fatalf("outer-b after null = %+v, want pending UNKNOWN with one unknown comparison", progress[1])
	}
	if progress[0].Comparisons != 1 {
		t.Fatalf("outer-a comparisons = %d; early request should not compare later rows", progress[0].Comparisons)
	}

	if err := op.ConsumeChunk(chunks[2]); err != nil {
		t.Fatal(err)
	}
	results, err := op.Finish()
	if err != nil {
		t.Fatal(err)
	}
	if results[0].Value != ss.TruthTrue || results[0].Comparisons != 1 || !results[0].EarlyTerminated {
		t.Fatalf("outer-a result = %+v", results[0])
	}
	if results[1].Value != ss.TruthTrue || results[1].Comparisons != 3 || !results[1].EarlyTerminated {
		t.Fatalf("outer-b result = %+v; it must recover its match from remaining data", results[1])
	}
	stats := op.Stats()
	if stats.RowsRead != 3 || stats.Comparisons != 4 || stats.EarlyTerminated != 2 {
		t.Fatalf("stats=%+v, want rows=3 comparisons=4 early=2", stats)
	}
}

func TestNotInEarlyFalseSkipsLaterNullForThatOuterOnly(t *testing.T) {
	reqs := []ss.Request{
		{ID: "matched", Expr: ss.ExprNotIn, Left: ss.Row{ss.Int(1)}, Rule: ss.SQLRule},
		{ID: "uncertain", Expr: ss.ExprNotIn, Left: ss.Row{ss.Int(2)}, Rule: ss.SQLRule},
	}
	rows := []ss.InnerRow{
		{Values: ss.Row{ss.Int(1)}, Payload: "decisive"},
		{Values: ss.Row{ss.NullDatum()}},
	}
	results := evaluateOrFail(t, reqs, nil, ss.Chunk{Rows: rows})

	if results[0].Value != ss.TruthFalse {
		t.Fatalf("matched NOT IN = %s, want FALSE", results[0].Value)
	}
	if results[0].Comparisons != 1 || results[0].SawUnknownComparison {
		t.Fatalf("matched request = %+v; later NULL must be skipped after decisive match", results[0])
	}
	if results[0].Witness != "decisive" {
		t.Fatalf("Witness = %v, want decisive payload", results[0].Witness)
	}
	if results[1].Value != ss.TruthUnknown || !results[1].SawUnknownComparison {
		t.Fatalf("uncertain NOT IN = %s unknownSeen=%v, want UNKNOWN/true", results[1].Value, results[1].SawUnknownComparison)
	}
	if results[1].Comparisons != 2 {
		t.Fatalf("uncertain comparisons = %d, want 2", results[1].Comparisons)
	}
}

func TestDependencyChunkingDoesNotChangeResults(t *testing.T) {
	rows := []ss.InnerRow{
		{Key: ss.RowOf(int64(1), int64(0)), Values: ss.Row{ss.Int(10)}},
		{Key: ss.RowOf(int64(2), nil), Values: ss.Row{ss.Int(20)}},
		{Key: ss.RowOf(int64(1), int64(0)), Values: ss.Row{ss.NullDatum()}},
		{Key: ss.RowOf(int64(2), nil), Values: ss.Row{ss.Int(30)}},
	}
	dependencyChunks := ss.ChunkByDependency(rows)
	if len(dependencyChunks) != 2 {
		t.Fatalf("ChunkByDependency len = %d, want 2", len(dependencyChunks))
	}
	for _, chunk := range dependencyChunks {
		if len(chunk.Rows) != 2 {
			t.Fatalf("chunk %q has %d rows, want 2", chunk.Set, len(chunk.Rows))
		}
	}

	reqs := []ss.Request{
		{ID: "dep-1-match", Expr: ss.ExprIn, Left: ss.Row{ss.Int(10)}, Param: ss.RowOf(int64(1), int64(0)), Rule: ss.SQLRule},
		{ID: "dep-2-exists-null-aware", Expr: ss.ExprExists, Param: ss.RowOf(int64(2), nil), Rule: ss.NullAwareRule},
		{ID: "all-not-in", Expr: ss.ExprNotIn, Left: ss.Row{ss.Int(99)}, Rule: ss.SQLRule},
	}
	corr := func(req ss.Request, row ss.InnerRow) (bool, error) {
		if len(req.Param) == 0 {
			return true, nil
		}
		return ss.MatchParameter(req, row)
	}

	whole := evaluateOrFail(t, reqs, corr, ss.Chunk{Rows: rows})

	rowChunks := make([]ss.Chunk, 0, len(rows))
	for _, row := range rows {
		rowChunks = append(rowChunks, ss.Chunk{Rows: []ss.InnerRow{row}})
	}
	perRow := evaluateOrFail(t, reqs, corr, rowChunks...)
	if !reflect.DeepEqual(whole, perRow) {
		t.Fatalf("per-row chunk results differ:\nwhole=%+v\nrow=%+v", whole, perRow)
	}

	byDependency := evaluateOrFail(t, reqs, corr, dependencyChunks...)
	// Dependency grouping may reorder interleaved keys. Final three-valued
	// results must be unchanged; scan-order counters such as correlation
	// checks before early termination are intentionally order-sensitive.
	if len(whole) != len(byDependency) {
		t.Fatalf("dependency result length = %d, want %d", len(byDependency), len(whole))
	}
	for i := range whole {
		if whole[i].Value != byDependency[i].Value {
			t.Fatalf("request %s value changed by dependency grouping: %s vs %s",
				whole[i].ID, whole[i].Value, byDependency[i].Value)
		}
	}
}

type scriptScanner struct {
	chunks    []ss.Chunk
	pos       int
	nextCalls int
}

func (s *scriptScanner) NextChunk(context.Context) (ss.Chunk, error) {
	s.nextCalls++
	if s.pos >= len(s.chunks) {
		return ss.Chunk{}, io.EOF
	}
	chunk := s.chunks[s.pos]
	s.pos++
	return chunk, nil
}

func TestEvaluateSharesScanAndStopsOnlyWhenAllOuterRowsDecisive(t *testing.T) {
	scanner := &scriptScanner{chunks: []ss.Chunk{
		{Rows: scalarInner(ss.Int(1))},
		{Rows: scalarInner(ss.NullDatum())},
		{Rows: scalarInner(ss.Int(2))},
		{Rows: scalarInner(ss.Int(99))},
	}}
	reqs := []ss.Request{
		{ID: "a", Expr: ss.ExprIn, Left: ss.Row{ss.Int(1)}, Rule: ss.SQLRule},
		{ID: "b", Expr: ss.ExprIn, Left: ss.Row{ss.Int(2)}, Rule: ss.SQLRule},
	}

	results, stats, err := ss.Evaluate(context.Background(), reqs, scanner)
	if err != nil {
		t.Fatal(err)
	}
	if scanner.nextCalls != 3 || scanner.pos != 3 {
		t.Fatalf("scanner calls=%d pos=%d; shared scan should stop after both resolve in chunk 3", scanner.nextCalls, scanner.pos)
	}
	if !stats.ScanStoppedEarly || stats.RowsRead != 3 {
		t.Fatalf("stats=%+v; want early stop after 3 read rows", stats)
	}
	if results[0].Value != ss.TruthTrue || results[1].Value != ss.TruthTrue {
		t.Fatalf("results=%s/%s, want TRUE/TRUE", results[0].Value, results[1].Value)
	}
}

func TestValidationAndComparisonErrors(t *testing.T) {
	if _, err := ss.NewOperator([]ss.Request{{Expr: ss.ExprInvalid}}); err == nil {
		t.Fatal("invalid expression should be rejected")
	}
	if _, err := ss.NewOperator([]ss.Request{
		{ID: "dup", Expr: ss.ExprExists},
		{ID: "dup", Expr: ss.ExprExists},
	}); err == nil {
		t.Fatal("duplicate IDs should be rejected")
	}
	if _, err := ss.NewOperator([]ss.Request{{
		Expr: ss.ExprIn,
		Left: ss.Row{ss.Int(1)},
		Rule: ss.Rule{NullsEqual: true}, // custom behavior but missing ID
	}}); err == nil {
		t.Fatal("custom rule without ID should be rejected")
	}

	op, err := ss.NewOperator([]ss.Request{{
		ID:   "arity",
		Expr: ss.ExprIn,
		Left: ss.Row{ss.Int(1), ss.Int(2)},
		Rule: ss.SQLRule,
	}})
	if err != nil {
		t.Fatal(err)
	}
	if err := op.Consume(ss.InnerRow{Values: ss.Row{ss.Int(1)}}); err == nil {
		t.Fatal("row arity mismatch should return an error")
	}
	if err := op.Consume(); err == nil {
		t.Fatal("processing error should remain sticky on later Consume calls")
	}
	if _, err := op.Finish(); err == nil {
		t.Fatal("Finish should retain the processing error")
	}

	finished, err := ss.NewOperator([]ss.Request{{ID: "ok", Expr: ss.ExprExists}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := finished.Finish(); err != nil {
		t.Fatal(err)
	}
	if err := finished.Consume(); !errors.Is(err, ss.ErrFinished) {
		t.Fatalf("Consume after Finish error = %v, want ErrFinished", err)
	}
}
