package dataflow

// This file builds the synthetic graphs used by the functional tests. All
// data is local and tiny.

// fanoutGraph returns P ->e1-> C1 and P ->e2-> C2. The producer publishes
// one physical block to BOTH consumers.
func fanoutGraph() *Graph {
	return &Graph{
		Ops: []OpID{"P", "C1", "C2"},
		Edges: []Edge{
			{ID: 1, From: "P", To: "C1"},
			{ID: 2, From: "P", To: "C2"},
		},
	}
}

func fanoutPrograms() map[OpID][]Instr {
	return map[OpID][]Instr{
		"P": {
			AllocInstr("b"),
			PublishInstr("b", 1, 2),
			ReleaseInstr("b"),
		},
		"C1": {
			TakeInstr(1, "x"),
			ReleaseInstr("x"),
		},
		"C2": {
			TakeInstr(2, "y"),
			ReleaseInstr("y"),
		},
	}
}

// chainGraph returns the acyclic chain A -e1-> B -e2-> C. Both B and C
// allocate output blocks BEFORE consuming their input, which creates
// resource cycles even though the graph is a DAG.
//
// Declaration order of edges is intentionally reversed in some tests via
// chainGraphScrambled to prove tie-breaking uses edge NUMBERS.
func chainGraph() *Graph {
	return &Graph{
		Ops: []OpID{"A", "B", "C"},
		Edges: []Edge{
			{ID: 1, From: "A", To: "B"},
			{ID: 2, From: "B", To: "C"},
		},
	}
}

func chainGraphScrambled() *Graph {
	g := chainGraph()
	g.Edges = []Edge{
		{ID: 2, From: "B", To: "C"},
		{ID: 1, From: "A", To: "B"},
	}
	return g
}

func chainPrograms() map[OpID][]Instr {
	return map[OpID][]Instr{
		// Source: allocate, publish, drop producer reference.
		"A": {
			AllocInstr("a"),
			PublishInstr("a", 1),
			ReleaseInstr("a"),
		},
		// Intermediate: grabs its output/pool block FIRST, then consumes.
		"B": {
			AllocInstr("b"),
			PublishInstr("b", 2),
			ReleaseInstr("b"),
			TakeInstr(1, "x"),
			ReleaseInstr("x"),
		},
		// Sink: grabs another pool slot FIRST, then takes its input.
		"C": {
			AllocInstr("c"),
			TakeInstr(2, "y"),
			ReleaseInstr("y"),
			ReleaseInstr("c"),
		},
	}
}

func mustStep(t interface {
	Helper()
	Fatalf(format string, args ...any)
}, d *Driver, op OpID) Record {
	t.Helper()
	rec, err := d.Step(op)
	if err != nil {
		t.Fatalf("Step(%q) failed unexpectedly: %v", op, err)
	}
	return *rec
}

func mustStepAny(t interface {
	Helper()
	Fatalf(format string, args ...any)
}, d *Driver) Record {
	t.Helper()
	rec, err := d.StepAny()
	if err != nil {
		t.Fatalf("StepAny failed unexpectedly: %v", err)
	}
	return *rec
}

// runStepAnyToCompletion drives the deterministic smallest-op schedule.
func runStepAnyToCompletion(t interface {
	Helper()
	Fatalf(format string, args ...any)
}, d *Driver, wantSteps int) {
	t.Helper()
	steps := 0
	for {
		_, err := d.StepAny()
		if err == ErrOperatorDone {
			break
		}
		if err != nil {
			t.Fatalf("unexpected scheduling deadlock after %d steps: %v; blockers: %+v", steps, err, d.Blockers())
		}
		steps++
	}
	if steps != wantSteps {
		t.Fatalf("executed %d steps, want %d", steps, wantSteps)
	}
	if !d.AllDone() {
		t.Fatalf("driver not all done after %d steps", steps)
	}
	if err := d.CheckInvariants(); err != nil {
		t.Fatalf("invariant violation: %v", err)
	}
	if used := d.PoolUsed(); used != 0 {
		t.Fatalf("pool not empty at completion: %d blocks still resident", used)
	}
}
