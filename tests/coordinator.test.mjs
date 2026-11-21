import test from 'node:test';
import assert from 'node:assert/strict';

import {
  Tournament,
  CoordinatorError,
  verifySettlementDigest,
  computeSettlementDigest,
} from '../src/index.js';
import {
  FOLDS,
  foldReport,
  checkpointFor,
  submission,
  referenceFrom,
  runFirstRound,
} from './helpers.mjs';

function expectCode(fn, code) {
  assert.throws(fn, (error) => {
    assert.ok(error instanceof CoordinatorError, `expected CoordinatorError, got ${error}`);
    assert.equal(error.code, code, `expected code ${code}, got ${error.code}: ${error.message}`);
    return true;
  });
}

test('normal: 20 -> 60 -> 180 with half retention at every stage', () => {
  const tournament = new Tournament({ stages: [20, 60, 180] });

  const round1 = tournament.openRound({
    experiments: ['e1', 'e2', 'e3', 'e4', 'e5', 'e6', 'e7', 'e8'],
    folds: FOLDS,
  });
  assert.equal(round1.round, 1);
  assert.equal(round1.trainedTo, 20);
  assert.deepEqual(round1.folds.map((f) => f.name), ['small', 'large']);

  // Scores out of 1100, strictly descending.
  const totals = { e1: 1000, e2: 950, e3: 900, e4: 850, e5: 800, e6: 750, e7: 700, e8: 650 };
  for (const [id, total] of Object.entries(totals)) {
    tournament.submitAttempt(submission(id, { step: 20, total }));
  }

  const s1 = tournament.settleRound();
  assert.equal(s1.round, 1);
  assert.equal(s1.eligibleCount, 8);
  assert.equal(s1.retainedCount, 4);
  assert.deepEqual(s1.promotedExperimentIds, ['e1', 'e2', 'e3', 'e4']);
  assert.deepEqual(
    s1.continuation.commands.map((c) => [c.experimentId, c.additionalSteps]),
    [['e1', 40], ['e2', 40], ['e3', 40], ['e4', 40]],
  );
  assert.ok(verifySettlementDigest(s1));

  // Round 2 roster must equal the promotion list, and commands are required.
  expectCode(() => tournament.openRound({ experiments: ['e1', 'e2'], folds: FOLDS }), 'ROSTER_MISMATCH');
  const round2 = tournament.openRound({ folds: FOLDS });
  assert.deepEqual(round2.experimentIds, ['e1', 'e2', 'e3', 'e4']);
  assert.equal(round2.trainedTo, 60);

  const round1Commands = new Map(s1.continuation.commands.map((c) => [c.experimentId, c]));
  const r2Totals = { e1: 900, e2: 950, e3: 850, e4: 1000 };
  for (const [id, total] of Object.entries(r2Totals)) {
    tournament.submitAttempt(
      submission(id, {
        step: 60,
        total,
        suffix: 'r2',
        continuedFrom: referenceFrom(round1Commands.get(id)),
      }),
    );
  }

  const s2 = tournament.settleRound();
  assert.equal(s2.round, 2);
  assert.equal(s2.retainedCount, 2);
  assert.deepEqual(s2.promotedExperimentIds, ['e4', 'e2']);
  assert.deepEqual(
    s2.continuation.commands.map((c) => [c.experimentId, c.additionalSteps]),
    [['e4', 120], ['e2', 120]],
  );

  const round2Commands = new Map(s2.continuation.commands.map((c) => [c.experimentId, c]));
  const round3 = tournament.openRound({ folds: FOLDS });
  assert.deepEqual(round3.experimentIds, ['e4', 'e2']);
  assert.equal(round3.trainedTo, 180);
  for (const id of ['e4', 'e2']) {
    tournament.submitAttempt(
      submission(id, {
        step: 180,
        total: id === 'e4' ? 1050 : 1000,
        suffix: 'r3',
        continuedFrom: referenceFrom(round2Commands.get(id)),
      }),
    );
  }

  const s3 = tournament.settleRound();
  assert.equal(s3.retainedCount, 1);
  assert.deepEqual(s3.promotedExperimentIds, ['e4']);
  assert.equal(s3.continuation, null);
  assert.equal(tournament.phase, 'complete');
  expectCode(() => tournament.openRound({ folds: FOLDS }), 'TOURNAMENT_COMPLETE');

  // Every round's digest is independently reproducible.
  for (const settlement of tournament.settlements) {
    assert.equal(computeSettlementDigest(settlement), settlement.settlementDigest);
    assert.ok(verifySettlementDigest(settlement));
  }
});

test('boundary: retention count floors to half with at least one', () => {
  for (const n of [1, 2, 3, 4, 5, 6, 7, 9]) {
    const ids = Array.from({ length: n }, (_, i) => `x${i + 1}`);
    const tournament = new Tournament({ stages: [20, 60] });
    tournament.openRound({ experiments: ids, folds: FOLDS });
    ids.forEach((id, index) => {
      tournament.submitAttempt(submission(id, { step: 20, total: 1100 - index * 10 }));
    });
    const settlement = tournament.settleRound();
    assert.equal(settlement.retainedCount, Math.max(1, Math.floor(n / 2)), `n=${n}`);
    assert.equal(settlement.promotedExperimentIds.length, settlement.retainedCount);
    assert.equal(settlement.continuation.commands.length, settlement.retainedCount);
  }
});

test('boundary: single experiment is retained and gets a continuation command', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['solo'], folds: FOLDS });
  tournament.submitAttempt(submission('solo', { step: 20, total: 800 }));
  const settlement = tournament.settleRound();
  assert.equal(settlement.retainedCount, 1);
  assert.deepEqual(settlement.promotedExperimentIds, ['solo']);
  assert.equal(settlement.continuation.commands[0].additionalSteps, 40);
});

test('score is sample-weighted across 10:1 fold sizes, not a macro average', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  // small = 10 samples, large = 1000 samples: exactly ten times.
  tournament.openRound({
    experiments: ['macroWinner', 'weightedWinner'],
    folds: [
      { name: 'small', samples: 10 },
      { name: 'large', samples: 1000 },
    ],
  });

  // macroWinner: 9/10 and 800/1000 => 809/1010 = 0.80099 ; macro 0.85
  // weightedWinner: 5/10 and 900/1000 => 905/1010 = 0.89604 ; macro 0.70
  tournament.submitAttempt({
    experimentId: 'macroWinner',
    folds: [
      { name: 'small', correct: 9, samples: 10 },
      { name: 'large', correct: 800, samples: 1000 },
    ],
    checkpoint: checkpointFor('macroWinner', 20),
  });
  tournament.submitAttempt({
    experimentId: 'weightedWinner',
    folds: [
      { name: 'small', correct: 5, samples: 10 },
      { name: 'large', correct: 900, samples: 1000 },
    ],
    checkpoint: checkpointFor('weightedWinner', 20),
  });

  const settlement = tournament.settleRound();
  assert.deepEqual(settlement.promotedExperimentIds, ['weightedWinner']);
  const [first, second] = settlement.ranking.filter((r) => r.rank !== null);
  assert.equal(first.experimentId, 'weightedWinner');
  assert.equal(first.scoreRatio, '181/202'); // 905/1010
  assert.equal(second.scoreRatio, '809/1010');
  assert.ok(first.score > second.score);
});

test('interleaved: ties are broken by experiment id and promotion boundary is reported', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  // Five experiments, three tie exactly on 900/1100.
  // eligible=5 => retain floor(5/2)=2. Tied group of three spans the boundary.
  tournament.openRound({ experiments: ['10', '2', '3', '4', '5'], folds: FOLDS });
  const scores = { 10: 1000, 2: 900, 3: 900, 4: 900, 5: 800 };
  for (const [id, total] of Object.entries(scores)) {
    tournament.submitAttempt(submission(id, { step: 20, total }));
  }

  const settlement = tournament.settleRound();
  assert.equal(settlement.retainedCount, 2);
  // Numeric ordering inside the tie: 2, 3, 4 -> promote 2 and 3.
  assert.deepEqual(settlement.promotedExperimentIds, ['10', '2']);
  assert.deepEqual(
    settlement.ranking.filter((r) => r.rank !== null).map((r) => r.experimentId),
    ['10', '2', '3', '4', '5'],
  );
  const tie = settlement.tieGroups.find((g) => g.experimentIds.length === 3);
  assert.deepEqual(tie.experimentIds, ['2', '3', '4']);
  assert.deepEqual(tie.promotedIds, ['2']);
  assert.equal(tie.spansPromotionBoundary, true);
  assert.equal(tie.resolvedBy, 'experimentIdAscendingNumericAware');
});

test('interleaved: unrelated earlier attempt cannot win via a stale high score after reopen', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a', 'b', 'c'], folds: FOLDS });

  // a posts a very high result...
  tournament.submitAttempt({
    experimentId: 'a',
    folds: foldReport(100, 1000), // 1100/1100
    checkpoint: checkpointFor('a', 20, 'stale'),
  });
  tournament.submitAttempt(submission('b', { step: 20, total: 900 }));
  tournament.submitAttempt(submission('c', { step: 20, total: 800 }));

  // ...then is reopened, disqualifying that result entirely.
  const reopened = tournament.reopenExperiment({ experimentId: 'a', reason: 'restart from scratch' });
  assert.equal(reopened.supersededAttemptId, 'r1a1');
  assert.equal(reopened.newAttemptId, 'r1a4');
  assert.equal(reopened.hadSubmission, true);

  // The cancelled attempt's checkpoint (same digest) can never come back.
  expectCode(
    () =>
      tournament.submitAttempt(
        submission('a', { step: 20, total: 700, suffix: 'stale' }),
      ),
    'SUPERSEDED_CHECKPOINT',
  );
  // Same bytes, different path label: digest is what disqualifies it.
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: foldReport(70, 630),
        checkpoint: { ...checkpointFor('a', 20, 'stale'), path: '/checkpoints/copied.bin' },
      }),
    'SUPERSEDED_CHECKPOINT',
  );

  // A genuinely fresh attempt is accepted and replaces the disqualified one.
  tournament.submitAttempt(submission('a', { step: 20, total: 700, suffix: 'fresh' }));

  const settlement = tournament.settleRound();
  // 3 eligible => retain 1: b (900) beats a (700) and c (800).
  assert.equal(settlement.eligibleCount, 3);
  assert.equal(settlement.retainedCount, 1);
  assert.deepEqual(settlement.promotedExperimentIds, ['b']);
  const promoted = settlement.ranking.find((r) => r.experimentId === 'b');
  assert.equal(promoted.rank, 1);

  const disqualified = settlement.disqualifiedAttempts.find((d) => d.experimentId === 'a');
  assert.equal(disqualified.reason, 'SUPERSEDED_BY_REOPEN');
  assert.equal(disqualified.hadSubmission, true);
  assert.equal(disqualified.scoreRatio, '1/1');
  assert.equal(disqualified.checkpoint.path, checkpointFor('a', 20, 'stale').path);
  // No ranking row may expose the stale attempt.
  assert.ok(
    settlement.ranking.every(
      (row) => !(row.experimentId === 'a' && row.scoreRatio === '1/1'),
    ),
  );
});

test('interleaved: old attempt folds and checkpoint are disqualified even if the replacement is worse', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a', 'b'], folds: FOLDS });
  tournament.submitAttempt({
    experimentId: 'a',
    folds: foldReport(100, 1000),
    checkpoint: checkpointFor('a', 20, 'first'),
  });
  tournament.submitAttempt(submission('b', { step: 20, total: 950 }));

  tournament.reopenExperiment({ experimentId: 'a', reason: 'bad checkpoint' });
  tournament.submitAttempt({
    experimentId: 'a',
    folds: foldReport(10, 100), // 110/1100, much worse
    checkpoint: checkpointFor('a', 20, 'second'),
  });

  const settlement = tournament.settleRound();
  // eligible = 2 => retain 1. b (950) beats a (110). a is not promoted.
  assert.deepEqual(settlement.promotedExperimentIds, ['b']);
  const rowA = settlement.ranking.find((r) => r.experimentId === 'a' && r.rank !== null);
  assert.equal(rowA.scoreRatio, '1/10');
  assert.equal(rowA.attemptId, 'r1a3');
  assert.equal(rowA.checkpoint.path, checkpointFor('a', 20, 'second').path);
});

test('interleaved: late result after settlement is rejected and audited, cannot participate', () => {
  const { tournament, settlement } = runFirstRound({
    experiments: ['a', 'b', 'c', 'd'],
    totals: { a: 1000, b: 900, c: 800, d: 700 },
  });
  assert.deepEqual(settlement.promotedExperimentIds, ['a', 'b']);

  expectCode(
    () => tournament.submitAttempt(submission('d', { step: 20, total: 1100, suffix: 'late' })),
    'ROUND_CLOSED',
  );

  const audit = tournament.auditLog;
  const late = audit.filter(
    (entry) => entry.type === 'submission_rejected' && entry.experimentId === 'd',
  );
  assert.equal(late.length, 1);
  assert.equal(late[0].code, 'ROUND_CLOSED');
  assert.equal(late[0].round, 1);

  // The settled promotion list is unchanged.
  assert.deepEqual(tournament.lastSettlement.promotedExperimentIds, ['a', 'b']);
});

test('interleaved: late high score cannot be laundered by reopening after settlement', () => {
  const { tournament } = runFirstRound({
    experiments: ['a', 'b', 'c'],
    totals: { a: 900, b: 800, c: 700 },
  });
  expectCode(() => tournament.reopenExperiment({ experimentId: 'c' }), 'ROUND_CLOSED');
  expectCode(
    () => tournament.submitAttempt(submission('c', { step: 20, total: 1100 })),
    'ROUND_CLOSED',
  );
  assert.deepEqual(tournament.lastSettlement.promotedExperimentIds, ['a']);
  assert.equal(tournament.lastSettlement.eligibleCount, 3);
});

test('continuation commands reference the promoted checkpoint and state only added steps', () => {
  const { tournament, settlement } = runFirstRound({
    experiments: ['a', 'b', 'c', 'd'],
    totals: { a: 1000, b: 950, c: 900, d: 850 },
  });

  const promotedRows = settlement.ranking.filter((row) => row.promoted);
  assert.equal(promotedRows.length, 2);
  for (const command of settlement.continuation.commands) {
    const row = promotedRows.find((r) => r.experimentId === command.experimentId);
    assert.ok(row, 'command must map to a promoted row');
    // Exact checkpoint identity, not a copied value.
    assert.equal(command.attemptId, row.attemptId);
    assert.equal(command.checkpoint.path, row.checkpoint.path);
    assert.equal(command.checkpoint.step, row.checkpoint.step);
    assert.equal(command.checkpoint.digest, row.checkpoint.digest);
    // Only the delta is given; target/start are not stated.
    assert.equal(command.additionalSteps, 40);
    assert.deepEqual(Object.keys(command).sort(), [
      'additionalSteps',
      'attemptId',
      'checkpoint',
      'experimentId',
    ]);
    assert.equal('targetStep' in command, false);
    assert.equal('startStep' in command, false);
    assert.equal('fromStep' in command, false);
    // The checkpoint's own step is the observed state at handoff.
    assert.equal(row.checkpoint.step, 20);
    // The promoted checkpoint came from this round's promoted attempt.
    assert.equal(command.checkpoint.digest, row.checkpoint.digest);
  }
  // The next stage's target is stated once on the continuation block.
  assert.equal(settlement.continuation.targetStep, 60);
  assert.equal(settlement.continuation.round, 2);
});

test('ten-to-one folds: promotion list and continuation start are independently checkable', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  const folds = [
    { name: 'tiny', samples: 10 },
    { name: 'big', samples: 1000 },
  ];
  tournament.openRound({ experiments: ['a', 'b', 'c'], folds });

  // Recompute expected scores by hand from raw fold counts.
  const raw = {
    a: [
      { name: 'tiny', correct: 1, samples: 10 },
      { name: 'big', correct: 990, samples: 1000 },
    ],
    b: [
      { name: 'tiny', correct: 10, samples: 10 },
      { name: 'big', correct: 900, samples: 1000 },
    ],
    c: [
      { name: 'tiny', correct: 9, samples: 10 },
      { name: 'big', correct: 850, samples: 1000 },
    ],
  };
  const expected = Object.fromEntries(
    Object.entries(raw).map(([id, report]) => {
      const correct = report.reduce((sum, f) => sum + f.correct, 0);
      const samples = report.reduce((sum, f) => sum + f.samples, 0);
      return [id, { correct, samples }];
    }),
  );
  // a: 991/1010, b: 910/1010, c: 859/1010
  assert.deepEqual(
    Object.fromEntries(Object.entries(expected).map(([id, v]) => [id, v.correct])),
    { a: 991, b: 910, c: 859 },
  );

  for (const [id, report] of Object.entries(raw)) {
    tournament.submitAttempt({
      experimentId: id,
      folds: report,
      checkpoint: checkpointFor(id, 20),
    });
  }
  // The roster's declared fold sizes must match the reported sizes exactly.
  assert.deepEqual(
    tournament.snapshot().rounds[0].roster.folds,
    [
      { name: 'tiny', samples: 10 },
      { name: 'big', samples: 1000 },
    ],
  );

  const settlement = tournament.settleRound();
  // Independent promotion check: rank by exact cross-multiplied totals.
  const ranked = Object.entries(expected)
    .map(([id, v]) => ({ id, ...v }))
    .sort((x, y) => {
      const left = BigInt(x.correct) * BigInt(y.samples);
      const right = BigInt(y.correct) * BigInt(x.samples);
      if (left !== right) return left > right ? -1 : 1;
      return x.id < y.id ? -1 : x.id > y.id ? 1 : 0;
    });
  const expectedPromoted = ranked.slice(0, Math.max(1, Math.floor(ranked.length / 2))).map((r) => r.id);
  assert.deepEqual(settlement.promotedExperimentIds, expectedPromoted);
  assert.deepEqual(settlement.promotedExperimentIds, ['a']);

  // Independent continuation-start check: start == promoted checkpoint step,
  // added steps == next stage - start, and the command carries no absolute.
  const promotionRow = settlement.ranking.find((r) => r.promoted);
  const command = settlement.continuation.commands.find(
    (c) => c.experimentId === promotionRow.experimentId,
  );
  const startStep = command.checkpoint.step;
  assert.equal(startStep, 20);
  assert.equal(startStep + command.additionalSteps, 60);
  assert.equal(command.checkpoint.path, checkpointFor('a', 20).path);
});

test('boundary: fold roster is frozen against unknown, duplicate, missing and resized folds', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a'], folds: FOLDS });

  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: [
          { name: 'small', correct: 1, samples: 100 },
          { name: 'large', correct: 1, samples: 1000 },
          { name: 'extra', correct: 1, samples: 10 },
        ],
        checkpoint: checkpointFor('a', 20),
      }),
    'FOLD_SET_MISMATCH',
  );

  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: [
          { name: 'small', correct: 1, samples: 100 },
          { name: 'small', correct: 1, samples: 100 },
        ],
        checkpoint: checkpointFor('a', 20),
      }),
    'FOLD_SET_MISMATCH',
  );

  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: [{ name: 'small', correct: 1, samples: 100 }],
        checkpoint: checkpointFor('a', 20),
      }),
    'FOLD_SET_MISMATCH',
  );

  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: [
          { name: 'small', correct: 1, samples: 99 },
          { name: 'large', correct: 1, samples: 1000 },
        ],
        checkpoint: checkpointFor('a', 20),
      }),
    'FOLD_SAMPLE_MISMATCH',
  );

  // None of the rejected submissions counted.
  expectCode(() => tournament.settleRound(), 'ROUND_INCOMPLETE');
  expectCode(() => tournament.settleRound({ requireAllSubmissions: false }), 'NO_ELIGIBLE_EXPERIMENTS');
});

test('boundary: unknown experiment and duplicate active submission are rejected', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a', 'b'], folds: FOLDS });
  expectCode(
    () => tournament.submitAttempt(submission('zzz', { step: 20 })),
    'UNKNOWN_EXPERIMENT',
  );
  tournament.submitAttempt(submission('a', { step: 20 }));
  expectCode(
    () => tournament.submitAttempt(submission('a', { step: 20 })),
    'ATTEMPT_ALREADY_ACTIVE',
  );
  expectCode(() => tournament.reopenExperiment({ experimentId: 'b' }), 'NOTHING_TO_REOPEN');
});

test('boundary: checkpoint step must equal the round stage exactly', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a'], folds: FOLDS });
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: foldReport(10, 900),
        checkpoint: checkpointFor('a', 19),
      }),
    'CHECKPOINT_STEP_MISMATCH',
  );
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: foldReport(10, 900),
        checkpoint: checkpointFor('a', 60),
      }),
    'CHECKPOINT_STEP_MISMATCH',
  );
});

test('interleaved: continuation round requires the exact promoted checkpoint as continuedFrom', () => {
  const { tournament, settlement } = runFirstRound({
    experiments: ['a', 'b', 'c', 'd'],
    totals: { a: 1000, b: 900, c: 800, d: 700 },
  });
  const commands = new Map(settlement.continuation.commands.map((c) => [c.experimentId, c]));
  tournament.openRound({ folds: FOLDS });

  const goodRef = referenceFrom(commands.get('a'));

  // Missing reference.
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: foldReport(10, 900),
        checkpoint: checkpointFor('a', 60, 'r2'),
      }),
    'MISSING_CONTINUATION_BASE',
  );
  // Wrong attempt id.
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: foldReport(10, 900),
        checkpoint: checkpointFor('a', 60, 'r2'),
        continuedFrom: { ...goodRef, attemptId: 'a9' },
      }),
    'CONTINUATION_BASE_MISMATCH',
  );
  // Wrong path.
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: foldReport(10, 900),
        checkpoint: checkpointFor('a', 60, 'r2'),
        continuedFrom: { ...goodRef, path: '/checkpoints/forged.bin' },
      }),
    'CONTINUATION_BASE_MISMATCH',
  );
  // Wrong digest.
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: foldReport(10, 900),
        checkpoint: checkpointFor('a', 60, 'r2'),
        continuedFrom: { ...goodRef, digest: 'forged' },
      }),
    'CONTINUATION_BASE_MISMATCH',
  );

  // The correct reference is accepted and recorded.
  const accepted = tournament.submitAttempt({
    experimentId: 'a',
    folds: foldReport(10, 900),
    checkpoint: checkpointFor('a', 60, 'r2'),
    continuedFrom: goodRef,
  });
  assert.deepEqual(accepted.continuedFrom, goodRef);
  assert.equal(accepted.continuedFrom.step, 20);
});

test('boundary: round 1 attempts may not claim a continuation base', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a'], folds: FOLDS });
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: foldReport(10, 900),
        checkpoint: checkpointFor('a', 20),
        continuedFrom: { attemptId: 'a0', path: '/x', step: 0 },
      }),
    'CONTINUATION_BASE_MISMATCH',
  );
});

test('boundary: a fold size fixed by an earlier round cannot be silently resized', () => {
  const { tournament } = runFirstRound({
    experiments: ['a', 'b'],
    totals: { a: 1000, b: 900 },
  });
  expectCode(
    () =>
      tournament.openRound({
        folds: [
          { name: 'small', samples: 200 },
          { name: 'large', samples: 1000 },
        ],
      }),
    'FOLD_SAMPLE_CONFLICT',
  );
  // Opening again with a consistent declaration works.
  const round2 = tournament.openRound({ folds: FOLDS });
  assert.equal(round2.round, 2);
});

test('boundary: checkpoint must carry a content digest', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a'], folds: FOLDS });
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        folds: foldReport(10, 900),
        checkpoint: { path: '/checkpoints/a.bin', step: 20 },
      }),
    'INVALID_CHECKPOINT',
  );
});

test('boundary: attempt ids are unique per round across experiments', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a', 'b'], folds: FOLDS });
  tournament.submitAttempt({
    experimentId: 'a',
    attemptId: 'shared',
    folds: foldReport(10, 900),
    checkpoint: checkpointFor('a', 20),
  });
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'b',
        attemptId: 'shared',
        folds: foldReport(10, 800),
        checkpoint: checkpointFor('b', 20),
      }),
    'DUPLICATE_ATTEMPT_ID',
  );
});

test('boundary: a reopen must issue a fresh attempt id', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a'], folds: FOLDS });
  tournament.submitAttempt({
    experimentId: 'a',
    attemptId: 'only',
    folds: foldReport(10, 900),
    checkpoint: checkpointFor('a', 20),
  });
  expectCode(
    () => tournament.reopenExperiment({ experimentId: 'a', attemptId: 'only' }),
    'DUPLICATE_ATTEMPT_ID',
  );
  // The attempt was not touched by the failed reopen.
  const settlement = tournament.settleRound();
  assert.equal(settlement.ranking[0].attemptId, 'only');
});

test('boundary: a continuation round carries the fold roster forward when omitted', () => {
  const { tournament } = runFirstRound({
    experiments: ['a', 'b'],
    totals: { a: 1000, b: 900 },
  });
  const round2 = tournament.openRound({});
  assert.deepEqual(
    round2.folds.map((fold) => [fold.name, fold.samples]),
    [
      ['small', 100],
      ['large', 1000],
    ],
  );
  assert.deepEqual(round2.experimentIds, ['a']);
});

test('boundary: openRound requires experiments on round 1 and folds always', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  expectCode(() => tournament.openRound({ folds: FOLDS }), 'INVALID_ROSTER');
  expectCode(() => tournament.openRound({ experiments: ['a'] }), 'INVALID_ROSTER');
  expectCode(() => tournament.openRound({ experiments: ['a'], folds: [] }), 'INVALID_ROSTER');
  expectCode(
    () => tournament.openRound({ experiments: ['a'], folds: ['f', 'f'] }),
    'INVALID_ROSTER',
  );
});

test('boundary: a failed openRound leaves no fold state behind', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  // Opening round 1 is rejected because `experiments` is missing, even though
  // the fold list declares 'ghost' with 500 samples. That size must not leak.
  expectCode(
    () => tournament.openRound({ folds: [{ name: 'ghost', samples: 500 }] }),
    'INVALID_ROSTER',
  );
  const round1 = tournament.openRound({ experiments: ['a'], folds: ['ghost'] });
  assert.equal(round1.folds[0].samples, null);

  // A failed continuation open must also leave the declared size unrecorded.
  tournament.submitAttempt({
    experimentId: 'a',
    folds: [{ name: 'ghost', correct: 5, samples: 50 }],
    checkpoint: checkpointFor('a', 20),
  });
  const settlement = tournament.settleRound();
  assert.equal(settlement.eligibleCount, 1);
  // Rostering an experiment outside the promotion list fails before folds.
  expectCode(
    () => tournament.openRound({ experiments: ['nope'], folds: [{ name: 'other', samples: 500 }] }),
    'ROSTER_MISMATCH',
  );
  // 'other' must still be unknown, so declaring a different size is allowed.
  const round2 = tournament.openRound({ folds: [{ name: 'other', samples: 250 }] });
  assert.equal(round2.folds[0].samples, 250);
});

test('boundary: settlement requires every submission by default, partial only on request', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a', 'b'], folds: FOLDS });
  tournament.submitAttempt(submission('a', { step: 20, total: 900 }));
  expectCode(() => tournament.settleRound(), 'ROUND_INCOMPLETE');
  const settlement = tournament.settleRound({ requireAllSubmissions: false });
  assert.deepEqual(settlement.excludedExperiments.map((e) => e.experimentId), ['b']);
  assert.deepEqual(settlement.promotedExperimentIds, ['a']);
});

test('boundary: an experiment reopened and never resubmitted is excluded from settlement', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a', 'b'], folds: FOLDS });
  tournament.submitAttempt(submission('a', { step: 20, total: 1000 }));
  tournament.submitAttempt(submission('b', { step: 20, total: 900 }));
  tournament.reopenExperiment({ experimentId: 'a', reason: 'invalidate' });

  // Default settlement refuses to proceed while a rostered experiment is open.
  expectCode(() => tournament.settleRound(), 'ROUND_INCOMPLETE');
  const settlement = tournament.settleRound({ requireAllSubmissions: false });
  assert.equal(settlement.eligibleCount, 1);
  assert.equal(settlement.retainedCount, 1);
  assert.deepEqual(settlement.promotedExperimentIds, ['b']);
  assert.deepEqual(settlement.excludedExperiments.map((e) => e.experimentId), ['a']);
  assert.equal(settlement.excludedExperiments[0].reason, 'ATTEMPT_OPEN');
  assert.deepEqual(settlement.excludedExperiments[0].disqualifiedAttempts, ['r1a1']);
  assert.equal(settlement.disqualifiedAttempts.length, 1);
  assert.equal(settlement.disqualifiedAttempts[0].scoreRatio, '10/11');
});

test('boundary: reopening twice replaces the placeholder, old attempts stay disqualified', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a'], folds: FOLDS });
  tournament.submitAttempt(submission('a', { step: 20, total: 900 }));
  tournament.reopenExperiment({ experimentId: 'a' });
  tournament.reopenExperiment({ experimentId: 'a' });
  tournament.submitAttempt(submission('a', { step: 20, total: 600, suffix: 'third' }));
  const settlement = tournament.settleRound();
  assert.equal(settlement.disqualifiedAttempts.length, 2);
  assert.deepEqual(
    settlement.disqualifiedAttempts.map((d) => d.attemptId),
    ['r1a1', 'r1a2'],
  );
  assert.equal(settlement.ranking[0].attemptId, 'r1a3');
});

test('boundary: roster and settlement snapshots are deeply frozen', () => {
  const { tournament, settlement } = runFirstRound({
    experiments: ['a', 'b'],
    totals: { a: 900, b: 800 },
  });
  assert.ok(Object.isFrozen(settlement));
  assert.ok(Object.isFrozen(settlement.ranking));
  assert.ok(Object.isFrozen(settlement.ranking[0]));
  assert.ok(Object.isFrozen(settlement.continuation.commands[0]));
  assert.ok(Object.isFrozen(tournament.auditLog));
  assert.ok(Object.isFrozen(tournament.snapshot()));

  // Mutating a returned settlement must not change internal state.
  assert.throws(() => {
    settlement.promotedExperimentIds.push('b');
  }, TypeError);
});

test('boundary: tampered settlement fails digest verification', () => {
  const { settlement } = runFirstRound({ experiments: ['a', 'b'], totals: { a: 900, b: 800 } });
  assert.ok(verifySettlementDigest(settlement));
  const tampered = { ...settlement, retainedCount: 2 };
  assert.equal(verifySettlementDigest(tampered), false);
  const forged = { ...settlement, promotedExperimentIds: ['b'] };
  assert.equal(verifySettlementDigest(forged), false);
});

test('interleaved: audit log records every accepted, rejected and superseded event in order', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a', 'b'], folds: FOLDS });
  tournament.submitAttempt(submission('a', { step: 20, total: 900 }));
  tournament.submitAttempt(submission('b', { step: 20, total: 800 }));
  tournament.reopenExperiment({ experimentId: 'b' });
  tournament.submitAttempt(submission('b', { step: 20, total: 850, suffix: 'b2' }));
  tournament.settleRound();

  const audit = tournament.auditLog;
  const types = audit.map((entry) => entry.type);
  assert.deepEqual(types, [
    'round_opened',
    'attempt_submitted',
    'attempt_submitted',
    'attempt_superseded',
    'attempt_submitted',
    'round_settled',
  ]);
  assert.deepEqual(
    audit.map((entry) => entry.seq),
    [1, 2, 3, 4, 5, 6],
  );
  const superseded = audit.find((entry) => entry.type === 'attempt_superseded');
  assert.equal(superseded.experimentId, 'b');
  assert.equal(superseded.hadSubmission, true);
  assert.equal(superseded.supersededBy, 'r1a3');
});

test('interleaved: resubmitting an attempt id already used is rejected', () => {
  const tournament = new Tournament({ stages: [20, 60] });
  tournament.openRound({ experiments: ['a'], folds: FOLDS });
  tournament.submitAttempt({
    experimentId: 'a',
    attemptId: 'custom-1',
    folds: foldReport(10, 900),
    checkpoint: checkpointFor('a', 20),
  });
  tournament.reopenExperiment({ experimentId: 'a', attemptId: 'custom-2' });
  expectCode(
    () =>
      tournament.submitAttempt({
        experimentId: 'a',
        attemptId: 'custom-1',
        folds: foldReport(10, 800),
        checkpoint: checkpointFor('a', 20, 'fresh'),
      }),
    'ATTEMPT_ID_MISMATCH',
  );
  // Submitting under the assigned placeholder id works.
  const accepted = tournament.submitAttempt({
    experimentId: 'a',
    attemptId: 'custom-2',
    folds: foldReport(10, 800),
    checkpoint: checkpointFor('a', 20, 'fresh'),
  });
  assert.equal(accepted.attemptId, 'custom-2');
});
