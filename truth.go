package sharedscan

// Truth is the SQL-style three-valued logic result.
//
// The zero value is TruthUnknown so an unset result cannot accidentally be
// interpreted as FALSE or TRUE.
type Truth uint8

const (
	// TruthUnknown represents SQL UNKNOWN / NULL.
	TruthUnknown Truth = iota
	// TruthFalse represents SQL FALSE.
	TruthFalse
	// TruthTrue represents SQL TRUE.
	TruthTrue
)

// String returns the uppercase SQL-facing name.
func (t Truth) String() string {
	switch t {
	case TruthTrue:
		return "TRUE"
	case TruthFalse:
		return "FALSE"
	default:
		return "UNKNOWN"
	}
}

// Not applies three-valued negation: NOT UNKNOWN remains UNKNOWN.
func Not(t Truth) Truth {
	switch t {
	case TruthTrue:
		return TruthFalse
	case TruthFalse:
		return TruthTrue
	default:
		return TruthUnknown
	}
}

// And applies three-valued conjunction. FALSE dominates UNKNOWN; therefore
// FALSE AND UNKNOWN is FALSE. With no operands it returns TRUE.
func And(values ...Truth) Truth {
	sawUnknown := false
	for _, v := range values {
		if v == TruthFalse {
			return TruthFalse
		}
		if v == TruthUnknown {
			sawUnknown = true
		}
	}
	if sawUnknown {
		return TruthUnknown
	}
	return TruthTrue
}

// Or applies three-valued disjunction. TRUE dominates UNKNOWN; therefore
// TRUE OR UNKNOWN is TRUE. With no operands it returns FALSE.
func Or(values ...Truth) Truth {
	sawUnknown := false
	for _, v := range values {
		if v == TruthTrue {
			return TruthTrue
		}
		if v == TruthUnknown {
			sawUnknown = true
		}
	}
	if sawUnknown {
		return TruthUnknown
	}
	return TruthFalse
}
