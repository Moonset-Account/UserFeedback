package incview

import (
	"math/rand"
	"reflect"
	"testing"
)

// recompute builds the expected view from scratch out of the base tables,
// using the same inner-join semantics but no incremental state.
func recompute(facts []Fact, devices map[string]string) map[string]Group {
	out := make(map[string]Group)
	for _, f := range facts {
		room, ok := devices[f.Device]
		if !ok {
			continue
		}
		g, seen := out[room]
		if !seen {
			g = Group{Room: room, Min: f.Duration}
		}
		g.Count++
		g.Total += int64(f.Duration)
		if f.Duration < g.Min {
			g.Min = f.Duration
		}
		out[room] = g
	}
	return out
}

func actualGroups(v *View) map[string]Group {
	out := make(map[string]Group)
	for _, g := range v.Groups() {
		out[g.Room] = g
	}
	return out
}

func changedRooms(before, after map[string]Group) map[string]bool {
	out := map[string]bool{}
	for room, g := range before {
		if a, ok := after[room]; !ok || a != g {
			out[room] = true
		}
	}
	for room, g := range after {
		if b, ok := before[room]; !ok || b != g {
			out[room] = true
		}
	}
	return out
}

type devOp struct {
	id   string
	room string // empty means delete the mapping
}

type factOp struct {
	row      int
	device   string
	duration int
	del      bool
}

// TestRandomizedAgainstFullRecompute drives long random transactions and
// checks after every commit that the incremental aggregates equal a full
// recompute, that Deltas describe exactly the rooms that really changed, and
// that VisitedRows never reaches beyond the devices the transaction touched.
func TestRandomizedAgainstFullRecompute(t *testing.T) {
	rng := rand.New(rand.NewSource(20261122))
	v := NewView()

	mapped := map[string]string{}
	factSet := map[int]Fact{}
	rooms := []string{"east", "west", "north", "south", "attic"}
	devices := []string{"d0", "d1", "d2", "d3", "d4", "d5"}

	for step := 0; step < 400; step++ {
		before := actualGroups(v)

		// Build a random, order-insensitive transaction by key.
		devAssign := map[string]string{}
		devDeleted := map[string]bool{}
		nDev := rng.Intn(3)
		for i := 0; i < nDev; i++ {
			id := devices[rng.Intn(len(devices))]
			if rng.Intn(4) == 0 {
				devDeleted[id] = true
				delete(devAssign, id)
			} else {
				devAssign[id] = rooms[rng.Intn(len(rooms))]
				delete(devDeleted, id)
			}
		}
		factAssign := map[int]Fact{}
		factDeleted := map[int]bool{}
		nFact := rng.Intn(6)
		for i := 0; i < nFact; i++ {
			row := 1 + rng.Intn(30)
			if rng.Intn(4) == 0 {
				factDeleted[row] = true
				delete(factAssign, row)
			} else {
				factAssign[row] = Fact{Row: row, Device: devices[rng.Intn(len(devices))], Duration: rng.Intn(50)}
				delete(factDeleted, row)
			}
		}

		// Projected base tables: what the commit should leave behind.
		projDevices := map[string]string{}
		for k, val := range mapped {
			projDevices[k] = val
		}
		for id, room := range devAssign {
			projDevices[id] = room
		}
		for id := range devDeleted {
			delete(projDevices, id)
		}
		projFacts := map[int]Fact{}
		for k, val := range factSet {
			projFacts[k] = val
		}
		for row, f := range factAssign {
			projFacts[row] = f
		}
		for row := range factDeleted {
			delete(projFacts, row)
		}

		// Which devices had a mapping or ownership change.
		movedDevices := map[string]bool{}
		for _, id := range devices {
			oldRoom, hadOld := mapped[id]
			newRoom, hasNew := projDevices[id]
			if hadOld != hasNew || oldRoom != newRoom {
				movedDevices[id] = true
			}
		}

		// Allowed visited rows: every row of a moved device (both sides),
		// plus rows whose fact actually changed.
		allowed := map[int]bool{}
		for row, f := range factSet {
			if movedDevices[f.Device] {
				allowed[row] = true
			}
			if pf, ok := projFacts[row]; !ok || pf != f {
				allowed[row] = true
			}
		}
		for row, f := range projFacts {
			if movedDevices[f.Device] {
				allowed[row] = true
			}
			if old, ok := factSet[row]; !ok || old != f {
				allowed[row] = true
			}
		}

		tx := v.Begin()
		for id, room := range devAssign {
			if err := tx.SetDevice(id, room); err != nil {
				tx.Rollback()
				t.Fatalf("step %d SetDevice(%s,%s): %v", step, id, room, err)
			}
		}
		for id := range devDeleted {
			if _, ok := mapped[id]; !ok {
				continue // nothing to delete
			}
			if err := tx.DeleteDevice(id); err != nil {
				tx.Rollback()
				t.Fatalf("step %d DeleteDevice(%s): %v", step, id, err)
			}
		}
		for row, f := range factAssign {
			if err := tx.PutFact(row, f.Device, f.Duration); err != nil {
				tx.Rollback()
				t.Fatalf("step %d PutFact(%d): %v", step, row, err)
			}
		}
		for row := range factDeleted {
			if _, ok := factSet[row]; !ok {
				if _, inserted := factAssign[row]; !inserted {
					continue
				}
			}
			if err := tx.DeleteFact(row); err != nil {
				tx.Rollback()
				t.Fatalf("step %d DeleteFact(%d): %v", step, row, err)
			}
		}
		res := tx.Commit()

		mapped = projDevices
		factSet = projFacts

		after := actualGroups(v)
		want := recompute(mapValues(factSet), mapped)
		if !reflect.DeepEqual(after, want) {
			t.Fatalf("step %d: view %+v, full recompute %+v", step, after, want)
		}

		changed := changedRooms(before, after)
		if len(changed) != len(res.Deltas) {
			t.Fatalf("step %d: changed rooms %v, deltas %+v", step, changed, res.Deltas)
		}
		for _, d := range res.Deltas {
			if !changed[d.Room] {
				t.Fatalf("step %d: delta for unchanged room %s", step, d.Room)
			}
			b, hadBefore := before[d.Room]
			a, hasAfter := after[d.Room]
			if hadBefore != d.BeforeExists || hasAfter != d.AfterExists {
				t.Fatalf("step %d room %s existence: delta %+v, before %v/%v after %v/%v",
					step, d.Room, d, b, hadBefore, a, hasAfter)
			}
			if hadBefore && b != d.Before {
				t.Fatalf("step %d room %s before: delta %+v, actual %+v", step, d.Room, d.Before, b)
			}
			if hasAfter && a != d.After {
				t.Fatalf("step %d room %s after: delta %+v, actual %+v", step, d.Room, d.After, a)
			}
		}
		for _, row := range res.VisitedRows {
			if !allowed[row] {
				t.Fatalf("step %d: visited row %d outside touched devices (moved %v)", step, row, movedDevices)
			}
		}
	}
}

func mapValues(m map[int]Fact) []Fact {
	out := make([]Fact, 0, len(m))
	for _, f := range m {
		out = append(out, f)
	}
	return out
}
