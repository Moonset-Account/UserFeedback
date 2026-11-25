// Package sampling implements a sampling-permission planner for a
// remote-diagnostics terminal agent running on a shared terminal.
//
// The agent receives process instances (with the user that actually started
// them and their login session), terminal foreground-switch events, and the
// consent records (data types + time ranges) that users have granted. Given a
// requested sampling plan window, it computes, for every process, the
// sub-intervals in which each field may be collected.
//
// Rules enforced by the planner:
//
//   - Machine-scope consent only ever permits device-health fields
//     ("device.*"); it never unlocks process-level data.
//   - Process-level fields ("process.*") require a user-scope consent granted
//     for the user that actually started the process. A consent may
//     optionally be pinned to a login session ID.
//   - A revocation narrows the plan from the revocation moment onward.
//   - Foreground switches never reassign ownership of background processes:
//     a background process keeps its original starter, so another user's
//     consent (even the current foreground user's) never covers it.
package sampling

import (
	"sort"
	"strings"
	"time"
)

// DataField identifies a single collectible field.
type DataField string

const (
	// FieldDeviceHealth is machine-level device health data.
	FieldDeviceHealth DataField = "device.health"
	// FieldProcessCPU is per-process CPU usage (process-level).
	FieldProcessCPU DataField = "process.cpu"
	// FieldProcessMemory is per-process memory usage (process-level).
	FieldProcessMemory DataField = "process.memory"
	// FieldProcessCmdline is the process command line (process-level).
	FieldProcessCmdline DataField = "process.cmdline"
)

// IsMachineField reports whether f is machine-level (device health) data.
// Machine-level fields are permitted by machine-scope consent; everything
// else is process-level and requires the actual starter's authorization.
func IsMachineField(f DataField) bool {
	return strings.HasPrefix(string(f), "device.")
}

// Interval is a half-open time range [Start, End).
type Interval struct {
	Start time.Time
	End   time.Time
}

// Empty reports whether the interval contains no time.
func (iv Interval) Empty() bool { return !iv.End.After(iv.Start) }

// Intersect returns the overlap of iv and o (possibly empty).
func (iv Interval) Intersect(o Interval) Interval {
	s := iv.Start
	if o.Start.After(s) {
		s = o.Start
	}
	e := iv.End
	if o.End.Before(e) {
		e = o.End
	}
	return Interval{Start: s, End: e}
}

// Scope is the breadth of a consent grant.
type Scope int

const (
	// ScopeMachine permits machine-level (device health) data only.
	ScopeMachine Scope = iota
	// ScopeUser permits process-level data for one specific user's processes.
	ScopeUser
)

// Consent records a user's agreement to share certain data types over a
// time range.
type Consent struct {
	ID        string // unique consent identifier
	GrantedBy string // user who granted the consent (used by revocations)
	Scope     Scope  // machine-level or user-level
	Subject   string // ScopeUser only: the starter whose processes are covered
	LoginID   string // ScopeUser only: restrict to this login session; "" = all sessions of Subject
	Fields    []DataField
	Window    Interval
}

// Revocation withdraws consent at a moment in time. If ConsentID is set,
// only that consent is revoked; otherwise every consent granted by By is
// revoked. The plan narrows from At onward.
type Revocation struct {
	ConsentID string
	By        string
	At        time.Time
}

// Process is a process instance observed on the shared terminal.
type Process struct {
	PID       int
	StartUser string // user that actually started the process
	LoginID   string // login session the process belongs to
}

// ForegroundSwitch records the terminal switching its foreground user.
// It is accepted as input for auditing, but it never changes which user a
// background process belongs to.
type ForegroundSwitch struct {
	At      time.Time
	UserID  string
	LoginID string
}

// FieldWindow is one sub-interval of a plan together with the fields that
// may be collected during it. Fields are sorted for determinism.
type FieldWindow struct {
	Interval Interval
	Fields   []DataField
}

// ProcessPlan is the sampling schedule for one process.
type ProcessPlan struct {
	PID     int
	Windows []FieldWindow
}

// Result is the resolved sampling plan.
type Result struct {
	// Machine holds the sub-intervals in which device-level fields may be
	// collected under machine-scope consent.
	Machine []FieldWindow
	// Processes holds, per process, the sub-intervals and allowed fields.
	Processes []ProcessPlan
}

// BuildPlan resolves a requested sampling window against the granted
// consents and revocations. switches is deliberately not consulted for
// ownership: foreground switches preserve background processes' original
// attribution.
func BuildPlan(procs []Process, switches []ForegroundSwitch, consents []Consent, revocations []Revocation, plan Interval) Result {
	_ = switches // foreground switches never reassign process ownership

	res := Result{}
	if plan.Empty() {
		return res
	}

	// Machine-level fields from machine-scope consents only.
	machine := map[DataField][]Interval{}
	for _, c := range consents {
		if c.Scope != ScopeMachine {
			continue
		}
		w := effectiveWindow(c, revocations).Intersect(plan)
		if w.Empty() {
			continue
		}
		for _, f := range c.Fields {
			if !IsMachineField(f) {
				continue // machine consent covers device health data only
			}
			machine[f] = append(machine[f], w)
		}
	}
	res.Machine = combine(mergeAll(machine))

	// Process-level fields, attributed strictly by the actual starter.
	for _, p := range procs {
		fields := map[DataField][]Interval{}
		for _, c := range consents {
			if c.Scope != ScopeUser {
				continue
			}
			if c.Subject != p.StartUser {
				continue
			}
			if c.LoginID != "" && c.LoginID != p.LoginID {
				continue
			}
			w := effectiveWindow(c, revocations).Intersect(plan)
			if w.Empty() {
				continue
			}
			for _, f := range c.Fields {
				if IsMachineField(f) {
					continue // process-level data requires user scope; device fields never come from user consent
				}
				fields[f] = append(fields[f], w)
			}
		}
		res.Processes = append(res.Processes, ProcessPlan{
			PID:     p.PID,
			Windows: combine(mergeAll(fields)),
		})
	}
	return res
}

// effectiveWindow narrows a consent's window by every applicable
// revocation: the consent stops being valid at the earliest revocation
// moment.
func effectiveWindow(c Consent, revs []Revocation) Interval {
	w := c.Window
	for _, r := range revs {
		if r.ConsentID != "" {
			if r.ConsentID != c.ID {
				continue
			}
		} else if r.By != c.GrantedBy {
			continue
		}
		if r.At.Before(w.End) {
			w.End = r.At
		}
	}
	return w
}

// mergeAll sorts and merges overlapping or touching intervals per field.
func mergeAll(perField map[DataField][]Interval) map[DataField][]Interval {
	out := make(map[DataField][]Interval, len(perField))
	for f, ivs := range perField {
		sort.Slice(ivs, func(i, j int) bool {
			if ivs[i].Start.Equal(ivs[j].Start) {
				return ivs[i].End.Before(ivs[j].End)
			}
			return ivs[i].Start.Before(ivs[j].Start)
		})
		var merged []Interval
		for _, iv := range ivs {
			if iv.Empty() {
				continue
			}
			if n := len(merged); n > 0 && !iv.Start.After(merged[n-1].End) {
				if iv.End.After(merged[n-1].End) {
					merged[n-1].End = iv.End
				}
				continue
			}
			merged = append(merged, iv)
		}
		out[f] = merged
	}
	return out
}

// combine turns per-field interval lists into a piecewise-constant timeline
// of sub-intervals, each carrying the set of fields allowed during it.
func combine(perField map[DataField][]Interval) []FieldWindow {
	var bounds []time.Time
	for _, ivs := range perField {
		for _, iv := range ivs {
			if iv.Empty() {
				continue
			}
			bounds = append(bounds, iv.Start, iv.End)
		}
	}
	if len(bounds) == 0 {
		return nil
	}
	sort.Slice(bounds, func(i, j int) bool { return bounds[i].Before(bounds[j]) })
	dedup := bounds[:1]
	for _, t := range bounds[1:] {
		if !t.Equal(dedup[len(dedup)-1]) {
			dedup = append(dedup, t)
		}
	}

	var out []FieldWindow
	for i := 0; i+1 < len(dedup); i++ {
		seg := Interval{Start: dedup[i], End: dedup[i+1]}
		var fs []DataField
		for f, ivs := range perField {
			for _, iv := range ivs {
				if !iv.Start.After(seg.Start) && !iv.End.Before(seg.End) {
					fs = append(fs, f)
					break
				}
			}
		}
		if len(fs) == 0 {
			continue
		}
		sort.Slice(fs, func(a, b int) bool { return fs[a] < fs[b] })
		if n := len(out); n > 0 && out[n-1].Interval.End.Equal(seg.Start) && equalFields(out[n-1].Fields, fs) {
			out[n-1].Interval.End = seg.End
			continue
		}
		out = append(out, FieldWindow{Interval: seg, Fields: fs})
	}
	return out
}

func equalFields(a, b []DataField) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
