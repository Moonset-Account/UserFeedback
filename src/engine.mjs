import { AdmissionError } from './errors.mjs';
import { canonicalJson, objectFingerprint } from './canonical.mjs';
import { evaluateCondition } from './condition.mjs';
import { normalizeInitialObject } from './program.mjs';

/**
 * Internal object state is a null-prototype object. Field values are finite
 * JSON values; absence is represented by the absence of an own property.
 */
export function cloneState(state) {
  const out = Object.create(null);
  for (const key of Object.keys(state)) {
    out[key] = structuredClone(state[key]);
  }
  return out;
}

/** Convert an internal state to a plain JSON object with sorted keys. */
export function stateToObject(state) {
  const keys = Object.keys(state).sort();
  return Object.fromEntries(
    keys.map((key) => [key, structuredClone(state[key])]),
  );
}

export function stateFingerprint(state) {
  return objectFingerprint(state);
}

function fieldSnapshot(state, field) {
  if (Object.hasOwn(state, field)) {
    return { present: true, value: structuredClone(state[field]) };
  }
  return { present: false };
}

/**
 * Apply one hook to a mutable state. Returns whether the condition matched and
 * whether the write actually modified the object. A write that stores the value
 * already present (or deletes an absent field) is a no-op; it does not count as
 * an effective change.
 */
export function applyHookToState(state, hook) {
  if (!evaluateCondition(hook.when, state)) {
    return { matched: false, action: null, changed: false };
  }
  const { kind, field } = hook.action;
  if (kind === 'set') {
    const alreadySame =
      Object.hasOwn(state, field) &&
      canonicalJson(state[field]) === canonicalJson(hook.action.value);
    if (alreadySame) {
      return { matched: true, action: 'set', field, changed: false };
    }
    state[field] = structuredClone(hook.action.value);
    return { matched: true, action: 'set', field, changed: true };
  }
  if (!Object.hasOwn(state, field)) {
    return { matched: true, action: 'delete', field, changed: false };
  }
  delete state[field];
  return { matched: true, action: 'delete', field, changed: true };
}

/** Validate that `order` is a permutation of all hook ids. */
export function validateOrder(program, order) {
  if (!Array.isArray(order)) {
    throw new AdmissionError(
      'INVALID_ORDER',
      'round order must be an array of hook ids',
    );
  }
  if (order.length !== program.hookIds.length) {
    throw new AdmissionError(
      'INVALID_ORDER',
      `round order must contain each of the ${program.hookIds.length} hooks exactly once`,
      { expected: program.hookIds, received: order },
    );
  }
  const seen = new Set();
  for (const id of order) {
    if (typeof id !== 'string' || !program.hooksById.has(id)) {
      throw new AdmissionError(
        'INVALID_ORDER',
        `round order contains unknown hook id ${JSON.stringify(id)}`,
        { id },
      );
    }
    if (seen.has(id)) {
      throw new AdmissionError(
        'INVALID_ORDER',
        `round order contains hook id ${JSON.stringify(id)} more than once`,
        { id },
      );
    }
    seen.add(id);
  }
  return true;
}

/**
 * Execute one round: every hook exactly once, in `order`, each hook reading the
 * object as left by the previous hook in the same round.
 */
export function advanceRound(program, startState, order, { trace = false } = {}) {
  validateOrder(program, order);
  const state = cloneState(startState);
  const traceEntries = trace ? [] : null;
  let effectiveWrites = 0;
  for (let index = 0; index < order.length; index += 1) {
    const hook = program.hooksById.get(order[index]);
    const before = trace ? fieldSnapshot(state, hook.action.field) : undefined;
    const outcome = applyHookToState(state, hook);
    if (outcome.changed) effectiveWrites += 1;
    if (trace) {
      traceEntries.push({
        hook: hook.id,
        matched: outcome.matched,
        action: outcome.action,
        field: hook.action.field,
        before,
        after: fieldSnapshot(state, hook.action.field),
        changed: outcome.changed,
        pendingBefore: order.slice(index),
        pendingAfter: order.slice(index + 1),
      });
    }
  }
  return {
    state,
    changed: effectiveWrites > 0,
    effectiveWrites,
    trace: traceEntries,
  };
}

/** Public single-round entry point. */
export function runRound(program, initialObject, order) {
  const state = normalizeInitialObject(program, initialObject);
  const result = advanceRound(program, state, order, { trace: true });
  return {
    object: stateToObject(result.state),
    changed: result.changed,
    effectiveWrites: result.effectiveWrites,
    trace: result.trace,
  };
}

/** Run an explicit sequence of rounds, each with its own order. */
export function runRounds(program, initialObject, orders) {
  if (!Array.isArray(orders)) {
    throw new AdmissionError(
      'INVALID_ORDER',
      'runRounds expects an array of round orders',
    );
  }
  let object = stateToObject(normalizeInitialObject(program, initialObject));
  const rounds = [];
  for (const order of orders) {
    const result = runRound(program, object, order);
    rounds.push(result);
    object = result.object;
  }
  return { rounds, object };
}

/* ------------------------------------------------------------------ *
 * Step-level execution (used by evidence replay and by the analyser)
 * ------------------------------------------------------------------ */

/** Full pending set of a fresh round, sorted for canonical form. */
export function fullPendingSet(program) {
  return [...program.hookIds].sort();
}

/**
 * A run cursor is `{ state, pending, changed }`:
 *   state    current object (null-prototype)
 *   pending  hooks of the current round that have not executed yet (sorted)
 *   changed  whether the current round has already performed an effective write
 *
 * Executing a hook removes it from `pending`. When `pending` becomes empty the
 * round closes: a round with no effective write terminates the run; otherwise a
 * new round starts with the full pending set and `changed = false`.
 */
export function createRunCursor(program, state) {
  return {
    state: cloneState(state),
    pending: fullPendingSet(program),
    changed: false,
  };
}

/**
 * Execute one hook step in place. Returns `{ terminated }` where `terminated`
 * means this step closed a round that performed no effective write.
 * Throws INVALID_STEP when the hook is not pending in the current round.
 */
export function executeStep(program, cursor, hookId) {
  if (!cursor.pending.includes(hookId)) {
    throw new AdmissionError(
      'INVALID_STEP',
      `hook ${JSON.stringify(hookId)} is not pending in the current round`,
      { hook: hookId, pending: cursor.pending },
    );
  }
  const hook = program.hooksById.get(hookId);
  const outcome = applyHookToState(cursor.state, hook);
  cursor.changed = cursor.changed || outcome.changed;
  cursor.pending = cursor.pending.filter((id) => id !== hookId);
  if (cursor.pending.length > 0) {
    return { terminated: false, roundCompleted: false, changed: outcome.changed };
  }
  const roundChanged = cursor.changed;
  if (!roundChanged) {
    return { terminated: true, roundCompleted: true, changed: false };
  }
  cursor.pending = fullPendingSet(program);
  cursor.changed = false;
  return { terminated: false, roundCompleted: true, changed: true };
}

/**
 * Split a raw step sequence into complete rounds plus an optional partial
 * round prefix (the hooks already executed in the current round).
 */
export function groupSteps(program, steps) {
  const full = fullPendingSet(program);
  if (full.length === 0) {
    if (steps.length > 0) {
      throw new AdmissionError(
        'INTERNAL_STEP_INVALID',
        'steps recorded for a program with no hooks',
        { steps },
      );
    }
    return { rounds: [], prefix: [] };
  }
  const rounds = [];
  let current = [];
  let pending = new Set(full);
  for (const id of steps) {
    if (!pending.has(id)) {
      throw new AdmissionError(
        'INTERNAL_STEP_INVALID',
        `hook ${JSON.stringify(id)} executed twice in one round`,
        { steps },
      );
    }
    current.push(id);
    pending.delete(id);
    if (pending.size === 0) {
      rounds.push(current);
      current = [];
      pending = new Set(full);
    }
  }
  return { rounds, prefix: current };
}
