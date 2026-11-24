import { AdmissionError } from './errors.mjs';
import { canonicalJson } from './canonical.mjs';
import { evaluateCondition } from './condition.mjs';
import {
  createRunCursor,
  executeStep,
  stateFingerprint,
  stateToObject,
} from './engine.mjs';
import {
  defineProgram,
  normalizeInitialObject,
  programSignature,
} from './program.mjs';

export const EVIDENCE_VERSION = 1;

/**
 * Evidence format
 * --------------
 * Evidence is a self-contained record replayed from the caller's initial object
 * by the independent `replayEvidence` entry point, without consulting the
 * analyser's graph or cache.
 *
 *   {
 *     version: 1,
 *     kind: 'termination' | 'nontermination',
 *     programSignature, programHookIds,
 *     initialObject,
 *     path: round[]          complete rounds, each with >= 1 effective write
 *   }
 *
 * termination adds:
 *   stableRound: round      the complete round with zero effective writes
 *   expected: { decision, object }
 *
 * nontermination adds:
 *   prefix: hookId[]        hooks of the current round already executed
 *   cycle:  hookId[]        further steps that close back onto the start state
 *   expected: {
 *     startObject, endObject,     // structurally equal objects
 *     startPending, endPending,   // equal within-round pending sets
 *     startChanged, endChanged    // equal within-round change flags
 *   }
 *
 * The pending set and change flag are part of the cycle claim: closing the
 * object alone is not a legal repetition, because the set of hooks still to be
 * executed in the round would differ.
 */

function failure(code, message, details = {}) {
  return { code, message, details };
}

function sameStringSet(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) {
    return false;
  }
  const left = [...a].sort();
  const right = [...b].sort();
  return left.every((value, index) => value === right[index]);
}

function isStringArray(value) {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function checkProgramMatch(program, evidence, failures) {
  if (evidence.programSignature !== programSignature(program)) {
    failures.push(
      failure('PROGRAM_MISMATCH', 'evidence was produced for a different program'),
    );
  }
  if (!sameStringSet(evidence.programHookIds, program.hookIds)) {
    failures.push(
      failure(
        'PROGRAM_HOOKS_MISMATCH',
        'evidence hook ids differ from the program',
      ),
    );
  }
}

function abandon(kind, failures, trace) {
  return { ok: false, kind, failures, trace, reproduced: null };
}

/**
 * Execute one complete round from the evidence. Returns
 * `{ ok, terminated, wrote }` or null after pushing a failure.
 */
function replayRound(program, cursor, round, label, failures, trace) {
  const hookCount = program.hookIds.length;
  if (!isStringArray(round)) {
    failures.push(
      failure('INVALID_EVIDENCE', `${label} must be an array of hook ids`, { label }),
    );
    return null;
  }
  if (hookCount === 0) {
    if (round.length !== 0) {
      failures.push(
        failure('INVALID_ROUND_SIZE', `${label} must be empty: no hooks`, { label }),
      );
      return null;
    }
    return { ok: true, terminated: true, wrote: false };
  }
  if (round.length !== hookCount) {
    failures.push(
      failure(
        'INVALID_ROUND_SIZE',
        `${label} has ${round.length} hooks, expected ${hookCount}`,
        { label, round },
      ),
    );
    return null;
  }
  const from = stateFingerprint(cursor.state);
  let terminated = false;
  let wrote = false;
  for (const hookId of round) {
    if (!cursor.pending.includes(hookId)) {
      failures.push(
        failure(
          'INVALID_STEP',
          `${label}: hook ${JSON.stringify(hookId)} is not pending in this round`,
          { label, hook: hookId, pending: [...cursor.pending] },
        ),
      );
      return null;
    }
    const step = executeStep(program, cursor, hookId);
    if (step.changed) wrote = true;
    if (step.terminated) {
      terminated = true;
      break;
    }
  }
  trace.push({
    section: label,
    order: [...round],
    from,
    to: stateFingerprint(cursor.state),
    terminated,
    wrote,
  });
  if (!terminated && cursor.pending.length !== hookCount) {
    failures.push(
      failure(
        'INVALID_ROUND_SIZE',
        `${label} did not execute every hook exactly once`,
        { label, pending: [...cursor.pending] },
      ),
    );
    return null;
  }
  return { ok: true, terminated, wrote };
}

/**
 * Replay evidence from scratch and return an independent verdict.
 *
 * `ok` is true only when every claim the evidence makes is reproduced by actual
 * execution of the hook interpreter.
 */
export function replayEvidence(program, evidence) {
  if (
    evidence === null ||
    typeof evidence !== 'object' ||
    Array.isArray(evidence)
  ) {
    throw new AdmissionError('INVALID_EVIDENCE', 'evidence must be an object');
  }
  if (evidence.version !== EVIDENCE_VERSION) {
    throw new AdmissionError(
      'INVALID_EVIDENCE',
      `unsupported evidence version ${JSON.stringify(evidence.version)}`,
      { version: evidence.version },
    );
  }
  if (evidence.kind !== 'termination' && evidence.kind !== 'nontermination') {
    throw new AdmissionError(
      'INVALID_EVIDENCE',
      `unsupported evidence kind ${JSON.stringify(evidence.kind)}`,
      { kind: evidence.kind },
    );
  }
  const failures = [];
  const trace = [];
  checkProgramMatch(program, evidence, failures);

  if (!Array.isArray(evidence.path)) {
    failures.push(
      failure('INVALID_EVIDENCE', 'evidence.path must be an array of rounds'),
    );
    return abandon(evidence.kind, failures, trace);
  }

  let initial;
  try {
    initial = normalizeInitialObject(program, evidence.initialObject);
  } catch (error) {
    failures.push(
      failure('INITIAL_OBJECT_INVALID', error.message, {
        code: error.code ?? null,
      }),
    );
    return abandon(evidence.kind, failures, trace);
  }
  const cursor = createRunCursor(program, initial);

  for (let index = 0; index < evidence.path.length; index += 1) {
    const result = replayRound(
      program,
      cursor,
      evidence.path[index],
      `path[${index}]`,
      failures,
      trace,
    );
    if (result === null) return abandon(evidence.kind, failures, trace);
    if (result.terminated) {
      failures.push(
        failure(
          'PATH_ROUND_TERMINATED',
          `path[${index}] performed no effective write and would have ended the run`,
          { index },
        ),
      );
      return abandon(evidence.kind, failures, trace);
    }
    if (!result.wrote) {
      failures.push(
        failure(
          'PATH_ROUND_NO_WRITE',
          `path[${index}] performed no effective write`,
          { index },
        ),
      );
      return abandon(evidence.kind, failures, trace);
    }
  }

  if (evidence.kind === 'termination') {
    return replayTermination(program, cursor, evidence, failures, trace);
  }
  return replayCycle(program, cursor, evidence, failures, trace);
}

function replayTermination(program, cursor, evidence, failures, trace) {
  const result = replayRound(
    program,
    cursor,
    evidence.stableRound,
    'stableRound',
    failures,
    trace,
  );
  if (result === null) return abandon('termination', failures, trace);
  if (!result.terminated) {
    failures.push(
      failure(
        'STABLE_ROUND_CHANGED',
        'the round claimed as stable performs at least one effective write',
      ),
    );
  }
  const object = stateToObject(cursor.state);
  const decision = evaluateCondition(program.accept, cursor.state)
    ? 'accepted'
    : 'rejected';
  const expected = evidence.expected ?? {};
  // Declared expectations are optional claims; when present they must match
  // what actual execution produced. The stable-round check above is mandatory.
  if (Object.hasOwn(expected, 'object') &&
      canonicalJson(expected.object) !== canonicalJson(object)) {
    failures.push(
      failure('OBJECT_MISMATCH', 'replayed final object differs from evidence', {
        expected: canonicalJson(expected.object),
        actual: canonicalJson(object),
      }),
    );
  }
  if (Object.hasOwn(expected, 'decision') && expected.decision !== decision) {
    failures.push(
      failure('DECISION_MISMATCH', 'replayed decision differs from evidence', {
        expected: expected.decision,
        actual: decision,
      }),
    );
  }
  return {
    ok: failures.length === 0,
    kind: 'termination',
    failures,
    trace,
    reproduced: { decision, object },
  };
}

function replayCycle(program, cursor, evidence, failures, trace) {
  if (!isStringArray(evidence.prefix)) {
    failures.push(
      failure('INVALID_EVIDENCE', 'nontermination evidence requires a prefix array'),
    );
    return abandon('nontermination', failures, trace);
  }
  if (!isStringArray(evidence.cycle) || evidence.cycle.length === 0) {
    failures.push(
      failure(
        'INVALID_EVIDENCE',
        'nontermination evidence requires a non-empty cycle array',
      ),
    );
    return abandon('nontermination', failures, trace);
  }
  for (const hookId of evidence.prefix) {
    if (!cursor.pending.includes(hookId)) {
      failures.push(
        failure(
          'INVALID_STEP',
          `prefix: hook ${JSON.stringify(hookId)} is not pending`,
          { pending: [...cursor.pending] },
        ),
      );
      return abandon('nontermination', failures, trace);
    }
    const step = executeStep(program, cursor, hookId);
    if (step.terminated) {
      failures.push(
        failure(
          'PREFIX_TERMINATED',
          'the prefix closed a round with no effective write',
          { hook: hookId },
        ),
      );
      return abandon('nontermination', failures, trace);
    }
  }

  const startObject = stateToObject(cursor.state);
  const startFingerprint = stateFingerprint(cursor.state);
  const startPending = [...cursor.pending].sort();
  const startChanged = cursor.changed;
  trace.push({
    section: 'cycleStart',
    object: startObject,
    pending: startPending,
    changed: startChanged,
  });

  let terminatedAt = null;
  for (let index = 0; index < evidence.cycle.length; index += 1) {
    const hookId = evidence.cycle[index];
    if (!cursor.pending.includes(hookId)) {
      failures.push(
        failure(
          'INVALID_STEP',
          `cycle[${index}]: hook ${JSON.stringify(hookId)} is not pending`,
          { index, pending: [...cursor.pending] },
        ),
      );
      return abandon('nontermination', failures, trace);
    }
    const step = executeStep(program, cursor, hookId);
    if (step.terminated) {
      terminatedAt = index;
      break;
    }
  }
  if (terminatedAt !== null) {
    failures.push(
      failure(
        'CYCLE_TERMINATED',
        'the cycle reaches an unchanged round, so it terminates',
        { index: terminatedAt },
      ),
    );
    return abandon('nontermination', failures, trace);
  }

  const endObject = stateToObject(cursor.state);
  const endFingerprint = stateFingerprint(cursor.state);
  const endPending = [...cursor.pending].sort();
  const endChanged = cursor.changed;
  trace.push({
    section: 'cycleEnd',
    object: endObject,
    pending: endPending,
    changed: endChanged,
  });

  if (startFingerprint !== endFingerprint) {
    failures.push(
      failure('CYCLE_OBJECT_NOT_CLOSED', 'cycle does not return to the same object', {
        start: startFingerprint,
        end: endFingerprint,
      }),
    );
  }
  if (!sameStringSet(startPending, endPending)) {
    failures.push(
      failure(
        'CYCLE_PENDING_NOT_CLOSED',
        'cycle does not return to the same within-round pending hook set',
        { start: startPending, end: endPending },
      ),
    );
  }
  if (startChanged !== endChanged) {
    failures.push(
      failure(
        'CYCLE_CHANGED_FLAG_NOT_CLOSED',
        'cycle does not return to the same within-round change flag',
        { start: startChanged, end: endChanged },
      ),
    );
  }

  const expected = evidence.expected ?? {};
  // `expected` fields are optional claims; when supplied they must agree with
  // what replay actually produced. The closure checks above are mandatory.
  if (
    Object.hasOwn(expected, 'startObject') &&
    canonicalJson(expected.startObject) !== canonicalJson(startObject)
  ) {
    failures.push(
      failure('START_OBJECT_MISMATCH', 'cycle start object differs from evidence', {
        expected: canonicalJson(expected.startObject),
        actual: canonicalJson(startObject),
      }),
    );
  }
  if (
    Object.hasOwn(expected, 'endObject') &&
    canonicalJson(expected.endObject) !== canonicalJson(endObject)
  ) {
    failures.push(
      failure('END_OBJECT_MISMATCH', 'cycle end object differs from evidence', {
        expected: canonicalJson(expected.endObject),
        actual: canonicalJson(endObject),
      }),
    );
  }
  if (
    Object.hasOwn(expected, 'startPending') &&
    !sameStringSet(expected.startPending, startPending)
  ) {
    failures.push(
      failure('START_PENDING_MISMATCH', 'cycle start pending set differs from evidence', {
        expected: expected.startPending,
        actual: startPending,
      }),
    );
  }
  if (
    Object.hasOwn(expected, 'endPending') &&
    !sameStringSet(expected.endPending, endPending)
  ) {
    failures.push(
      failure('END_PENDING_MISMATCH', 'cycle end pending set differs from evidence', {
        expected: expected.endPending,
        actual: endPending,
      }),
    );
  }
  if (Object.hasOwn(expected, 'startChanged') && expected.startChanged !== startChanged) {
    failures.push(
      failure('START_CHANGED_MISMATCH', 'cycle start change flag differs from evidence', {
        expected: expected.startChanged,
        actual: startChanged,
      }),
    );
  }
  if (Object.hasOwn(expected, 'endChanged') && expected.endChanged !== endChanged) {
    failures.push(
      failure('END_CHANGED_MISMATCH', 'cycle end change flag differs from evidence', {
        expected: expected.endChanged,
        actual: endChanged,
      }),
    );
  }

  return {
    ok: failures.length === 0,
    kind: 'nontermination',
    failures,
    trace,
    reproduced: {
      startObject,
      endObject,
      startPending,
      endPending,
      startChanged,
      endChanged,
      closes:
        startFingerprint === endFingerprint &&
        sameStringSet(startPending, endPending) &&
        startChanged === endChanged,
    },
  };
}

/**
 * Independent verification entry point: re-derive the program from a raw spec
 * and replay the evidence. Any disagreement between the analyser and this path
 * surfaces as replay failures or a refused spec.
 */
export function verifyEvidence(spec, evidence) {
  const program = defineProgram(spec);
  return replayEvidence(program, evidence);
}

/** Alias: replaying a witness is replaying evidence. */
export const replayWitness = replayEvidence;
