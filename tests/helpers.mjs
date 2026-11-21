import { Tournament } from '../src/index.js';

export const FOLDS = Object.freeze([
  { name: 'small', samples: 100 },
  { name: 'large', samples: 1000 },
]);

/**
 * Build the fold report for the standard ten-to-one fold split.
 *
 * @param {number} smallCorrect
 * @param {number} largeCorrect
 */
export function foldReport(smallCorrect, largeCorrect) {
  return [
    { name: 'small', correct: smallCorrect, samples: 100 },
    { name: 'large', correct: largeCorrect, samples: 1000 },
  ];
}

export function checkpointFor(experimentId, step, suffix = 'ckpt') {
  return {
    path: `/checkpoints/${experimentId}-step${step}-${suffix}.bin`,
    step,
    digest: `digest-${experimentId}-${step}-${suffix}`,
  };
}

/** Fold report for a total correct out of 1100, split 10:90 without overflow. */
export function submission(experimentId, { step = 20, total = 900, suffix = 'ckpt', continuedFrom } = {}) {
  const small = Math.min(100, Math.round(total * 0.1));
  const large = Math.min(1000, total - small);
  return {
    experimentId,
    folds: foldReport(small, large),
    checkpoint: checkpointFor(experimentId, step, suffix),
    ...(continuedFrom === undefined ? {} : { continuedFrom }),
  };
}

/** Continuation reference derived strictly from a settlement command. */
export function referenceFrom(command) {
  const ref = {
    attemptId: command.attemptId,
    path: command.checkpoint.path,
    step: command.checkpoint.step,
  };
  if (command.checkpoint.digest !== undefined) ref.digest = command.checkpoint.digest;
  return ref;
}

/** Two-stage (20 -> 60) tournament with four experiments, ready for round 2. */
export function runFirstRound({ stages = [20, 60], experiments = ['a', 'b', 'c', 'd'], totals } = {}) {
  const tournament = new Tournament({ stages });
  tournament.openRound({ experiments, folds: FOLDS });
  const scores = totals ?? Object.fromEntries(experiments.map((id, index) => [id, 900 - index * 100]));
  for (const id of experiments) {
    tournament.submitAttempt(submission(id, { step: stages[0], total: scores[id] }));
  }
  const settlement = tournament.settleRound();
  return { tournament, settlement, scores };
}

export { Tournament };
