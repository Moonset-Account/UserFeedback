import { createHash } from 'node:crypto';
import { CoordinatorError } from './errors.js';
import {
  compareScoreEntries,
  retentionCount,
  aggregateFolds,
  ratioString,
  normalizeFoldResults,
  normalizeCheckpoint,
  deepFreeze,
  cloneJson,
  isNonEmptyString,
} from './util.js';

const DEFAULT_STAGES = Object.freeze([20, 60, 180]);
const RULE_VERSION = 1;

function assertStringArray(value, label) {
  if (!Array.isArray(value)) {
    throw new CoordinatorError(
      'INVALID_ROSTER',
      `${label} must be an array of non-empty strings`,
      { field: label },
    );
  }
  const seen = new Set();
  for (const entry of value) {
    if (!isNonEmptyString(entry)) {
      throw new CoordinatorError(
        'INVALID_ROSTER',
        `${label} must contain only non-empty strings`,
        { field: label, value: entry },
      );
    }
    if (seen.has(entry)) {
      throw new CoordinatorError(
        'INVALID_ROSTER',
        `${label} contains duplicate entry "${entry}"`,
        { field: label, value: entry },
      );
    }
    seen.add(entry);
  }
  if (value.length === 0) {
    throw new CoordinatorError('INVALID_ROSTER', `${label} must not be empty`, {
      field: label,
    });
  }
  return [...value];
}

/**
 * Accept either ["fold-a", "fold-b"] or
 * [{ name: "fold-a", samples: 1000 }, ...].
 * Returns [{ name, declaredSamples: number | null }].
 */
function normalizeFoldDeclaration(folds) {
  if (!Array.isArray(folds) || folds.length === 0) {
    throw new CoordinatorError(
      'INVALID_ROSTER',
      'folds must be a non-empty array of names or { name, samples } objects',
    );
  }
  const out = [];
  const seen = new Set();
  for (const raw of folds) {
    let name;
    let declaredSamples = null;
    if (typeof raw === 'string') {
      name = raw;
    } else if (raw !== null && typeof raw === 'object' && !Array.isArray(raw)) {
      name = raw.name;
      if (raw.samples !== undefined) {
        if (!Number.isSafeInteger(raw.samples) || raw.samples <= 0) {
          throw new CoordinatorError(
            'INVALID_ROSTER',
            `fold "${String(name)}" declares an invalid sample count`,
            { fold: name, samples: raw.samples },
          );
        }
        declaredSamples = raw.samples;
      }
    } else {
      throw new CoordinatorError(
        'INVALID_ROSTER',
        'each fold must be a string or a { name, samples } object',
      );
    }
    if (!isNonEmptyString(name)) {
      throw new CoordinatorError('INVALID_ROSTER', 'every fold needs a non-empty name');
    }
    if (seen.has(name)) {
      throw new CoordinatorError(
        'INVALID_ROSTER',
        `fold "${name}" is declared more than once`,
        { fold: name },
      );
    }
    seen.add(name);
    out.push({ name, declaredSamples });
  }
  return out;
}

function validateStages(stages) {
  if (!Array.isArray(stages) || stages.length === 0) {
    throw new CoordinatorError(
      'INVALID_STAGES',
      'stages must be a non-empty array of positive step counts',
    );
  }
  let previous = 0;
  for (const stage of stages) {
    if (!Number.isSafeInteger(stage) || stage <= 0) {
      throw new CoordinatorError(
        'INVALID_STAGES',
        'every stage must be a positive safe integer',
        { stage },
      );
    }
    if (stage <= previous) {
      throw new CoordinatorError(
        'INVALID_STAGES',
        'stages must be strictly increasing',
        { stage, previous },
      );
    }
    previous = stage;
  }
  return [...stages];
}

function canonicalJson(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  const keys = Object.keys(value).sort();
  return `{${keys
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
    .join(',')}}`;
}

/**
 * SHA-256 over the settlement body with keys sorted, so two independent
 * implementations of the same rule produce the same digest.
 *
 * @param {object} settlement
 * @returns {string} lowercase hex digest
 */
export function computeSettlementDigest(settlement) {
  const { settlementDigest: _ignored, ...body } = settlement;
  return createHash('sha256').update(canonicalJson(body)).digest('hex');
}

/**
 * Recompute a settlement digest independently of how it was produced.
 *
 * @param {object} settlement
 * @returns {boolean}
 */
export function verifySettlementDigest(settlement) {
  if (settlement === null || typeof settlement !== 'object') return false;
  if (typeof settlement.settlementDigest !== 'string') return false;
  return computeSettlementDigest(settlement) === settlement.settlementDigest;
}

function sameMembers(a, b) {
  if (a.length !== b.length) return false;
  const set = new Set(a);
  return b.every((entry) => set.has(entry));
}

/**
 * Step tournament coordinator.
 *
 * Lifecycle:
 *   openRound -> submitAttempt* / reopenExperiment* -> settleRound -> openRound ...
 *
 * Behavioural contract:
 *  - Each round freezes its experiment and validation-fold roster before any
 *    submission is accepted.
 *  - Score = sum(correct) / sum(samples) over the frozen folds (sample
 *    weighted, never a macro average of fold accuracies).
 *  - Tie-break: experiment id ascending, numeric-aware ("2" < "10").
 *  - Retention: max(1, floor(eligibleCount / 2)).
 *  - Reopening an unsettled experiment disqualifies the live attempt's folds
 *    and checkpoint permanently; a late submission can never resurrect it.
 *  - Results arriving after settlement are rejected and audited.
 *  - Continuation commands reference the promoted attempt's checkpoint and
 *    state only the additional step count.
 */
export class Tournament {
  #stages;
  #foldsEstablished = new Map();
  #rounds = [];
  #audit = [];
  #seq = 0;

  /**
   * @param {{ stages?: number[] }} [options]
   */
  constructor({ stages = DEFAULT_STAGES } = {}) {
    this.#stages = Object.freeze(validateStages(stages));
  }

  get stages() {
    return Object.freeze([...this.#stages]);
  }

  /** @returns {'idle' | 'open' | 'settled' | 'complete'} */
  get phase() {
    if (this.#rounds.length === 0) return 'idle';
    const last = this.#rounds[this.#rounds.length - 1];
    if (last.status === 'open') return 'open';
    return last.settlement.continuation === null ? 'complete' : 'settled';
  }

  get currentRound() {
    return this.#rounds.length === 0 ? null : this.#rounds[this.#rounds.length - 1].round;
  }

  get settlements() {
    return deepFreeze(
      this.#rounds
        .filter((round) => round.settlement !== null)
        .map((round) => cloneJson(round.settlement)),
    );
  }

  get lastSettlement() {
    const last = this.#rounds[this.#rounds.length - 1];
    return last?.settlement ?? null;
  }

  /** Continuation commands produced by the last settlement, or null. */
  get continuationCommands() {
    return this.lastSettlement?.continuation?.commands ?? null;
  }

  get auditLog() {
    return deepFreeze(cloneJson(this.#audit));
  }

  #lastRound() {
    return this.#rounds[this.#rounds.length - 1] ?? null;
  }

  #requireOpenRound() {
    const round = this.#lastRound();
    if (!round) {
      throw new CoordinatorError('NO_ROSTER', 'no round is open; call openRound() first');
    }
    if (round.status !== 'open') {
      throw new CoordinatorError('ROUND_CLOSED', `round ${round.round} is already settled`, {
        round: round.round,
      });
    }
    return round;
  }

  #log(type, fields = {}) {
    this.#seq += 1;
    this.#audit.push(deepFreeze({ seq: this.#seq, type, ...cloneJson(fields) }));
  }

  /**
   * Resolve each declared fold against sizes already fixed by earlier rounds.
   * Pure: nothing is recorded until the whole declaration validates, so a
   * rejected openRound() cannot leave half-registered fold sizes behind.
   */
  #resolveFolds(declaration) {
    const resolved = declaration.map(({ name, declaredSamples }) => {
      const known = this.#foldsEstablished.get(name);
      if (declaredSamples !== null && known !== undefined && declaredSamples !== known) {
        throw new CoordinatorError(
          'FOLD_SAMPLE_CONFLICT',
          `fold "${name}" was previously fixed at ${known} samples but this round declares ${declaredSamples}`,
          { fold: name, known, declared: declaredSamples },
        );
      }
      return { name, samples: declaredSamples ?? known ?? null };
    });

    for (const fold of resolved) {
      if (fold.samples !== null) this.#foldsEstablished.set(fold.name, fold.samples);
    }
    return resolved;
  }

  #freezeRoster(round) {
    round.roster = deepFreeze({
      round: round.round,
      trainedTo: round.trainedTo,
      folds: round.folds.map((fold) => ({ ...fold })),
      experimentIds: [...round.experimentIds],
    });
    return round.roster;
  }

  /**
   * Freeze the roster for the next round.
   *
   * Round 1: the caller supplies the initial experiment list. Later rounds:
   * the roster is exactly the previous round's promotion list (and is
   * reordered to promotion order).
   *
   * @param {{ experiments?: string[], folds: (string|{name: string, samples?: number})[] }} input
   */
  openRound({ experiments, folds } = {}) {
    const last = this.#lastRound();
    if (last && last.status === 'open') {
      throw new CoordinatorError(
        'ROUND_STILL_OPEN',
        `round ${last.round} is still open; settle it before opening another`,
        { round: last.round },
      );
    }

    // A continuation round freezes a fresh copy of the fold roster. If the
    // caller does not restate it, the previous round's verified fold list is
    // carried forward -- never silently dropped.
    let foldInput = folds;
    if (foldInput === undefined && last) {
      foldInput = last.folds.map((fold) =>
        fold.samples === null ? fold.name : { name: fold.name, samples: fold.samples },
      );
    }

    let roundNumber;
    let experimentIds;
    let bases = new Map();

    if (!last) {
      roundNumber = 1;
      experimentIds = assertStringArray(experiments, 'experiments');
    } else {
      if (last.settlement.continuation === null) {
        throw new CoordinatorError(
          'TOURNAMENT_COMPLETE',
          `round ${last.round} was the final stage at step ${last.trainedTo}`,
          { round: last.round, trainedTo: last.trainedTo },
        );
      }
      roundNumber = last.round + 1;
      const promotedIds = last.settlement.continuation.commands.map(
        (command) => command.experimentId,
      );

      if (experiments === undefined) {
        experimentIds = [...promotedIds];
      } else {
        const supplied = assertStringArray(experiments, 'experiments');
        if (!sameMembers(supplied, promotedIds)) {
          throw new CoordinatorError(
            'ROSTER_MISMATCH',
            'a continuation round may only roster the experiments promoted by the previous round',
            { expected: [...promotedIds], received: supplied },
          );
        }
        experimentIds = [...promotedIds];
      }

      bases = new Map(
        last.settlement.continuation.commands.map((command) => [
          command.experimentId,
          deepFreeze({
            round: last.round,
            attemptId: command.attemptId,
            checkpoint: { ...command.checkpoint },
            additionalSteps: command.additionalSteps,
          }),
        ]),
      );
    }

    if (roundNumber > this.#stages.length) {
      throw new CoordinatorError(
        'TOURNAMENT_COMPLETE',
        `no stage is defined for round ${roundNumber}`,
      );
    }

    const trainedTo = this.#stages[roundNumber - 1];

    // Validate every continuation base against the new stage *before* any
    // fold sizes are recorded, so a failed openRound() leaves no residue.
    for (const [experimentId, base] of bases) {
      const reached = base.checkpoint.step + base.additionalSteps;
      if (reached !== trainedTo) {
        throw new CoordinatorError(
          'STAGE_MISMATCH',
          `continuation for "${experimentId}" reaches step ${reached}, but stage ${roundNumber} trains to ${trainedTo}`,
          { experimentId, trainedTo, reached },
        );
      }
    }

    const declaration = normalizeFoldDeclaration(foldInput);
    const roundNumberFolds = this.#resolveFolds(declaration);

    const round = {
      round: roundNumber,
      trainedTo,
      folds: roundNumberFolds.map((fold) => ({ ...fold })),
      experimentIds: [...experimentIds],
      bases,
      attempts: new Map(),
      attemptLog: [],
      attemptCounter: 0,
      status: 'open',
      settlement: null,
      roster: null,
    };
    this.#freezeRoster(round);
    this.#rounds.push(round);

    this.#log('round_opened', {
      round: roundNumber,
      trainedTo,
      experimentIds: [...experimentIds],
      folds: round.folds.map((fold) => ({ ...fold })),
    });

    return deepFreeze(cloneJson(round.roster));
  }

  #validateContinuationBase(round, experimentId, continuedFrom) {
    const base = round.bases.get(experimentId);
    if (!base) {
      if (continuedFrom !== undefined && continuedFrom !== null) {
        throw new CoordinatorError(
          'CONTINUATION_BASE_MISMATCH',
          'round 1 attempts must not declare continuedFrom',
          { experimentId },
        );
      }
      return null;
    }

    if (
      continuedFrom === null ||
      typeof continuedFrom !== 'object' ||
      Array.isArray(continuedFrom)
    ) {
      throw new CoordinatorError(
        'MISSING_CONTINUATION_BASE',
        `round ${round.round} attempts must declare continuedFrom with the promoted checkpoint`,
        { experimentId, expected: base.checkpoint },
      );
    }

    const mismatches = [];
    if (continuedFrom.attemptId !== base.attemptId) {
      mismatches.push(['attemptId', base.attemptId, continuedFrom.attemptId]);
    }
    if (continuedFrom.path !== base.checkpoint.path) {
      mismatches.push(['path', base.checkpoint.path, continuedFrom.path]);
    }
    if (continuedFrom.step !== base.checkpoint.step) {
      mismatches.push(['step', base.checkpoint.step, continuedFrom.step]);
    }
    if (
      base.checkpoint.digest !== undefined &&
      continuedFrom.digest !== base.checkpoint.digest
    ) {
      mismatches.push(['digest', base.checkpoint.digest, continuedFrom.digest]);
    }
    if (mismatches.length > 0) {
      throw new CoordinatorError(
        'CONTINUATION_BASE_MISMATCH',
        `continuedFrom does not reference the promoted attempt: ${mismatches
          .map(([field]) => field)
          .join(', ')}`,
        { experimentId, mismatches },
      );
    }

    const normalized = {
      attemptId: base.attemptId,
      path: base.checkpoint.path,
      step: base.checkpoint.step,
    };
    if (base.checkpoint.digest !== undefined) normalized.digest = base.checkpoint.digest;
    return normalized;
  }

  /**
   * Auto-assigned attempt ids are unique across the whole tournament
   * (r1a1, r1a2, r2a1, ...) so a continuation reference from an earlier round
   * can never be confused with a later round's attempt.
   */
  #newAttemptId(round) {
    round.attemptCounter += 1;
    return `r${round.round}a${round.attemptCounter}`;
  }

  #assertAttemptIdAvailable(round, experimentId, attemptId, placeholder) {
    const clash = round.attemptLog.find(
      (attempt) => attempt !== placeholder && attempt.attemptId === attemptId,
    );
    if (clash) {
      throw new CoordinatorError(
        'DUPLICATE_ATTEMPT_ID',
        `attempt id "${attemptId}" was already used in round ${round.round}`,
        { experimentId, attemptId, usedBy: clash.experimentId },
      );
    }
  }

  /**
   * Submit the final result for one experiment in the open round.
   *
   * A submitted attempt is final; retrying requires reopenExperiment(), which
   * disqualifies the current attempt before the replacement is accepted.
   *
   * @param {{
   *   experimentId: string,
   *   attemptId?: string,
   *   folds: {name: string, correct: number, samples: number}[],
   *   checkpoint: {path: string, step: number, digest?: string},
   *   continuedFrom?: {attemptId: string, path: string, step: number, digest?: string}
   * }} input
   */
  submitAttempt(input) {
    const round = this.#lastRound();
    try {
      if (!round) {
        throw new CoordinatorError('NO_ROSTER', 'no round is open; call openRound() first');
      }
      if (round.status !== 'open') {
        throw new CoordinatorError(
          'ROUND_CLOSED',
          `round ${round.round} is settled; late results cannot participate`,
          { round: round.round },
        );
      }

      const { experimentId } = input ?? {};
      if (!isNonEmptyString(experimentId) || !round.experimentIds.includes(experimentId)) {
        throw new CoordinatorError(
          'UNKNOWN_EXPERIMENT',
          `experiment ${JSON.stringify(experimentId)} is not in the frozen roster for round ${round.round}`,
          { round: round.round, expected: [...round.experimentIds] },
        );
      }

      const active = round.attempts.get(experimentId);
      if (active && active.status === 'submitted') {
        throw new CoordinatorError(
          'ATTEMPT_ALREADY_ACTIVE',
          `experiment "${experimentId}" already has attempt "${active.attemptId}"; reopen it before submitting again`,
          { experimentId, attemptId: active.attemptId },
        );
      }
      const placeholder = active && active.status === 'open' ? active : null;

      let attemptId = input.attemptId;
      if (attemptId === undefined) {
        attemptId = placeholder ? placeholder.attemptId : this.#newAttemptId(round);
      } else if (!isNonEmptyString(attemptId)) {
        throw new CoordinatorError('INVALID_ATTEMPT_ID', 'attemptId must be a non-empty string');
      }
      if (placeholder && attemptId !== placeholder.attemptId) {
        throw new CoordinatorError(
          'ATTEMPT_ID_MISMATCH',
          `the live placeholder for "${experimentId}" is "${placeholder.attemptId}"; submit under that id`,
          { experimentId, expected: placeholder.attemptId, received: attemptId },
        );
      }
      this.#assertAttemptIdAvailable(round, experimentId, attemptId, placeholder);

      const checkpoint = normalizeCheckpoint(input.checkpoint, round.trainedTo);

      // A checkpoint is identified by its content digest. Once an attempt is
      // superseded its checkpoint is disqualified for good, so the same bytes
      // may not reappear under a new attempt id or a new path.
      const stale = round.attemptLog.find(
        (attempt) =>
          attempt !== placeholder &&
          attempt.experimentId === experimentId &&
          attempt.status === 'superseded' &&
          attempt.checkpoint !== null &&
          attempt.checkpoint.digest === checkpoint.digest,
      );
      if (stale) {
        throw new CoordinatorError(
          'SUPERSEDED_CHECKPOINT',
          `checkpoint digest belongs to superseded attempt "${stale.attemptId}" and cannot be resubmitted`,
          { experimentId, attemptId: stale.attemptId, digest: checkpoint.digest },
        );
      }

      const continuedFrom = this.#validateContinuationBase(
        round,
        experimentId,
        input.continuedFrom,
      );

      const expectedSamples = new Map(round.folds.map((fold) => [fold.name, fold.samples]));
      const foldResults = normalizeFoldResults(
        input.folds,
        round.folds.map((fold) => fold.name),
        expectedSamples,
      );
      const aggregate = aggregateFolds(foldResults);

      // A fold whose size was not declared up front is fixed by the first
      // accepted submission of the round, then enforced for everyone else.
      // Nothing is recorded until the submission is fully validated.
      let foldsResolved = false;
      for (const fold of round.folds) {
        if (fold.samples !== null) continue;
        const observed = foldResults.find((entry) => entry.name === fold.name).samples;
        fold.samples = observed;
        this.#foldsEstablished.set(fold.name, observed);
        foldsResolved = true;
      }
      if (foldsResolved) this.#freezeRoster(round);

      const record = {
        experimentId,
        attemptId,
        status: 'submitted',
        folds: foldResults,
        correct: aggregate.correct,
        samples: aggregate.samples,
        score: aggregate.score,
        scoreRatio: aggregate.scoreRatio,
        checkpoint: deepFreeze({ ...checkpoint }),
        continuedFrom: continuedFrom ? deepFreeze(continuedFrom) : null,
        supersededBy: null,
        supersededReason: null,
      };

      if (placeholder) {
        Object.assign(placeholder, record);
      } else {
        round.attempts.set(experimentId, record);
        round.attemptLog.push(record);
      }

      this.#log('attempt_submitted', {
        round: round.round,
        experimentId,
        attemptId,
        correct: aggregate.correct,
        samples: aggregate.samples,
        scoreRatio: aggregate.scoreRatio,
        checkpoint: { ...checkpoint },
      });

      return deepFreeze({
        round: round.round,
        trainedTo: round.trainedTo,
        experimentId,
        attemptId,
        correct: aggregate.correct,
        samples: aggregate.samples,
        score: aggregate.score,
        scoreRatio: aggregate.scoreRatio,
        folds: foldResults.map((entry) => ({ ...entry })),
        checkpoint: { ...checkpoint },
        continuedFrom: record.continuedFrom ? { ...record.continuedFrom } : null,
      });
    } catch (error) {
      if (error instanceof CoordinatorError) {
        this.#log('submission_rejected', {
          round: round ? round.round : null,
          experimentId: isNonEmptyString(input?.experimentId) ? input.experimentId : null,
          attemptId: isNonEmptyString(input?.attemptId) ? input.attemptId : null,
          code: error.code,
        });
      }
      throw error;
    }
  }

  /**
   * Restart an experiment whose round has not been settled yet.
   *
   * The live attempt's fold results and checkpoint are disqualified at once.
   * The experiment must then submit a fresh attempt; until it does it is
   * excluded from settlement.
   *
   * @param {{ experimentId: string, reason?: string, attemptId?: string }} input
   */
  reopenExperiment({ experimentId, reason, attemptId } = {}) {
    const round = this.#requireOpenRound();
    if (!isNonEmptyString(experimentId) || !round.experimentIds.includes(experimentId)) {
      throw new CoordinatorError(
        'UNKNOWN_EXPERIMENT',
        `experiment ${JSON.stringify(experimentId)} is not in the frozen roster for round ${round.round}`,
        { round: round.round, expected: [...round.experimentIds] },
      );
    }

    const current = round.attempts.get(experimentId);
    if (!current || current.status === 'superseded') {
      throw new CoordinatorError(
        'NOTHING_TO_REOPEN',
        `experiment "${experimentId}" has no live attempt to reopen`,
        { experimentId, round: round.round },
      );
    }
    if (reason !== undefined && !isNonEmptyString(reason)) {
      throw new CoordinatorError('INVALID_ROSTER', 'reason must be a non-empty string when provided');
    }

    const newAttemptId = attemptId === undefined ? this.#newAttemptId(round) : attemptId;
    if (!isNonEmptyString(newAttemptId)) {
      throw new CoordinatorError('INVALID_ATTEMPT_ID', 'attemptId must be a non-empty string');
    }
    if (newAttemptId === current.attemptId) {
      throw new CoordinatorError(
        'DUPLICATE_ATTEMPT_ID',
        `a reopen must issue a new attempt id; "${newAttemptId}" is being disqualified`,
        { experimentId, attemptId: newAttemptId },
      );
    }
    this.#assertAttemptIdAvailable(round, experimentId, newAttemptId, current);

    const hadSubmission = current.status === 'submitted';
    // Disqualify before anything else, so no late submission can revive it.
    current.status = 'superseded';
    current.supersededBy = newAttemptId;
    current.supersededReason = reason ?? null;

    const placeholder = {
      experimentId,
      attemptId: newAttemptId,
      status: 'open',
      folds: null,
      correct: null,
      samples: null,
      score: null,
      scoreRatio: null,
      checkpoint: null,
      continuedFrom: null,
      supersededBy: null,
      supersededReason: null,
    };
    round.attempts.set(experimentId, placeholder);
    round.attemptLog.push(placeholder);

    this.#log('attempt_superseded', {
      round: round.round,
      experimentId,
      attemptId: current.attemptId,
      supersededBy: newAttemptId,
      reason: reason ?? null,
      hadSubmission,
      disqualification: 'SUPERSEDED_BY_REOPEN',
    });

    return deepFreeze({
      round: round.round,
      trainedTo: round.trainedTo,
      experimentId,
      supersededAttemptId: current.attemptId,
      newAttemptId,
      reason: reason ?? null,
      hadSubmission,
    });
  }

  /**
   * Close the open round: rank eligible attempts, apply the retention rule,
   * and emit continuation commands.
   *
   * By default every rostered experiment must have a current, valid attempt
   * ("收齐"): an experiment that was reopened and never resubmitted blocks
   * settlement. Pass `{ requireAllSubmissions: false }` to settle a partial
   * round explicitly; those experiments are then reported as excluded and can
   * never be promoted.
   *
   * @param {{ requireAllSubmissions?: boolean }} [options]
   */
  settleRound({ requireAllSubmissions = true } = {}) {
    const round = this.#requireOpenRound();
    const foldNames = round.folds.map((fold) => fold.name);

    const eligible = [];
    const excludedExperiments = [];
    const disqualifiedAttempts = [];

    for (const experimentId of round.experimentIds) {
      for (const attempt of round.attemptLog) {
        if (attempt.experimentId !== experimentId || attempt.status !== 'superseded') continue;
        disqualifiedAttempts.push(
          deepFreeze({
            experimentId,
            attemptId: attempt.attemptId,
            reason: 'SUPERSEDED_BY_REOPEN',
            supersededBy: attempt.supersededBy,
            supersededReason: attempt.supersededReason,
            hadSubmission: attempt.folds !== null && attempt.correct !== null,
            correct: attempt.correct,
            samples: attempt.samples,
            scoreRatio:
              attempt.correct === null ? null : ratioString(attempt.correct, attempt.samples),
            checkpoint: attempt.checkpoint ? { ...attempt.checkpoint } : null,
          }),
        );
      }

      const current = round.attempts.get(experimentId);
      if (!current || current.status !== 'submitted') {
        excludedExperiments.push(
          deepFreeze({
            experimentId,
            reason: current && current.status === 'open' ? 'ATTEMPT_OPEN' : 'NO_SUBMISSION',
            liveAttemptId: current ? current.attemptId : null,
            disqualifiedAttempts: round.attemptLog
              .filter(
                (attempt) =>
                  attempt.experimentId === experimentId && attempt.status === 'superseded',
              )
              .map((attempt) => attempt.attemptId),
          }),
        );
        continue;
      }

      // Defensive re-checks: settlement must never trust unchecked input.
      const names = current.folds.map((entry) => entry.name);
      if (names.length !== foldNames.length || names.some((name, i) => name !== foldNames[i])) {
        throw new CoordinatorError(
          'FOLD_SET_MISMATCH',
          `attempt ${current.attemptId} does not cover the frozen folds in order`,
          { experimentId, attemptId: current.attemptId },
        );
      }
      for (const entry of current.folds) {
        const fixed = round.folds.find((fold) => fold.name === entry.name).samples;
        if (fixed !== null && fixed !== entry.samples) {
          throw new CoordinatorError(
            'FOLD_SAMPLE_MISMATCH',
            `fold "${entry.name}" changed sample count after the roster was fixed`,
            { fold: entry.name, fixed, received: entry.samples },
          );
        }
      }
      if (current.checkpoint.step !== round.trainedTo) {
        throw new CoordinatorError(
          'CHECKPOINT_STEP_MISMATCH',
          `attempt ${current.attemptId} checkpoint is not at step ${round.trainedTo}`,
          { experimentId, attemptId: current.attemptId },
        );
      }

      eligible.push({
        experimentId,
        attemptId: current.attemptId,
        folds: current.folds.map((entry) => ({ ...entry })),
        correct: current.correct,
        samples: current.samples,
        score: current.score,
        scoreRatio: ratioString(current.correct, current.samples),
        checkpoint: { ...current.checkpoint },
        continuedFrom: current.continuedFrom ? { ...current.continuedFrom } : null,
      });
    }

    if (requireAllSubmissions && excludedExperiments.length > 0) {
      throw new CoordinatorError(
        'ROUND_INCOMPLETE',
        'some rostered experiments have no final submission for this round',
        {
          round: round.round,
          missing: excludedExperiments.map((entry) => entry.experimentId),
        },
      );
    }
    if (eligible.length === 0) {
      throw new CoordinatorError(
        'NO_ELIGIBLE_EXPERIMENTS',
        `round ${round.round} has no experiment with a valid submission`,
        { round: round.round },
      );
    }

    eligible.sort(compareScoreEntries);
    const retainedCount = retentionCount(eligible.length);
    const promoted = eligible.slice(0, retainedCount);
    const promotedIds = new Set(promoted.map((entry) => entry.experimentId));

    const tieGroups = [];
    let currentGroup = null;
    for (const entry of eligible) {
      if (currentGroup && currentGroup.scoreRatio === entry.scoreRatio) {
        currentGroup.experimentIds.push(entry.experimentId);
        if (promotedIds.has(entry.experimentId)) currentGroup.promotedIds.push(entry.experimentId);
      } else {
        currentGroup = {
          scoreRatio: entry.scoreRatio,
          experimentIds: [entry.experimentId],
          promotedIds: promotedIds.has(entry.experimentId) ? [entry.experimentId] : [],
        };
        tieGroups.push(currentGroup);
      }
    }
    const tieReport = tieGroups
      .filter((group) => group.experimentIds.length > 1)
      .map((group) =>
        deepFreeze({
          scoreRatio: group.scoreRatio,
          experimentIds: [...group.experimentIds],
          promotedIds: [...group.promotedIds],
          resolvedBy: 'experimentIdAscendingNumericAware',
          spansPromotionBoundary:
            group.promotedIds.length > 0 && group.promotedIds.length < group.experimentIds.length,
        }),
      );

    const ranking = eligible.map((entry, index) => ({
      rank: index + 1,
      experimentId: entry.experimentId,
      attemptId: entry.attemptId,
      folds: entry.folds.map((fold) => ({ ...fold })),
      correct: entry.correct,
      samples: entry.samples,
      score: entry.score,
      scoreRatio: entry.scoreRatio,
      promoted: index < retainedCount,
      checkpoint: { ...entry.checkpoint },
      continuedFrom: entry.continuedFrom ? { ...entry.continuedFrom } : null,
    }));
    // Unranked experiments are listed after the ranked ones, never mixed in.
    for (const excluded of excludedExperiments) {
      ranking.push({
        rank: null,
        experimentId: excluded.experimentId,
        attemptId: excluded.liveAttemptId,
        correct: null,
        samples: null,
        score: null,
        scoreRatio: null,
        promoted: false,
        checkpoint: null,
        continuedFrom: null,
      });
    }

    const nextIndex = this.#stages.indexOf(round.trainedTo) + 1;
    const nextStage = nextIndex < this.#stages.length ? this.#stages[nextIndex] : null;

    let continuation = null;
    if (nextStage !== null) {
      continuation = deepFreeze({
        round: round.round + 1,
        targetStep: nextStage,
        commands: promoted.map((entry) =>
          deepFreeze({
            experimentId: entry.experimentId,
            attemptId: entry.attemptId,
            checkpoint: { ...entry.checkpoint },
            additionalSteps: nextStage - entry.checkpoint.step,
          }),
        ),
      });
    }

    const body = {
      kind: 'settlement',
      ruleVersion: RULE_VERSION,
      round: round.round,
      trainedTo: round.trainedTo,
      folds: round.folds.map((fold) => ({ ...fold })),
      retentionRule: 'max(1, floor(eligibleCount / 2))',
      eligibleCount: eligible.length,
      retainedCount,
      ranking,
      promotedExperimentIds: promoted.map((entry) => entry.experimentId),
      tieGroups: tieReport,
      disqualifiedAttempts,
      excludedExperiments,
      continuation,
    };
    const settlement = deepFreeze({
      ...cloneJson(body),
      settlementDigest: computeSettlementDigest(body),
    });

    round.status = 'settled';
    round.settlement = settlement;

    this.#log('round_settled', {
      round: round.round,
      promotedExperimentIds: [...settlement.promotedExperimentIds],
      settlementDigest: settlement.settlementDigest,
    });

    return settlement;
  }

  /**
   * Frozen state snapshot. Superseded attempts remain visible so every
   * disqualification can be audited after the fact.
   */
  snapshot() {
    const rounds = this.#rounds.map((round) => {
      const attempts = round.attemptLog.map((attempt) => ({
        experimentId: attempt.experimentId,
        attemptId: attempt.attemptId,
        status: attempt.status,
        folds: attempt.folds,
        correct: attempt.correct,
        samples: attempt.samples,
        scoreRatio: attempt.scoreRatio,
        checkpoint: attempt.checkpoint,
        continuedFrom: attempt.continuedFrom,
        supersededBy: attempt.supersededBy ?? null,
        supersededReason: attempt.supersededReason ?? null,
      }));
      return {
        round: round.round,
        trainedTo: round.trainedTo,
        status: round.status,
        roster: round.roster,
        attempts,
        settlement: round.settlement,
      };
    });

    return deepFreeze({
      stages: [...this.#stages],
      phase: this.phase,
      rounds: cloneJson(rounds),
      auditLog: cloneJson(this.#audit),
    });
  }
}
