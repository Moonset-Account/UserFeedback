package capacitycontroller

// Pump evaluates every window that has closed but not yet been published,
// freezes the verdicts, and returns one Decision per window.
//
// Missing evidence breaks the high/low streaks: an unverifiable window is
// published and resets both counters. A due action that cannot be emitted
// (an unacknowledged action is still pending, or cooldown is running) is
// retained as a blocked decision and retried on every later Pump; it is
// flushed at the first legal instant and never re-issued.
func (c *Controller) Pump(now int64) []Decision {
	c.mu.Lock()
	defer c.mu.Unlock()

	var out []Decision
	for {
		start := int64(c.order) * c.cfg.WindowSize
		end := start + c.cfg.WindowSize
		if now < end {
			break
		}
		v := c.evaluateWindow(c.order, start, end)
		c.frozen[c.order] = cloneVerdict(v)
		c.order++

		d := Decision{Verdict: cloneVerdict(v)}
		c.applyVerdict(v, now, &d)
		out = append(out, d)
	}

	// Retry a previously withheld action at the first legal instant.
	if c.blocked != nil && c.blockedEmittedAs == 0 && c.canIssue(now) {
		dir := ScaleIn
		if c.blocked.Verdict.Status == StatusHigh {
			dir = ScaleOut
		}
		act := c.issueAction(dir, c.blocked.Verdict, now)
		c.blockedEmittedAs = act.ID
		out = append(out, Decision{
			Verdict: cloneVerdict(c.blocked.Verdict),
			Action:  act,
			Flushed: true,
		})
		c.blocked = nil
		c.blockedEmittedAs = 0
	}
	return out
}

func (c *Controller) applyVerdict(v Verdict, now int64, d *Decision) {
	switch v.Status {
	case StatusHigh:
		c.highStreak++
		c.lowStreak = 0
		if c.blocked != nil && c.blocked.Verdict.Status == StatusLow {
			c.blocked, c.blockedEmittedAs = nil, 0
		}
		if c.highStreak >= c.cfg.OutStreak {
			c.requestAction(ScaleOut, v, now, d)
		}
	case StatusLow:
		c.lowStreak++
		c.highStreak = 0
		if c.blocked != nil && c.blocked.Verdict.Status == StatusHigh {
			c.blocked, c.blockedEmittedAs = nil, 0
		}
		if c.lowStreak >= c.cfg.InStreak {
			c.requestAction(ScaleIn, v, now, d)
		}
	default:
		// Normal and unverifiable windows both interrupt any streak:
		// missing evidence cannot bridge two pressure runs. A decision
		// that already became due (and is merely blocked) is retained,
		// because its own evidence remains valid; only opposite pressure
		// invalidates it.
		c.highStreak = 0
		c.lowStreak = 0
	}
}

// requestAction either emits the action now or parks it as the blocked
// decision. Emitting (now or later) resets the pressure streaks.
func (c *Controller) requestAction(dir Direction, v Verdict, now int64, d *Decision) {
	if c.canIssue(now) {
		d.Action = c.issueAction(dir, v, now)
		c.highStreak = 0
		c.lowStreak = 0
		return
	}
	d.Blocked = true
	// Newer pressure evidence replaces an older blocked decision; the
	// old candidate was never published and therefore never acknowledged.
	c.blocked = &Decision{Verdict: cloneVerdict(v)}
	c.blockedEmittedAs = 0
}

func (c *Controller) canIssue(now int64) bool {
	return c.pendingActionID == 0 && now >= c.cooldownUntil
}

func (c *Controller) issueAction(dir Direction, v Verdict, at int64) *Action {
	c.actionID++
	act := &Action{
		ID:        c.actionID,
		Window:    v.Window,
		Direction: dir,
		IssuedAt:  at,
	}
	c.actions = append(c.actions, act)
	c.pendingActionID = act.ID
	c.highStreak = 0
	c.lowStreak = 0
	return act
}
