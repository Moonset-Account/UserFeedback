/**
 * Test-only helpers.  Nothing here is imported by the orchestrator itself.
 *
 * The remote services are modelled as an *independent* backend that survives
 * orchestrator restarts, exactly like a real service with an idempotency
 * store keyed by the operation credential.  That is what makes a re-issued
 * in-doubt command safe: the credential is identical, so the backend returns
 * the original outcome instead of applying the effect again.
 */

import { setTimeout as sleep } from 'node:timers/promises'

export class SimulatedCrash extends Error {
  constructor(message = 'simulated process crash') {
    super(message)
    this.name = 'SimulatedCrash'
  }
}

/**
 * @typedef {object} Behaviour
 * @property {'ok'|'fail'} [type]
 * @property {string} [message]
 * @property {number} [delayMs]
 */

/**
 * @param {object} [options]
 * @param {(ctx: object) => any} [options.result]  custom forward result factory
 */
export function createServiceHarness(options = {}) {
  const calls = []
  /** @type {Map<string, Behaviour[]>} */
  const behaviours = new Map()
  const forwardEffects = new Map() // forward credential -> result
  const compensationEffects = new Map() // compensate credential -> true

  const key = (stepId, phase) => `${stepId}:${phase}`

  function nextBehaviour(stepId, phase) {
    const queue = behaviours.get(key(stepId, phase))
    if (!queue || queue.length === 0) return { type: 'ok' }
    if (queue.length === 1) return queue[0] // a single behaviour repeats
    return queue.shift()
  }

  async function invoke(phase, ctx) {
    const call = {
      order: calls.length + 1,
      phase,
      stepId: ctx.stepId,
      service: ctx.service,
      credential: ctx.credential,
      commandId: ctx.commandId,
      attempt: ctx.attempt,
      deduped: false,
      outcome: 'ok',
    }
    calls.push(call)

    const behaviour = nextBehaviour(ctx.stepId, phase)
    if (behaviour.delayMs) await sleep(behaviour.delayMs)
    if (behaviour.type === 'fail') {
      call.outcome = 'fail'
      throw new Error(
        behaviour.message ?? `synthetic failure in ${ctx.stepId}:${phase}`,
      )
    }

    if (phase === 'forward') {
      if (forwardEffects.has(ctx.credential)) {
        call.deduped = true
        return forwardEffects.get(ctx.credential)
      }
      const result = options.result
        ? options.result(ctx)
        : { stepId: ctx.stepId, appliedWith: ctx.credential }
      forwardEffects.set(ctx.credential, result)
      return result
    }

    if (compensationEffects.has(ctx.credential)) {
      call.deduped = true
      return { undone: true, deduped: true }
    }
    compensationEffects.set(ctx.credential, true)
    return { undone: true }
  }

  const services = new Proxy(
    {},
    {
      get(_target, name) {
        if (typeof name !== 'string') return undefined
        return {
          async forward(ctx) {
            return invoke('forward', { ...ctx, service: name })
          },
          async compensate(ctx) {
            return invoke('compensate', { ...ctx, service: name })
          },
        }
      },
    },
  )

  return {
    services,
    calls,
    /** Queue a behaviour for a specific step and phase. */
    script(stepId, phase, ...entries) {
      behaviours.set(key(stepId, phase), entries.map(normalise))
      return this
    },
    callsFor(stepId, phase) {
      return calls.filter(
        (call) =>
          call.stepId === stepId && (phase === undefined || call.phase === phase),
      )
    },
    /** Number of times the forward effect was *applied* (dedupes excluded). */
    appliedForwardCount(stepId) {
      return calls.filter(
        (call) =>
          call.stepId === stepId &&
          call.phase === 'forward' &&
          !call.deduped &&
          call.outcome === 'ok',
      ).length
    },
    /** Number of times the compensation effect was *applied* (dedupes excluded). */
    appliedCompensationCount(stepId) {
      return calls.filter(
        (call) =>
          call.stepId === stepId &&
          call.phase === 'compensate' &&
          !call.deduped &&
          call.outcome === 'ok',
      ).length
    },
    credentialsFor(stepId, phase) {
      return this.callsFor(stepId, phase).map((call) => call.credential)
    },
    reset() {
      calls.length = 0
      behaviours.clear()
      forwardEffects.clear()
      compensationEffects.clear()
    },
  }
}

function normalise(entry) {
  if (entry === 'ok') return { type: 'ok' }
  if (entry === 'fail') return { type: 'fail' }
  if (typeof entry === 'object' && entry !== null) return { ...entry }
  throw new TypeError(`unsupported behaviour: ${String(entry)}`)
}

/**
 * Event-log wrapper that simulates the process dying while a particular event
 * is being recorded: the matching append is discarded and a SimulatedCrash is
 * thrown.  Everything recorded before it is durable.
 */
export class CrashingLog {
  #inner
  #shouldCrash
  #crashed = false

  /**
   * @param {{append: Function, events: Function, snapshot?: Function}} inner
   * @param {(event: object) => boolean} shouldCrash
   */
  constructor(inner, shouldCrash) {
    this.#inner = inner
    this.#shouldCrash = shouldCrash
  }

  append(event) {
    if (!this.#crashed && this.#shouldCrash(event)) {
      this.#crashed = true
      throw new SimulatedCrash(`crashed while recording ${event.type}`)
    }
    return this.#inner.append(event)
  }

  events() {
    return this.#inner.events()
  }

  snapshot() {
    return this.#inner.snapshot()
  }

  get crashed() {
    return this.#crashed
  }
}

/**
 * Build a diamond plan:  root -> {left, right} -> join
 *
 * @param {object} services
 * @param {{join?: boolean}} [options]
 */
export function diamondPlan(services, options = {}) {
  const steps = [
    { id: 'root', service: 'storage' },
    { id: 'left', service: 'network', dependsOn: ['root'] },
    { id: 'right', service: 'compute', dependsOn: ['root'] },
  ]
  if (options.join !== false) {
    steps.push({ id: 'join', service: 'database', dependsOn: ['left', 'right'] })
  }
  return { steps, services }
}
