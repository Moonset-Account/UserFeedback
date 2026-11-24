package egress

// AmbiguityCase is the two-table reviewer fixture. The two true tables have
// identical gate decisions before IndistinguishableUntil. Continue is legal
// in ContinueSafeScenario but misses a gate in the other; SplitEarly is legal
// in SplitSafeScenario but the extra fragment overhead makes its later slice
// miss the gate in the other phase.
type AmbiguityCase struct {
	IndistinguishableUntil int        `json:"indistinguishable_until"`
	Overhead               int        `json:"overhead"`
	MinNonLast             int        `json:"min_non_last"`
	Frames                 []Frame    `json:"frames"`
	Scenarios              []Scenario `json:"scenarios"`
	Continue               []Fragment `json:"continue_choice"`
	SplitEarly             []Fragment `json:"split_early_choice"`
}

// ChoiceRisk is one choice evaluated under one true table.
type ChoiceRisk struct {
	Choice     string      `json:"choice"`
	Scenario   string      `json:"scenario"`
	Legal      bool        `json:"legal"`
	FirstMiss  *GateMiss   `json:"first_miss,omitempty"`
	Evaluation *Evaluation `json:"-"`
}

// AmbiguityReport contains all four cross-evaluations.
type AmbiguityReport struct {
	Case          AmbiguityCase `json:"case"`
	ContinueInA   ChoiceRisk    `json:"continue_in_a"`
	ContinueInB   ChoiceRisk    `json:"continue_in_b"`
	SplitEarlyInA ChoiceRisk    `json:"split_early_in_a"`
	SplitEarlyInB ChoiceRisk    `json:"split_early_in_b"`
}

// StandardAmbiguityCase returns the short integer-byte reviewer example.
//
// Table A closes the initial normal window at tick 5 and reopens 7..12.
// Table B keeps it open until tick 6, inserts a one-tick switch gap, then has
// only a three-tick normal window 7..10. Before tick 3 both tables are simply
// "normal gate open", so the first slice boundary is the first possible
// distinction point.
func StandardAmbiguityCase() AmbiguityCase {
	frames := []Frame{{ID: "N", Class: Normal, Arrival: 0, Length: 5, Deadline: 100}}
	tableA := &GateTable{Entries: []GateEntry{
		{Duration: 5, Gates: GateNormal},
		{Duration: 2, Gates: GateClosed},
		{Duration: 5, Gates: GateNormal},
	}}
	tableB := &GateTable{Entries: []GateEntry{
		{Duration: 6, Gates: GateNormal},
		{Duration: 1, Gates: GateClosed},
		{Duration: 3, Gates: GateNormal},
		{Duration: 2, Gates: GateClosed},
	}}
	continueChoice := []Fragment{{
		FrameID: "N", Class: Normal, Index: 1,
		Start: 0, Overhead: 1, Data: 5, End: 6, Bytes: 6, Final: true,
	}}
	splitChoice := []Fragment{
		{FrameID: "N", Class: Normal, Index: 1, Start: 0, Overhead: 1, Data: 2, End: 3, Bytes: 3, Final: false},
		{FrameID: "N", Class: Normal, Index: 2, Start: 7, Overhead: 1, Data: 3, End: 11, Bytes: 4, Final: true},
	}
	return AmbiguityCase{
		IndistinguishableUntil: 3,
		Overhead:               1,
		MinNonLast:             1,
		Frames:                 frames,
		Scenarios: []Scenario{
			{Name: "A_early_close_reopen_long", Table: tableA},
			{Name: "B_long_first_short_reopen", Table: tableB},
		},
		Continue:   continueChoice,
		SplitEarly: splitChoice,
	}
}

// ReviewAmbiguousChoices cross-checks continue-versus-split decisions against
// both possible true tables.
func ReviewAmbiguousChoices() (*AmbiguityReport, error) {
	c := StandardAmbiguityCase()
	rep := &AmbiguityReport{Case: c}
	var err error
	setRisk := func(dst *ChoiceRisk, choiceName string, sc Scenario, frags []Fragment) error {
		ev, checkErr := CheckFragments(Config{
			Table: sc.Table, Frames: c.Frames,
			MinNonLast: c.MinNonLast, Overhead: c.Overhead,
		}, frags)
		if checkErr != nil {
			return checkErr
		}
		r := ChoiceRisk{Choice: choiceName, Scenario: sc.Name, Evaluation: ev, Legal: ev.Valid()}
		if gm, ok := ev.FirstGateMiss(); ok {
			gm.Scenario = sc.Name
			r.FirstMiss = &gm
			r.Legal = false
		}
		*dst = r
		return nil
	}
	if err = setRisk(&rep.ContinueInA, "continue", c.Scenarios[0], c.Continue); err != nil {
		return nil, err
	}
	if err = setRisk(&rep.ContinueInB, "continue", c.Scenarios[1], c.Continue); err != nil {
		return nil, err
	}
	if err = setRisk(&rep.SplitEarlyInA, "split_early", c.Scenarios[0], c.SplitEarly); err != nil {
		return nil, err
	}
	if err = setRisk(&rep.SplitEarlyInB, "split_early", c.Scenarios[1], c.SplitEarly); err != nil {
		return nil, err
	}
	return rep, nil
}
