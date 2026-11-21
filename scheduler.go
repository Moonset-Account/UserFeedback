// Package scheduler implements a discrete-event batch scheduler with
// conservative backfilling and an advanceable virtual clock.
//
// Jobs run non-preemptively and each job must obtain all of its slots at the
// same time. When the head of the waiting queue cannot start immediately, its
// earliest guaranteed start time (reservation) is derived from the reliable
// upper bounds (Runtime) of the running jobs. Later jobs may only backfill
// when they fit now and do not prevent the head from obtaining enough slots at
// its reservation time. Jobs that finish earlier than their upper bound may
// let the head start earlier; the reservation is recomputed from the jobs that
// are still running on every scheduling pass.
package scheduler

import (
	"errors"
	"sort"
)

// Job is one batch job submitted to the cluster.
//
// Time is expressed in an abstract integer unit. Runtime is a reliable upper
// bound and drives reservations. ActualRuntime models an observed early
// finish: zero (or any non-positive value) means "use Runtime", while a
// positive value no greater than Runtime is the duration actually charged.
type Job struct {
	ID            string
	Arrival       int64
	Slots         int
	Runtime       int64
	ActualRuntime int64
}

// Result records the simulated lifecycle of one job.
type Result struct {
	JobID    string
	Arrival  int64
	Start    int64
	Finish   int64
	Slots    int
	Estimate int64 // runtime upper bound
	Actual   int64 // duration that was actually charged
}

type running struct {
	job       Job
	start     int64
	estEnd    int64
	actualEnd int64
}

// Scheduler is a virtual-clock, conservative-backfill batch scheduler.
type Scheduler struct {
	totalSlots int
	now        int64

	// future holds submitted jobs whose arrival time has not been processed.
	// waiting holds jobs that have arrived and already entered the queue, in
	// strict queue order (arrival time, then submission order).
	future   []Job
	waiting  []Job
	running  []running
	finished []Result
	byID     map[string]Result

	// headReservation is the guaranteed start time of the current queue head,
	// or -1 when the head fits immediately and needs no reservation.
	headReservation int64
	headID          string
}

// New creates a scheduler for a cluster with the given number of slots.
func New(totalSlots int) (*Scheduler, error) {
	if totalSlots <= 0 {
		return nil, errors.New("scheduler: total slots must be positive")
	}
	return &Scheduler{
		totalSlots:      totalSlots,
		headReservation: -1,
		byID:            make(map[string]Result),
	}, nil
}

// Submit enqueues a job. Jobs are held until their arrival time and then
// considered strictly in submission order among jobs available at the same
// time.
func (s *Scheduler) Submit(j Job) error {
	if j.ID == "" {
		return errors.New("scheduler: job id must not be empty")
	}
	if _, exists := s.byID[j.ID]; exists {
		return errors.New("scheduler: duplicate job id: " + j.ID)
	}
	if j.Slots <= 0 {
		return errors.New("scheduler: job " + j.ID + " must request at least one slot")
	}
	if j.Slots > s.totalSlots {
		return errors.New("scheduler: job " + j.ID + " requests more slots than the cluster")
	}
	if j.Runtime <= 0 {
		return errors.New("scheduler: job " + j.ID + " must have a positive runtime bound")
	}
	if j.Arrival < 0 {
		return errors.New("scheduler: job " + j.ID + " has a negative arrival time")
	}
	if j.ActualRuntime > j.Runtime {
		return errors.New("scheduler: job " + j.ID + " actual runtime exceeds its reliable upper bound")
	}
	s.byID[j.ID] = Result{} // reserve the id; replaced when the job finishes
	s.future = append(s.future, j)
	return nil
}

// Now returns the current virtual time.
func (s *Scheduler) Now() int64 { return s.now }

// FreeSlots returns the slots currently available.
func (s *Scheduler) FreeSlots() int {
	used := 0
	for _, r := range s.running {
		used += r.job.Slots
	}
	return s.totalSlots - used
}

// PendingCount returns the number of submitted jobs that have not started,
// including jobs whose arrival time is still in the future.
func (s *Scheduler) PendingCount() int { return len(s.waiting) + len(s.future) }

// RunningCount returns the number of currently running jobs.
func (s *Scheduler) RunningCount() int { return len(s.running) }

// Reservation reports the current head-of-queue reservation. ok is false when
// there is no waiting head or the head fits without a reservation.
func (s *Scheduler) Reservation() (jobID string, at int64, ok bool) {
	if s.headReservation < 0 || s.headID == "" {
		return "", 0, false
	}
	return s.headID, s.headReservation, true
}

// Results returns finished jobs in completion order.
func (s *Scheduler) Results() []Result {
	out := make([]Result, len(s.finished))
	copy(out, s.finished)
	return out
}

// ResultOf returns the recorded result for a finished job.
func (s *Scheduler) ResultOf(id string) (Result, bool) {
	r, ok := s.byID[id]
	if !ok || r.JobID == "" {
		return Result{}, false
	}
	return r, true
}

// Idle reports whether nothing is running and nothing remains pending.
func (s *Scheduler) Idle() bool {
	return len(s.running) == 0 && len(s.waiting) == 0 && len(s.future) == 0
}

// Step advances the virtual clock to the next event (arrival or completion),
// processes all events at that time, and runs one scheduling pass. At equal
// times, slots of every finishing job are returned before any job starts. It
// returns the new virtual time and false only when no event remains.
func (s *Scheduler) Step() (int64, bool) {
	t, ok := s.nextEvent()
	if !ok {
		return s.now, false
	}
	s.now = t

	// Return slots of all jobs finishing at this time first.
	var still []running
	for _, r := range s.running {
		if r.actualEnd <= t {
			res := Result{
				JobID:    r.job.ID,
				Arrival:  r.job.Arrival,
				Start:    r.start,
				Finish:   r.actualEnd,
				Slots:    r.job.Slots,
				Estimate: r.job.Runtime,
				Actual:   r.actualEnd - r.start,
			}
			s.finished = append(s.finished, res)
			s.byID[r.job.ID] = res
		} else {
			still = append(still, r)
		}
	}
	s.running = still

	// Move every job that has arrived by now into the waiting queue.
	sort.SliceStable(s.future, func(i, j int) bool {
		return s.future[i].Arrival < s.future[j].Arrival
	})
	drained := 0
	for drained < len(s.future) && s.future[drained].Arrival <= t {
		s.waiting = append(s.waiting, s.future[drained])
		drained++
	}
	s.future = s.future[drained:]

	s.schedule()
	return s.now, true
}

// Run drives the simulation until every submitted job has finished and returns
// all results in completion order.
func (s *Scheduler) Run() []Result {
	for {
		if _, ok := s.Step(); !ok {
			return s.Results()
		}
	}
}

// nextEvent finds the smallest time at which something new happens: a job
// arrival (including one due at the current time that has not been processed)
// or a completion strictly after the current time.
func (s *Scheduler) nextEvent() (int64, bool) {
	var t int64
	found := false
	consider := func(v int64) {
		if !found || v < t {
			t, found = v, true
		}
	}
	for _, j := range s.future {
		consider(j.Arrival)
	}
	for _, r := range s.running {
		if r.actualEnd > s.now {
			consider(r.actualEnd)
		}
	}
	if !found {
		return 0, false
	}
	if t < s.now {
		t = s.now // late submission whose arrival time already passed
	}
	return t, true
}

// schedule performs one scheduling pass over the waiting queue.
func (s *Scheduler) schedule() {
	if len(s.waiting) == 0 {
		s.headID, s.headReservation = "", -1
		return
	}

	lead := s.waiting[0]

	// The head starts immediately whenever its slots are free.
	if lead.Slots <= s.FreeSlots() {
		s.start(lead)
		s.waiting = s.waiting[1:]
		s.headID, s.headReservation = "", -1
		// Restart the pass: the next job becomes the new head.
		s.schedule()
		return
	}

	// The head must wait: compute its earliest guaranteed start time from the
	// reliable runtime upper bounds of the jobs still running.
	res := s.reservation(lead.Slots, s.FreeSlots())
	s.headID, s.headReservation = lead.ID, res

	// Later jobs try to backfill in strict queue order. A backfill must fit
	// now and leave the head enough slots at the reservation time.
	kept := []Job{lead}
	for _, j := range s.waiting[1:] {
		if j.Slots <= s.FreeSlots() && s.preserves(lead.Slots, res, j) {
			s.start(j)
		} else {
			kept = append(kept, j)
		}
	}
	s.waiting = kept
}

// reservation returns the earliest time at which need slots are guaranteed to
// be free, based on estimated end times of currently running jobs.
func (s *Scheduler) reservation(need, free int) int64 {
	ends := make([]running, len(s.running))
	copy(ends, s.running)
	sort.Slice(ends, func(i, j int) bool { return ends[i].estEnd < ends[j].estEnd })

	available := free
	var t int64 = s.now
	for _, r := range ends {
		if available >= need {
			break
		}
		available += r.job.Slots
		t = r.estEnd
	}
	return t
}

// preserves reports whether starting backfill j still leaves need slots
// available to the queue head at reservation time r. Slots that running jobs
// (including the backfill itself) will have released by r are counted.
func (s *Scheduler) preserves(need int, r int64, j Job) bool {
	available := s.FreeSlots() - j.Slots
	for _, run := range s.running {
		if run.estEnd <= r {
			available += run.job.Slots
		}
	}
	// The backfill itself releases its slots if its estimate ends by r.
	if s.now+j.Runtime <= r {
		available += j.Slots
	}
	return available >= need
}

// start launches a job at the current virtual time.
func (s *Scheduler) start(j Job) {
	actual := j.ActualRuntime
	if actual <= 0 {
		actual = j.Runtime
	}
	s.running = append(s.running, running{
		job:       j,
		start:     s.now,
		estEnd:    s.now + j.Runtime,
		actualEnd: s.now + actual,
	})
}
