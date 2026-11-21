import { createHash } from 'node:crypto'
import { validatePlan } from './graph.mjs'
import { credentialFor } from './credentials.mjs'

/**
 * @typedef {'pending'|'in_doubt'|'succeeded'|'failed'} ForwardState
 * @typedef {'not_needed'|'in_doubt'|'retryable'|'compensated'|'abandoned'} CompensationState
 */

function serialiseError(error) {
  if (error instanceof Error) {
    return { name: error.name, message: error.message }
  }
  return { name: 'Error', message: String(error) }
}

function planFingerprint(plan) {
  const canonical = plan.steps.map((step) => ({
    id: step.id,
    service: step.service,
    dependsOn: [...(step.dependsOn ?? [])].sort(),
  }))
  return createHash('sha256').update(JSON.stringify(canonical)).digest('hex')
}

/**
 * Rebuild the full orchestrator state from the event log.  The log is the
 * only source of truth: a fresh process holding the log can continue exactly
 * where the previous one stopped.
 *
 * Projection fields per step:
 *   forward.state       pending | in_doubt | succeeded | failed
 *   compensation.state  not_needed | in_doubt | retryable | compensated | abandoned
 *
 * `in_doubt` means a `step.dispatched` was recorded without a terminal event,
 * i.e. an *unconfirmed command*.  It is re-issued on replay with the same
 * commandId (same operation credential).
 *
 * @param {ReadonlyArray<object>} events
 * @param {{steps: Array<{id: string}>}} plan
 */
export function projectEvents(events, plan) {
  const forward = new Map()
  const compensation = new Map()
  for (const step of plan.steps) {
    forward.set(step.id, {
      state: 'pending',
      result: undefined,
      error: undefined,
      attempt: 0,
      commandId: undefined,
    })
    compensation.set(step.id, {
      state: 'not_needed',
      error: undefined,
      attempt: 0,
      commandId: undefined,
      reason: undefined,
      blockedBy: undefined,
    })
  }

  const projection = {
    started: false,
    runId: undefined,
    aborted: false,
    failedStepId: undefined,
    abortReason: undefined,
    finished: undefined,
    forward,
    compensation,
  }
  const known = new Set(plan.steps.map((step) => step.id))

  for (const event of events) {
    switch (event.type) {
      case 'run.started':
        projection.started = true
        projection.runId = event.runId
        break

      case 'step.dispatched': {
        if (!known.has(event.stepId)) break
        const target =
          event.phase === 'forward'
            ? forward.get(event.stepId)
            : compensation.get(event.stepId)
        target.state = 'in_doubt'
        target.commandId = event.commandId
        target.attempt = event.attempt
        break
      }

      case 'step.succeeded': {
        if (!known.has(event.stepId)) break
        const target = forward.get(event.stepId)
        target.state = 'succeeded'
        target.result = event.result
        target.attempt = event.attempt
        target.commandId = event.commandId
        break
      }

      case 'step.failed': {
        if (!known.has(event.stepId)) break
        const target = forward.get(event.stepId)
        target.state = 'failed'
        target.error = event.error
        target.attempt = event.attempt
        target.commandId = event.commandId
        // A failure is itself enough to stop new work.  Deriving the abort
        // from the failure (not only from `run.aborted`) keeps replay
        // correct if the process died between recording the two events.
        projection.aborted = true
        projection.failedStepId ??= event.stepId
        projection.abortReason ??= event.error?.message
        break
      }

      case 'step.compensated': {
        if (!known.has(event.stepId)) break
        const target = compensation.get(event.stepId)
        target.state = 'compensated'
        target.error = undefined
        target.attempt = event.attempt
        target.commandId = event.commandId
        break
      }

      case 'step.compensation_failed': {
        if (!known.has(event.stepId)) break
        const target = compensation.get(event.stepId)
        target.state = 'retryable'
        target.error = event.error
        target.attempt = event.attempt
        target.commandId = event.commandId
        break
      }

      case 'step.abandoned': {
        if (!known.has(event.stepId)) break
        const target = compensation.get(event.stepId)
        target.state = 'abandoned'
        target.reason = event.reason
        target.blockedBy = event.blockedBy
        break
      }

      case 'run.aborted':
        projection.aborted = true
        projection.failedStepId ??= event.failedStepId
        projection.abortReason ??= event.reason
        break

      case 'run.finished':
        projection.finished = event
        break

      default:
        break
    }
  }

  return projection
}

/**
 * Event-sourced DAG saga orchestrator.
 *
 * Scheduling guarantees:
 *  - a step is dispatched only when *all* of its direct prerequisites succeeded;
 *  - after the first observed failure no new forward step is started, while
 *    every already-dispatched forward call is awaited to a conclusion;
 *  - successful steps are then compensated in reverse dependency order;
 *  - a step becomes compensatable only once every direct successor that
 *    actually succeeded has been compensated;
 *  - compensation retries reuse the same operation credential;
 *  - reopening from the log resumes unconfirmed commands with their original
 *    credential, so a re-issued call can never double-apply an effect.
 */
export class Orchestrator {
  #plan
  #services
  #graph
  #log
  #runId
  #maxCompensationAttempts
  #credentials

  /**
   * @param {object} options
   * @param {object} options.plan       {steps, services}
   * @param {string} options.runId
   * @param {object} options.log        event log (append/events)
   * @param {object} [options.services] override service implementations
   * @param {number} [options.maxCompensationAttempts=3]
   */
  constructor({ plan, runId, log, services, maxCompensationAttempts = 3 }) {
    if (typeof runId !== 'string' || runId.length === 0) {
      throw new TypeError('runId must be a non-empty string')
    }
    if (!Number.isInteger(maxCompensationAttempts) || maxCompensationAttempts < 1) {
      throw new TypeError('maxCompensationAttempts must be a positive integer')
    }
    if (!log || typeof log.append !== 'function' || typeof log.events !== 'function') {
      throw new TypeError('log must provide append() and events()')
    }
    this.#plan = plan
    this.#graph = validatePlan(plan)
    this.#services = services ?? plan.services
    this.#log = log
    this.#runId = runId
    this.#maxCompensationAttempts = maxCompensationAttempts
    this.#credentials = new Map(
      this.#graph.ids.map((id) => [
        id,
        {
          forward: credentialFor(runId, id, 'forward'),
          compensate: credentialFor(runId, id, 'compensate'),
        },
      ]),
    )
  }

  /** @returns {ReadonlyArray<object>} the durable log so far */
  events() {
    return this.#log.events()
  }

  /**
   * Drive the run to a terminal state.  Safe to call again after a crash: it
   * resumes from the log and continues unconfirmed commands.
   *
   * @returns {Promise<object>} run summary
   */
  async run() {
    let state = projectEvents(this.#log.events(), this.#plan)

    this.#validateReplay()

    if (state.finished) return this.#summarise(state)

    if (!state.started) {
      this.#startRun()
      state = projectEvents(this.#log.events(), this.#plan)
    }

    state = await this.#drainForward(state)

    if (!state.aborted) {
      this.#append({
        type: 'run.finished',
        status: 'completed',
        failedStepId: null,
        residualEffects: [],
      })
      return this.#summarise(projectEvents(this.#log.events(), this.#plan))
    }

    state = await this.#drainCompensation(state)
    state = this.#abandonBlockedSteps(state)

    const residualEffects = this.#residualEffects(state)
    this.#append({
      type: 'run.finished',
      status: residualEffects.length === 0 ? 'compensated' : 'compensation_incomplete',
      failedStepId: state.failedStepId ?? null,
      residualEffects,
    })
    return this.#summarise(projectEvents(this.#log.events(), this.#plan))
  }

  // ------------------------------------------------------------------
  // forward phase
  // ------------------------------------------------------------------

  /**
   * Dispatch ready steps in waves and await every issued call.  Returns the
   * updated projection.
   */
  async #drainForward(state) {
    const order = this.#graph.order
    const orderIndex = new Map(order.map((id, index) => [id, index]))

    for (;;) {
      const wave = []

      // Unconfirmed forward commands from a previous process must always be
      // resumed: they already may have side effects pending.  This happens
      // even after an abort, because the call is in flight, not new work.
      // A resume is the *same* command, so it keeps its original attempt
      // number and credential.
      for (const id of order) {
        const forward = state.forward.get(id)
        if (forward.state === 'in_doubt') {
          wave.push({
            id,
            phase: 'forward',
            attempt: forward.attempt,
            commandId: this.#credentials.get(id).forward,
          })
        }
      }

      // No new forward step may start after the first failure.
      if (!state.aborted) {
        for (const id of order) {
          const forward = state.forward.get(id)
          if (forward.state !== 'pending') continue
          const ready = this.#graph.prerequisites
            .get(id)
            .every((dep) => state.forward.get(dep).state === 'succeeded')
          if (ready) {
            wave.push({
              id,
              phase: 'forward',
              attempt: 1,
              commandId: this.#credentials.get(id).forward,
            })
          }
        }
      }

      if (wave.length === 0) break
      wave.sort((a, b) => orderIndex.get(a.id) - orderIndex.get(b.id))

      const outcomes = await this.#dispatch(wave, state)

      for (const outcome of outcomes) {
        const { entry } = outcome
        const forward = state.forward.get(entry.id)
        forward.attempt = entry.attempt
        forward.commandId = entry.commandId

        if (outcome.ok) {
          this.#append({
            type: 'step.succeeded',
            stepId: entry.id,
            commandId: entry.commandId,
            attempt: entry.attempt,
            result: outcome.value,
          })
          forward.state = 'succeeded'
          forward.result = outcome.value
          forward.error = undefined
        } else {
          this.#append({
            type: 'step.failed',
            stepId: entry.id,
            commandId: entry.commandId,
            attempt: entry.attempt,
            error: outcome.error,
          })
          forward.state = 'failed'
          forward.error = outcome.error

          if (!state.aborted) {
            state.aborted = true
            state.failedStepId = entry.id
            state.abortReason = outcome.error.message
            this.#append({
              type: 'run.aborted',
              failedStepId: entry.id,
              reason: outcome.error.message,
            })
          }
        }
      }
      // Loop again only to drain remaining in-doubt forwards; `state.aborted`
      // prevents starting any new step.
    }

    return state
  }

  // ------------------------------------------------------------------
  // compensation phase
  // ------------------------------------------------------------------

  /**
   * Compensate every successful step exactly once, respecting the rule that
   * a step may only be undone after all of its successful direct successors
   * are undone.  Retries are driven from the log so an interrupted run can be
   * resumed.
   */
  async #drainCompensation(state) {
    const order = this.#graph.order
    const orderIndex = new Map(order.map((id, index) => [id, index]))

    for (;;) {
      const wave = []

      for (const id of order) {
        const compensation = state.compensation.get(id)
        if (compensation.state === 'in_doubt') {
          // A resume of the same unconfirmed command keeps its attempt number.
          wave.push({
            id,
            phase: 'compensate',
            attempt: compensation.attempt,
            commandId: this.#credentials.get(id).compensate,
          })
        } else if (compensation.state === 'retryable') {
          wave.push({
            id,
            phase: 'compensate',
            attempt: compensation.attempt + 1,
            commandId: this.#credentials.get(id).compensate,
          })
        }
      }

      for (const id of order) {
        if (state.forward.get(id).state !== 'succeeded') continue
        if (state.compensation.get(id).state !== 'not_needed') continue
        if (this.#successorsCompensated(id, state)) {
          wave.push({
            id,
            phase: 'compensate',
            attempt: 1,
            commandId: this.#credentials.get(id).compensate,
          })
        }
      }

      if (wave.length === 0) break
      // Reverse topological order within a wave: successors go before their
      // prerequisites where both are eligible.
      wave.sort((a, b) => orderIndex.get(b.id) - orderIndex.get(a.id))

      // In-doubt/retry entries must not duplicate a not_needed entry for the
      // same step; the two loops above are disjoint by construction.
      const unique = dedupeWave(wave)

      const outcomes = await this.#dispatch(unique, state)

      for (const outcome of outcomes) {
        const { entry } = outcome
        const compensation = state.compensation.get(entry.id)
        compensation.attempt = entry.attempt
        compensation.commandId = entry.commandId

        if (outcome.ok) {
          this.#append({
            type: 'step.compensated',
            stepId: entry.id,
            commandId: entry.commandId,
            attempt: entry.attempt,
          })
          compensation.state = 'compensated'
          compensation.error = undefined
        } else {
          this.#append({
            type: 'step.compensation_failed',
            stepId: entry.id,
            commandId: entry.commandId,
            attempt: entry.attempt,
            error: outcome.error,
          })
          compensation.error = outcome.error

          if (entry.attempt >= this.#maxCompensationAttempts) {
            compensation.state = 'abandoned'
            compensation.reason = 'compensation_failed'
            this.#append({
              type: 'step.abandoned',
              stepId: entry.id,
              commandId: entry.commandId,
              attempt: entry.attempt,
              reason: 'compensation_failed',
            })
          } else {
            compensation.state = 'retryable'
          }
        }
      }
    }

    return state
  }

  /**
   * A step may be compensated only once every direct successor that actually
   * succeeded has itself been compensated.  A successor that never succeeded
   * (failed, or was never started) imposes no ordering constraint.
   */
  #successorsCompensated(id, state) {
    for (const dependent of this.#graph.dependents.get(id)) {
      if (state.forward.get(dependent).state !== 'succeeded') continue
      if (state.compensation.get(dependent).state !== 'compensated') return false
    }
    return true
  }

  /**
   * Steps whose successful successor can never be compensated are recorded
   * as deliberately abandoned rather than silently violating the ordering.
   */
  #abandonBlockedSteps(state) {
    const order = [...this.#graph.order].reverse()
    for (const id of order) {
      if (state.forward.get(id).state !== 'succeeded') continue
      const compensation = state.compensation.get(id)
      if (compensation.state !== 'not_needed') continue

      const blocking = this.#graph.dependents.get(id).filter((dependent) => {
        if (state.forward.get(dependent).state !== 'succeeded') return false
        return state.compensation.get(dependent).state !== 'compensated'
      })
      if (blocking.length === 0) continue

      compensation.state = 'abandoned'
      compensation.reason = 'blocked_by_successor'
      compensation.blockedBy = blocking
      this.#append({
        type: 'step.abandoned',
        stepId: id,
        reason: 'blocked_by_successor',
        blockedBy: blocking,
      })
    }
    return state
  }

  #residualEffects(state) {
    const residual = []
    for (const step of this.#plan.steps) {
      if (state.forward.get(step.id).state !== 'succeeded') continue
      const compensation = state.compensation.get(step.id)
      if (compensation.state === 'compensated') continue
      residual.push({
        stepId: step.id,
        service: step.service,
        state: compensation.state,
        reason: compensation.reason ?? 'not_compensated',
        blockedBy: compensation.blockedBy ?? null,
        error: compensation.error ?? null,
      })
    }
    return residual
  }

  // ------------------------------------------------------------------
  // dispatch / service invocation
  // ------------------------------------------------------------------

  /**
   * Invoke every entry in the wave concurrently and wait for *all* of them to
   * reach a conclusion before returning.  Each outcome records the moment it
   * was received, so "the first failure" reflects arrival order regardless of
   * dispatch order.
   */
  async #dispatch(wave, state) {
    for (const entry of wave) {
      this.#append({
        type: 'step.dispatched',
        stepId: entry.id,
        phase: entry.phase,
        commandId: entry.commandId,
        attempt: entry.attempt,
      })
    }

    const outcomes = []
    let received = 0

    await Promise.all(
      wave.map(async (entry) => {
        let outcome
        try {
          const value = await this.#invoke(entry, state)
          outcome = { entry, ok: true, value }
        } catch (error) {
          outcome = { entry, ok: false, error: serialiseError(error) }
        }
        outcome.received = ++received
        outcomes.push(outcome)
      }),
    )

    outcomes.sort((a, b) => a.received - b.received)
    return outcomes
  }

  async #invoke(entry, state) {
    const step = this.#plan.steps.find((candidate) => candidate.id === entry.id)
    const implementation = this.#services[step.service]
    if (!implementation) {
      throw new Error(`no service implementation for "${step.service}"`)
    }

    const base = {
      runId: this.#runId,
      stepId: entry.id,
      phase: entry.phase,
      commandId: entry.commandId,
      credential: entry.commandId,
      attempt: entry.attempt,
      data: step.data,
    }

    if (entry.phase === 'forward') {
      if (typeof implementation.forward !== 'function') {
        throw new Error(`service "${step.service}" has no forward()`)
      }
      const results = {}
      for (const dependency of this.#graph.prerequisites.get(entry.id)) {
        results[dependency] = state.forward.get(dependency).result
      }
      return implementation.forward({ ...base, results })
    }

    if (typeof implementation.compensate !== 'function') {
      throw new Error(`service "${step.service}" has no compensate()`)
    }
    return implementation.compensate({
      ...base,
      result: state.forward.get(entry.id).result,
    })
  }

  // ------------------------------------------------------------------
  // log / summary
  // ------------------------------------------------------------------

  #startRun() {
    this.#append({
      type: 'run.started',
      runId: this.#runId,
      planFingerprint: planFingerprint(this.#plan),
      steps: this.#plan.steps.map((step) => ({
        id: step.id,
        service: step.service,
        dependsOn: [...(step.dependsOn ?? [])],
      })),
      credentials: Object.fromEntries(
        this.#graph.ids.map((id) => [
          id,
          {
            forward: this.#credentials.get(id).forward,
            compensate: this.#credentials.get(id).compensate,
          },
        ]),
      ),
    })
  }

  /**
   * Replaying a log against a different plan (or run) would silently attach
   * events to the wrong steps.  Refuse instead.
   */
  #validateReplay() {
    const started = this.#log.events().find((event) => event.type === 'run.started')
    if (!started) return
    if (started.runId !== this.#runId) {
      throw new Error(
        `log belongs to run "${started.runId}", not "${this.#runId}"`,
      )
    }
    if (started.planFingerprint !== planFingerprint(this.#plan)) {
      throw new Error('log was recorded for a different plan')
    }
  }

  #append(event) {
    return this.#log.append(event)
  }

  #summarise(state) {
    return {
      runId: this.#runId,
      status: state.finished?.status ?? 'running',
      aborted: state.aborted,
      failedStepId: state.finished?.failedStepId ?? state.failedStepId ?? null,
      residualEffects: state.finished?.residualEffects ?? [],
      steps: this.#plan.steps.map((step) => ({
        id: step.id,
        forward: state.forward.get(step.id).state,
        compensation: state.compensation.get(step.id).state,
        attempts: {
          forward: state.forward.get(step.id).attempt,
          compensate: state.compensation.get(step.id).attempt,
        },
      })),
    }
  }
}

function dedupeWave(wave) {
  const seen = new Set()
  const result = []
  for (const entry of wave) {
    if (seen.has(entry.id)) continue
    seen.add(entry.id)
    result.push(entry)
  }
  return result
}
