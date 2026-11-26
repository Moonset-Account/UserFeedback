package sharedscan

import (
	"fmt"
	"strings"
)

// Expr identifies the supported subquery expression.
type Expr uint8

const (
	// ExprInvalid is the zero value and is rejected by NewOperator.
	ExprInvalid Expr = iota
	// ExprExists is SQL EXISTS. Its result is two-valued after WHERE filtering.
	ExprExists
	// ExprIn is a composite row-value IN predicate.
	ExprIn
	// ExprNotIn is NOT IN, evaluated as NOT over the IN three-valued result.
	ExprNotIn
)

// String returns the SQL expression name.
func (e Expr) String() string {
	switch e {
	case ExprExists:
		return "EXISTS"
	case ExprIn:
		return "IN"
	case ExprNotIn:
		return "NOT IN"
	default:
		return "INVALID"
	}
}

// Request is one outer row's evaluation state. A batch contains one Request
// per outer-row/subquery pair.
type Request struct {
	// ID is optional; NewOperator assigns req-N when empty.
	ID string

	// Expr is IN, NOT IN or EXISTS.
	Expr Expr

	// Left is the composite left-hand row for IN/NOT IN. It must be empty for
	// EXISTS; correlation input belongs in Param.
	Left Row

	// Param is the composite outer correlation parameter. A Correlator may
	// compare it with InnerRow.Key.
	Param Row

	// Rule is the explicit comparison rule. The zero value defaults to
	// SQLRule. A custom rule must have a non-empty ID.
	Rule Rule

	// CandidateSet optionally selects chunks with the same Chunk.Set. Empty
	// means the request observes every chunk offered to the operator.
	CandidateSet string
}

func (r Request) normalize(index int) (Request, error) {
	if r.Expr == ExprInvalid || r.Expr > ExprNotIn {
		return Request{}, fmt.Errorf("request %d: invalid expression %d", index, r.Expr)
	}
	if r.ID == "" {
		r.ID = fmt.Sprintf("req-%d", index)
	}
	if err := r.Left.validate("Left"); err != nil {
		return Request{}, fmt.Errorf("request %q: %w", r.ID, err)
	}
	if err := r.Param.validate("Param"); err != nil {
		return Request{}, fmt.Errorf("request %q: %w", r.ID, err)
	}
	rule, err := r.Rule.normalize()
	if err != nil {
		return Request{}, fmt.Errorf("request %q: %w", r.ID, err)
	}
	r.Rule = rule

	switch r.Expr {
	case ExprIn, ExprNotIn:
		if len(r.Left) == 0 {
			return Request{}, fmt.Errorf("request %q: %s requires a non-empty composite Left row", r.ID, r.Expr)
		}
	case ExprExists:
		if len(r.Left) != 0 {
			return Request{}, fmt.Errorf("request %q: EXISTS does not compare Left; put correlation values in Param", r.ID)
		}
	}
	return r, nil
}

// Fingerprint returns a semantic identity for the expression, comparison
// rule, candidate-set label, left values and correlation parameters. It is
// suitable as part of a caller-managed cache key. In particular, changing the
// rule ID or a NULL field position changes the fingerprint.
func (r Request) Fingerprint() (string, error) {
	normalized, err := r.normalize(0)
	if err != nil {
		return "", err
	}

	var b strings.Builder
	b.WriteString(normalized.Expr.String())
	b.WriteString("|set=")
	b.WriteString(normalized.CandidateSet)
	b.WriteString("|rule=")
	b.WriteString(normalized.Rule.ID)
	b.WriteString("|left=")
	appendRowKey(&b, normalized.Left)
	b.WriteString("|param=")
	appendRowKey(&b, normalized.Param)
	return b.String(), nil
}

// InnerRow is one row from the shared inner scan.
type InnerRow struct {
	// Key is the correlation key, conventionally matched against Request.Param.
	Key Row

	// Values is the composite inner side of an IN/NOT IN comparison.
	Values Row

	// Payload optionally retains the original executor tuple. A decisive
	// result exposes it through Result.Witness.
	Payload any
}

// Chunk is an incremental batch of inner rows. Chunk boundaries never change
// three-valued results; callers may create chunks by dependency key, size or
// any other scan property.
type Chunk struct {
	// Set is an optional physical candidate-set/dependency-set label.
	Set string

	// Dependency is optional metadata describing the dependency key for a
	// dependency-grouped chunk.
	Dependency Row

	// Rows are the inner rows in this chunk.
	Rows []InnerRow
}

// DependencySetID returns the physical candidate-set label for a composite
// dependency key. It preserves arity, types and NULL positions.
func DependencySetID(key Row) string {
	return "dep:" + key.PhysicalKey()
}

// ChunkByDependency groups rows by InnerRow.Key using DependencySetID. Group
// order follows first occurrence of each key. This is a physical grouping
// only; SQL NULL comparison semantics are still applied by the Correlator or
// comparison Rule.
func ChunkByDependency(rows []InnerRow) []Chunk {
	var order []string
	groups := make(map[string]*Chunk)

	for _, row := range rows {
		id := DependencySetID(row.Key)
		chunk, ok := groups[id]
		if !ok {
			dependency := append(Row(nil), row.Key...)
			chunk = &Chunk{Set: id, Dependency: dependency}
			groups[id] = chunk
			order = append(order, id)
		}
		chunk.Rows = append(chunk.Rows, row)
	}

	chunks := make([]Chunk, 0, len(order))
	for _, id := range order {
		chunks = append(chunks, *groups[id])
	}
	return chunks
}
