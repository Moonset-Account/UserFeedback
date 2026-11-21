/**
 * Durable event log.
 *
 * The orchestrator never trusts in-memory state across a restart: every
 * decision is derived from the recorded events.  Each event carries a
 * monotonically increasing `seq` assigned by the log.
 *
 * Event types:
 *
 *   run.started               { runId, planFingerprint, steps, credentials }
 *   step.dispatched           { stepId, phase, commandId, attempt }
 *   step.succeeded            { stepId, commandId, attempt, result }
 *   step.failed               { stepId, commandId, attempt, error }
 *   step.compensated          { stepId, commandId, attempt }
 *   step.compensation_failed  { stepId, commandId, attempt, error }
 *   step.abandoned            { stepId, reason, blockedBy? }
 *   run.aborted               { failedStepId, reason }
 *   run.finished              { status, failedStepId, residualEffects }
 *
 * In-doubt commands: a `step.dispatched` without a matching terminal event is
 * an *unconfirmed* command.  On replay it is re-issued with the exact same
 * `commandId` (same operation credential), never a fresh one.
 */

export class MemoryEventLog {
  #events
  #seq

  constructor(events = []) {
    this.#events = []
    this.#seq = 0
    for (const event of events) this.append(event)
  }

  /**
   * Append one event.  Any caller-supplied `seq`/`ts` is ignored; the log
   * owns sequence numbers so replay stays deterministic.
   *
   * @param {object} event
   * @returns {object} the stored event
   */
  append(event) {
    if (!event || typeof event !== 'object' || typeof event.type !== 'string') {
      throw new TypeError('event.type is required')
    }
    const { seq: _seq, ts: _ts, ...rest } = event
    const stored = Object.freeze({ seq: ++this.#seq, ...rest })
    this.#events.push(stored)
    return stored
  }

  /**
   * @returns {ReadonlyArray<object>} live, read-only view of the log
   */
  events() {
    return this.#events
  }

  /**
   * A detached copy safe to persist as JSON or hand to a fresh process.
   * @returns {object[]}
   */
  snapshot() {
    return structuredClone(this.#events)
  }

  /**
   * Rebuild a log from a snapshot produced by `snapshot()`.
   * @param {object[]} events
   * @returns {MemoryEventLog}
   */
  static from(events) {
    return new MemoryEventLog(events)
  }
}
