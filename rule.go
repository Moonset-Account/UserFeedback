package sharedscan

import (
	"errors"
	"fmt"
	"reflect"
	"strings"
)

// Rule is an explicitly named comparison rule.
//
// ID participates in request fingerprints, so two rules with different IDs
// are never conflated by a caller-managed cache. Equal is called only for two
// non-NULL values.
type Rule struct {
	// ID is the stable, explicit identity of this rule.
	ID string

	// NullsEqual selects NULL-safe equality (SQL IS NOT DISTINCT FROM) when
	// true. When false, any NULL operand yields TruthUnknown.
	NullsEqual bool

	// Equal compares two non-NULL scalar values. If nil, ScalarEqual is used.
	Equal func(a, b any) (bool, error)
}

// SQLRule is ordinary SQL equality: NULL compared with anything is UNKNOWN.
var SQLRule = Rule{ID: "sql_standard", NullsEqual: false, Equal: ScalarEqual}

// NullAwareRule treats two NULLs as equal and NULL versus non-NULL as false.
// It corresponds to IS NOT DISTINCT FROM.
var NullAwareRule = Rule{ID: "null_aware", NullsEqual: true, Equal: ScalarEqual}

// CaseInsensitiveRule compares strings with Unicode case folding while
// retaining ordinary SQL NULL behavior.
var CaseInsensitiveRule = Rule{
	ID:         "string_case_insensitive_sql_null",
	NullsEqual: false,
	Equal:      EqualFold,
}

func (r Rule) normalize() (Rule, error) {
	if r.ID == "" {
		if r.Equal == nil && !r.NullsEqual {
			return SQLRule, nil
		}
		return Rule{}, errors.New("sharedscan: custom comparison Rule requires a non-empty ID")
	}
	if r.Equal == nil {
		r.Equal = ScalarEqual
	}
	return r, nil
}

// Compare compares two scalar datums under the rule.
func (r Rule) Compare(a, b Datum) (Truth, error) {
	rule, err := r.normalize()
	if err != nil {
		return TruthUnknown, err
	}

	aNull := a.isNull()
	bNull := b.isNull()
	if aNull || bNull {
		if aNull && bNull && rule.NullsEqual {
			return TruthTrue, nil
		}
		if rule.NullsEqual {
			return TruthFalse, nil
		}
		return TruthUnknown, nil
	}

	equal, err := rule.Equal(a.Value, b.Value)
	if err != nil {
		return TruthUnknown, err
	}
	if equal {
		return TruthTrue, nil
	}
	return TruthFalse, nil
}

// CompareRows compares composite rows field by field and combines field
// results with three-valued AND. Arity mismatch is an error. All fields are
// evaluated so a later comparison error is not hidden by an earlier FALSE.
func CompareRows(a, b Row, rule Rule) (Truth, error) {
	if len(a) != len(b) {
		return TruthUnknown, fmt.Errorf("sharedscan: row arity mismatch: %d vs %d", len(a), len(b))
	}

	result := TruthTrue
	var firstErr error
	for i := range a {
		t, err := rule.Compare(a[i], b[i])
		if err != nil && firstErr == nil {
			firstErr = fmt.Errorf("field %d: %w", i, err)
		}
		result = And(result, t)
	}
	if firstErr != nil {
		return TruthUnknown, firstErr
	}
	return result, nil
}

// ScalarEqual is the default scalar comparator for the built-in Datum
// constructors. It also accepts the common Go integer kinds and compares
// values of the same numeric class.
func ScalarEqual(a, b any) (bool, error) {
	if a == nil || b == nil {
		return a == b, nil
	}

	va := reflect.ValueOf(a)
	vb := reflect.ValueOf(b)
	ka := va.Kind()
	kb := vb.Kind()

	if isSignedIntKind(ka) && isSignedIntKind(kb) {
		return va.Int() == vb.Int(), nil
	}
	if isUintKind(ka) && isUintKind(kb) {
		return va.Uint() == vb.Uint(), nil
	}
	if isSignedIntKind(ka) && isUintKind(kb) {
		x := va.Int()
		y := vb.Uint()
		return x >= 0 && uint64(x) == y, nil
	}
	if isUintKind(ka) && isSignedIntKind(kb) {
		x := vb.Int()
		y := va.Uint()
		return x >= 0 && uint64(x) == y, nil
	}
	if isFloatKind(ka) && isFloatKind(kb) {
		return va.Float() == vb.Float(), nil
	}
	if (isIntKind(ka) || isFloatKind(ka)) && (isIntKind(kb) || isFloatKind(kb)) {
		return numericFloat(va) == numericFloat(vb), nil
	}
	if ka == reflect.Bool && kb == reflect.Bool {
		return va.Bool() == vb.Bool(), nil
	}
	if ka == reflect.String && kb == reflect.String {
		return va.String() == vb.String(), nil
	}
	if va.Type() == vb.Type() && va.Type().Comparable() {
		return a == b, nil
	}
	return false, fmt.Errorf("unsupported scalar comparison %T vs %T", a, b)
}

// EqualFold compares two string values case-insensitively.
func EqualFold(a, b any) (bool, error) {
	as, ok := a.(string)
	if !ok {
		return false, fmt.Errorf("case-insensitive rule expects string, got %T", a)
	}
	bs, ok := b.(string)
	if !ok {
		return false, fmt.Errorf("case-insensitive rule expects string, got %T", b)
	}
	return strings.EqualFold(as, bs), nil
}

func isSignedIntKind(k reflect.Kind) bool {
	switch k {
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return true
	default:
		return false
	}
}

func isUintKind(k reflect.Kind) bool {
	switch k {
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
		return true
	default:
		return false
	}
}

func isIntKind(k reflect.Kind) bool {
	return isSignedIntKind(k) || isUintKind(k)
}

func isFloatKind(k reflect.Kind) bool {
	return k == reflect.Float32 || k == reflect.Float64
}

func numericFloat(v reflect.Value) float64 {
	switch {
	case isSignedIntKind(v.Kind()):
		return float64(v.Int())
	case isUintKind(v.Kind()):
		return float64(v.Uint())
	case isFloatKind(v.Kind()):
		return v.Float()
	default:
		return 0
	}
}
