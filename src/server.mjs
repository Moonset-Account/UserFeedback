// Idempotent execution entry point for an RPC service.
//
// Recovery contract, in one sentence: after a restart every resident record is
// an unconfirmed hypothesis — a record is only effective once its stable
// execution id is known and the business adapter answers `{status:'effective'}`
// for that exact id; an unresolved execution is re-confirmed with the *same*
// id, never with a freshly minted one.
//
// The execution id is written to the record before `adapter.execute()` is
// called. That write-ahead step is what makes a crash between "execution
// started" and "effect applied" recoverable: the restarted server can ask the
// business about the exact execution that may or may not have run. Only when
// the business certifies that execution never took effect (and no receipt was
// ever handed out for it) may the same id be driven again.

import { randomUUID } from 'node:crypto';

import { fingerprint, retryDeadline, validateParams } from './params.mjs';
import { Store } from './store.mjs';

const ok = (fields = {}) => ({ accepted: true, ...fields });
const rejected = (reason, fields = {}) => ({ accepted: false, reason, ...fields });

const PENDING = 'pending';
const EFFECTIVE = 'effective';
const UNKNOWN = 'unknown';

/** Identity of a parameter value; exposed so callers can inspect the dedupe key. */
export function parameterIdentity(params) {
  validateParams(params);
  return fingerprint(params);
}

export function createIdempotentExecutionServer(options = {}) {
  const {
    adapter,
    capacity = 1024,
    executionIdFactory = () => randomUUID(),
    clock = () => Date.now(),
    timers = { setTimeout, clearTimeout },
    snapshot,
  } = options;

  if (!adapter || typeof adapter.execute !== 'function' || typeof adapter.query !== 'function') {
    throw new TypeError('adapter must implement execute() and query()');
  }
  if (typeof executionIdFactory !== 'function') throw new TypeError('executionIdFactory must be a function');
  if (typeof clock !== 'function') throw new TypeError('clock must be a function');
  if (typeof timers.setTimeout !== 'function' || typeof timers.clearTimeout !== 'function') {
    throw new TypeError('timers must implement setTimeout() and clearTimeout()');
  }

  const now = () => clock();
  const store = new Store({ capacity });
  const flights = new Map(); // businessKey -> Flight (at most one per key)
  const resuming = new WeakSet(); // record -> a verification/execution is running

  // ---------------------------------------------------------------- records

  function makeRecord(fields) {
    // Snapshot the parameters: a caller mutating its own object after submit
    // must not rewrite the identity (or the operation) of a stored key.
    const parameters = structuredClone(fields.parameters);
    return {
      businessKey: fields.businessKey,
      parameters,
      identity: fingerprint(parameters),
      acceptedAt: fields.acceptedAt,
      retryDeadline: fields.retryDeadline,
      executionId: fields.executionId ?? null,
      outcome: fields.outcome ?? PENDING,
      effect: undefined,
      error: undefined,
      // The business certified the current execution id is unused, so the same
      // id may be driven again.
      safeToReuse: fields.safeToReuse ?? false,
      // A receipt was produced for this key at some point.
      delivered: fields.delivered ?? false,
      // Effect of a receipt that was recorded before a restart.
      priorReceiptEffect: fields.priorReceiptEffect,
    };
  }

  const flightFor = (record) => {
    const flight = flights.get(record.businessKey);
    return flight && flight.record === record ? flight : null;
  };

  function mayEvict(record) {
    if (flightFor(record)) return false;
    if (resuming.has(record)) return false;
    // An unresolved effect must never be forgotten: dropping its execution id
    // would force a later call to start a *new* execution, which may apply the
    // business effect a second time. Such records stay resident until recover()
    // gets a definite answer.
    return record.outcome !== UNKNOWN;
  }

  function admitNewRecord(record) {
    if (store.size < store.capacity) {
      store.insert(record);
      return { admitted: true };
    }
    const victim = store.evictionVictim(now(), mayEvict);
    if (!victim) return { admitted: false };
    store.purge(victim);
    store.insert(record);
    return { admitted: true, evicted: victim.businessKey };
  }

  // --------------------------------------------------------- flights/state

  function finishFlight(flight) {
    if (flight.state !== 'executing') return;
    flight.state = 'done';
    if (flights.get(flight.record.businessKey) === flight) {
      flights.delete(flight.record.businessKey);
    }
  }

  function settleEffective(flight, effect) {
    const record = flight.record;
    record.outcome = EFFECTIVE;
    record.effect = effect;
    record.error = undefined;
    record.safeToReuse = false;
    finishFlight(flight);
  }

  function settleNotEffective(flight, error) {
    const record = flight.record;
    record.outcome = PENDING;
    record.effect = undefined;
    record.error =
      error ?? {
        code: 'EXECUTION_NOT_EFFECTIVE',
        message: 'business confirmed this execution took no effect; retry with the same execution id',
      };
    finishFlight(flight);
  }

  function settleUnknown(flight, error) {
    const record = flight.record;
    record.outcome = UNKNOWN;
    record.effect = undefined;
    record.error =
      error ?? {
        code: 'EFFECT_UNKNOWN',
        message: 'business effect is still being confirmed; retry later with the same execution id',
      };
    finishFlight(flight);
  }

  /** Apply one adapter.execute() result. Never mints a new execution id. */
  function absorbExecuteResult(flight, result) {
    const outcome = result?.outcome;
    if (outcome === 'effective') return settleEffective(flight, result.effect);
    if (outcome === 'not_effective') {
      if (flight.record.delivered) {
        // A receipt was already produced for this key. Even though this run
        // reports no effect, re-running would risk a second application, so
        // the key stays unresolved rather than being declared reusable.
        return settleUnknown(flight, {
          code: 'RECEIPT_CONFLICT',
          message: 'a receipt was already recorded for this key; refusing to re-run',
        });
      }
      flight.record.safeToReuse = true;
      return settleNotEffective(flight, result.error);
    }
    if (outcome === 'unknown') return settleUnknown(flight, result.error);
    return settleUnknown(flight, {
      code: result?.error?.code ?? 'BAD_ADAPTER_RESULT',
      message: result?.error?.message ?? 'adapter returned an unrecognised result',
    });
  }

  async function driveExecute(flight, executionId) {
    const record = flight.record;
    flight.executionId = executionId;
    // Write-ahead: a restart must be able to query this exact execution even if
    // this process dies before the adapter returns.
    record.executionId = executionId;
    record.outcome = PENDING;
    record.safeToReuse = false;
    let result;
    try {
      result = await adapter.execute({
        executionId,
        businessKey: record.businessKey,
        operation: record.parameters,
        retryDeadline: record.retryDeadline,
        hadReceipt: record.delivered,
        freshVerify: flight.kind === 'verify',
      });
    } catch (error) {
      // A transport failure cannot distinguish "not applied" from "applied";
      // treat it as unknown, never as a licence to mint a new id.
      result = {
        outcome: 'unknown',
        error: {
          code: typeof error?.code === 'string' ? error.code : 'ADAPTER_THREW',
          message: typeof error?.message === 'string' ? error.message : 'adapter threw',
        },
      };
    }
    absorbExecuteResult(flight, result);
  }

  function startFlight(record, kind) {
    const flight = { record, kind, executionId: null, state: 'executing', promise: null };
    flights.set(record.businessKey, flight);
    return flight;
  }

  /** Attach a settlement guarantee so a flight can never stay "executing". */
  function guardFlight(flight, body) {
    flight.promise = (async () => {
      try {
        await body();
      } catch (error) {
        // An unexpected internal fault must still resolve the record, and it
        // must resolve it conservatively: unknown, never "safe to reuse".
        settleUnknown(flight, {
          code: 'INTERNAL_ERROR',
          message: typeof error?.message === 'string' ? error.message : 'unexpected internal error',
        });
      }
    })();
    flight.promise.catch(() => {});
    return flight;
  }

  function beginExecution(record) {
    const executionId = record.executionId ?? executionIdFactory();
    const flight = startFlight(record, 'execute');
    return guardFlight(flight, () => driveExecute(flight, executionId));
  }

  /**
   * Re-confirm an execution that may or may not have reached the business.
   * `unknown` leaves the record unresolved and is reported as "still
   * confirming"; only an explicit `not_effective` answer unlocks re-driving the
   * same id (and only when no receipt was ever handed out).
   */
  function beginVerification(record) {
    const flight = startFlight(record, 'verify');
    flight.executionId = record.executionId;
    resuming.add(record);
    guardFlight(flight, async () => {
      let answer;
      try {
        answer = await adapter.query({
          executionId: record.executionId,
          businessKey: record.businessKey,
          operation: record.parameters,
          retryDeadline: record.retryDeadline,
        });
      } catch (error) {
        answer = {
          status: 'unknown',
          error: {
            code: typeof error?.code === 'string' ? error.code : 'QUERY_THREW',
            message: typeof error?.message === 'string' ? error.message : 'adapter query threw',
          },
        };
      }
      if (answer?.status === 'effective') {
        settleEffective(flight, answer.effect);
      } else if (answer?.status === 'not_effective') {
        if (record.delivered) {
          // A receipt exists for this key; re-driving could apply the business
          // effect twice. Answer from the recorded receipt if it survived.
          const retained = record.priorReceiptEffect ?? record.effect;
          if (retained !== undefined) {
            record.outcome = EFFECTIVE;
            record.effect = retained;
            record.error = {
              code: 'RECEIPT_RETAINED',
              message: 'a receipt was already recorded for this key',
            };
            finishFlight(flight);
          } else {
            settleUnknown(flight, {
              code: 'RECEIPT_UNVERIFIABLE',
              message:
                'a receipt was handed out but the business no longer reports the effect; refusing to re-run',
            });
          }
        } else {
          record.safeToReuse = true;
          await driveExecute(flight, record.executionId);
        }
      } else {
        settleUnknown(
          flight,
          answer?.error ?? {
            code: 'EFFECT_UNKNOWN',
            message: 'business state is temporarily unknown; retry with the same execution id',
          },
        );
      }
    });
    flight.promise = flight.promise.finally(() => resuming.delete(record));
    return flight;
  }

  // ---------------------------------------------------------------- waiting

  function sleepUntil(limit, signal) {
    let timer;
    let onAbort;
    let settled = false;
    let settle;
    const promise = new Promise((resolve) => {
      settle = (value) => {
        if (settled) return;
        settled = true;
        timers.clearTimeout(timer);
        if (onAbort) signal?.removeEventListener('abort', onAbort);
        resolve(value);
      };
      const remaining = limit - now();
      if (!(remaining > 0)) {
        settled = true;
        resolve(false);
        return;
      }
      timer = timers.setTimeout(() => settle(false), remaining);
      onAbort = () => settle(false);
      signal?.addEventListener('abort', onAbort, { once: true });
    });
    return { promise, cancel: () => settle(false) };
  }

  /** Race a flight against the caller's limit, always releasing the timer. */
  async function raceFlight(flight, limit, signal) {
    const sleeper = sleepUntil(limit, signal);
    try {
      return await Promise.race([
        flight.promise.then(() => true),
        sleeper.promise.then(() => false),
      ]);
    } finally {
      sleeper.cancel();
    }
  }

  function receipt(record, join) {
    record.delivered = true;
    return ok({
      status: EFFECTIVE,
      effect: record.effect,
      executionId: record.executionId,
      retryDeadline: record.retryDeadline,
      join,
    });
  }

  function waitingView(record, join) {
    if (record.outcome === EFFECTIVE) return receipt(record, join);
    if (record.outcome === UNKNOWN) {
      return ok({
        status: UNKNOWN,
        executionId: record.executionId,
        retryDeadline: record.retryDeadline,
        error: record.error,
        join,
      });
    }
    return ok({
      status: PENDING,
      executionId: record.executionId,
      retryDeadline: record.retryDeadline,
      error: record.error,
      join,
    });
  }

  function callerLimit(deadline, record) {
    const fallback = record.retryDeadline;
    if (typeof deadline !== 'number' || !Number.isFinite(deadline)) return fallback;
    return Math.min(deadline, fallback);
  }

  // -------------------------------------------------------------- admission

  /**
   * Decide what a submit does with an existing record:
   * serve | join | verify | retry | conflict | replace.
   */
  function reuseDecision(record, identity) {
    const at = now();
    if (record.identity !== identity) {
      if (at < record.retryDeadline) {
        return {
          action: 'conflict',
          error: {
            code: 'PARAMETER_MISMATCH',
            message: 'this business key is already bound to different parameters inside its retry window',
            retryDeadline: record.retryDeadline,
            retryInMs: record.retryDeadline - at,
          },
        };
      }
      if (!mayEvict(record)) {
        return {
          action: 'conflict',
          error: {
            code: 'PENDING_CONFIRMATION',
            message:
              'the key expired but its previous effect is still unresolved; confirm it before rebinding the key',
            retryDeadline: record.retryDeadline,
            retryInMs: 0,
          },
        };
      }
      return { action: 'replace' };
    }
    if (record.outcome === EFFECTIVE) return { action: 'serve' };
    if (flightFor(record)) return { action: 'join' };
    if (record.outcome === UNKNOWN && record.executionId) return { action: 'verify' };
    if (record.executionId && !record.safeToReuse) return { action: 'verify' };
    return { action: 'retry' };
  }

  // ----------------------------------------------------------------- service

  const service = {
    /**
     * The single entry point. Inputs:
     *   businessKey  caller-chosen idempotency key
     *   params       business arguments (JSON-compatible)
     *   retryTtl     ms the key stays bound to these params after acceptance
     *   deadline     absolute instant when *this caller* stops waiting
     *
     * Concurrent calls with the same key and same params share one business
     * execution; each caller's wait ends independently at its own deadline.
     */
    async submit({ businessKey, params, retryTtl, deadline, signal } = {}) {
      try {
        validateParams(params);
      } catch (error) {
        return rejected('INVALID_PARAMS', { message: error.message });
      }
      if (typeof businessKey !== 'string' || businessKey.length === 0) {
        return rejected('INVALID_KEY', { message: 'businessKey must be a non-empty string' });
      }
      let expiry;
      try {
        expiry = retryDeadline(now(), retryTtl);
      } catch (error) {
        return rejected('INVALID_DEADLINE', { message: error.message });
      }
      if (typeof deadline !== 'number' || !Number.isFinite(deadline)) {
        return rejected('INVALID_DEADLINE', { message: 'deadline must be a finite absolute instant' });
      }

      const identity = fingerprint(params);
      let record = store.get(businessKey);

      if (record) {
        const decision = reuseDecision(record, identity);
        switch (decision.action) {
          case 'conflict':
            return rejected(decision.error.code, {
              ...decision.error,
              key: businessKey,
            });
          case 'replace':
            store.purge(record);
            record = null;
            break;
          case 'serve':
            return receipt(record, 'record');
          case 'join': {
            const flight = flightFor(record);
            return finishWait(record, callerLimit(deadline, record), signal, 'flight', flight);
          }
          case 'verify': {
            const flight = beginVerification(record);
            return finishWait(record, callerLimit(deadline, record), signal, 'verifying', flight);
          }
          case 'retry': {
            const flight = beginExecution(record);
            return finishWait(record, callerLimit(deadline, record), signal, 'started', flight);
          }
          default:
            throw new Error(`unexpected decision ${decision.action}`);
        }
      }

      const candidate = makeRecord({
        businessKey,
        parameters: params,
        acceptedAt: now(),
        retryDeadline: expiry,
      });
      const admission = admitNewRecord(candidate);
      if (!admission.admitted) {
        return rejected('CAPACITY', {
          message:
            'the idempotency window is full; no resident record is both past its retry window and safely reclaimable ' +
            '(records inside their retry window, still executing, or awaiting confirmation are never dropped)',
          capacity: store.capacity,
          resident: store.size,
        });
      }
      const flight = beginExecution(candidate);
      const view = await finishWait(
        candidate,
        callerLimit(deadline, candidate),
        signal,
        'started',
        flight,
      );
      return admission.evicted === undefined ? view : { ...view, evicted: admission.evicted };
    },

    /**
     * Re-confirm one key. Returns `unknown` ("still confirming") when the
     * business cannot answer yet; a later recover() retries the same id.
     */
    async recover({ businessKey } = {}) {
      const record = store.get(businessKey);
      if (!record) return null;
      return recoverRecord(record);
    },

    /** Re-confirm every resident record; the startup entry point. */
    async recoverAll() {
      const results = new Map();
      for (const record of store.values()) {
        results.set(record.businessKey, await recoverRecord(record));
      }
      return results;
    },

    /** Non-blocking state read, for polling callers. */
    status({ businessKey } = {}) {
      const record = store.get(businessKey);
      if (!record) return null;
      return {
        status: record.outcome,
        executionId: record.executionId,
        retryDeadline: record.retryDeadline,
        safeToReuse: record.safeToReuse,
        delivered: record.delivered,
        error: record.error,
      };
    },

    /** Durable state for a process restart. */
    snapshot() {
      return {
        capacity: store.capacity,
        records: store.values().map((record) => ({
          businessKey: record.businessKey,
          parameters: record.parameters,
          acceptedAt: record.acceptedAt,
          retryDeadline: record.retryDeadline,
          executionId: record.executionId,
          delivered: record.delivered,
          receiptEffect: record.outcome === EFFECTIVE ? record.effect : record.priorReceiptEffect,
        })),
      };
    },

    /** Diagnostic view; not part of the public RPC shape. */
    inspect(businessKey) {
      const record = store.get(businessKey);
      if (!record) return null;
      return {
        businessKey: record.businessKey,
        executionId: record.executionId,
        outcome: record.outcome,
        retryDeadline: record.retryDeadline,
        safeToReuse: record.safeToReuse,
        delivered: record.delivered,
        executing: Boolean(flightFor(record)),
      };
    },

    get size() {
      return store.size;
    },
  };

  async function finishWait(record, limit, signal, join, flight) {
    if (record.outcome === EFFECTIVE) return receipt(record, join);
    if (flight) await raceFlight(flight, limit, signal);
    return waitingView(record, join);
  }

  /**
   * Re-confirm one record. A record with no execution id was never dispatched,
   * so it may start fresh; one with an id is only ever queried and, following
   * an explicit `not_effective`, re-driven under that same id.
   */
  async function recoverRecord(record) {
    if (record.outcome === EFFECTIVE) {
      return { status: EFFECTIVE, executionId: record.executionId, checks: ['cached'] };
    }
    const existing = flightFor(record);
    if (existing) {
      await existing.promise;
      return { status: record.outcome, executionId: record.executionId, checks: ['joined-flight'] };
    }
    const checks = [];
    let flight;
    if (!record.executionId) {
      checks.push('no-execution-recorded');
      flight = beginExecution(record);
    } else {
      checks.push('query');
      flight = beginVerification(record);
    }
    await flight.promise;
    checks.push(record.outcome);
    return { status: record.outcome, executionId: record.executionId, checks };
  }

  // Restore runs before the service is handed to the caller.
  (function restore(from) {
    if (!from) return;
    if (!Array.isArray(from.records)) throw new TypeError('snapshot.records must be an array');
    if (from.capacity !== undefined && from.capacity !== capacity) {
      throw new RangeError('snapshot capacity does not match this server');
    }
    if (from.records.length > capacity) {
      throw new RangeError('snapshot holds more records than this server can track');
    }
    for (const saved of from.records) {
      if (typeof saved.businessKey !== 'string' || saved.businessKey.length === 0) {
        throw new TypeError('snapshot record is missing a business key');
      }
      validateParams(saved.parameters);
      const record = makeRecord({
        businessKey: saved.businessKey,
        parameters: saved.parameters,
        acceptedAt: saved.acceptedAt,
        retryDeadline: saved.retryDeadline,
        executionId: saved.executionId,
        // Everything restored is unconfirmed until recover() asks the business.
        outcome: saved.executionId ? UNKNOWN : PENDING,
        delivered: saved.delivered === true,
        priorReceiptEffect: saved.receiptEffect,
      });
      if (saved.executionId) {
        record.error = {
          code: 'EFFECT_UNKNOWN',
          message: 'restored record is unconfirmed; recover() must verify the execution id',
        };
      }
      store.insert(record);
    }
  })(snapshot);

  return service;
}
