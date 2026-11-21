/**
 * End-to-end walkthrough of the coordinator on synthetic data.
 *
 * It exercises the full 20 -> 60 -> 180 pipeline (including a reopen that
 * disqualifies an earlier attempt) and then *independently* re-derives the
 * promotion list and continuation start steps from the raw fold counts, using
 * the same rules but separate code. A mismatch exits non-zero.
 *
 * Run: node examples/demo.mjs
 */
import { Tournament, verifySettlementDigest, auditSettlement } from '../src/index.js';

const FOLDS = [
  { name: 'tiny', samples: 10 }, // ten times smaller than 'big'
  { name: 'big', samples: 1000 },
];

function folds(tiny, big) {
  return [
    { name: 'tiny', correct: tiny, samples: 10 },
    { name: 'big', correct: big, samples: 1000 },
  ];
}

function checkpoint(id, step, tag) {
  return { path: `/ckpt/${id}-${step}-${tag}.bin`, step, digest: `${id}-${step}-${tag}` };
}

function ref(command) {
  return {
    attemptId: command.attemptId,
    path: command.checkpoint.path,
    step: command.checkpoint.step,
    digest: command.checkpoint.digest,
  };
}

/** Independent re-derivation: exact rational ranking, floor-half retention. */
function crossCheck(settlement, rawCounts) {
  const rows = Object.entries(rawCounts).map(([id, report]) => {
    const correct = report.reduce((sum, fold) => sum + fold.correct, 0);
    const samples = report.reduce((sum, fold) => sum + fold.samples, 0);
    return { id, correct, samples };
  });
  rows.sort((x, y) => {
    const left = BigInt(x.correct) * BigInt(y.samples);
    const right = BigInt(y.correct) * BigInt(x.samples);
    if (left !== right) return left > right ? -1 : 1;
    const xn = /^\d+$/.test(x.id) ? BigInt(x.id) : null;
    const yn = /^\d+$/.test(y.id) ? BigInt(y.id) : null;
    if (xn !== null && yn !== null && xn !== yn) return xn < yn ? -1 : 1;
    if (xn !== null && yn === null) return -1;
    if (xn === null && yn !== null) return 1;
    return x.id < y.id ? -1 : x.id > y.id ? 1 : 0;
  });
  const retained = rows.length === 0 ? 0 : Math.max(1, Math.floor(rows.length / 2));
  return {
    promoted: rows.slice(0, retained).map((row) => row.id),
    retained,
    scores: Object.fromEntries(rows.map((row) => [row.id, `${row.correct}/${row.samples}`])),
  };
}

function assertEqual(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    console.error(`MISMATCH ${label}\n  expected ${e}\n  actual   ${a}`);
    process.exitCode = 1;
    return false;
  }
  console.log(`ok ${label}: ${a}`);
  return true;
}

const tournament = new Tournament({ stages: [20, 60, 180] });

// ---- Round 1: 6 experiments -------------------------------------------------
const round1Roster = ['10', '2', '3', '4', '5', '6'];
tournament.openRound({ experiments: round1Roster, folds: FOLDS });

const r1Raw = {
  10: folds(9, 880),
  2: folds(8, 820),
  3: folds(7, 810),
  4: folds(6, 800),
  5: folds(5, 500),
  6: folds(4, 400),
};

let sawStaleSubmissionRejected = false;

for (const id of round1Roster) {
  if (id === '6') {
    // First a poor attempt, then reopen it before the round closes.
    tournament.submitAttempt({
      experimentId: '6',
      folds: folds(10, 1000),
      checkpoint: checkpoint('6', 20, 'doomed'),
    });
    tournament.reopenExperiment({ experimentId: '6', reason: 'corrupted checkpoint' });
    try {
      // Resubmitting the disqualified checkpoint must fail.
      tournament.submitAttempt({
        experimentId: '6',
        folds: folds(10, 1000),
        checkpoint: checkpoint('6', 20, 'doomed'),
      });
    } catch (error) {
      sawStaleSubmissionRejected = error.code === 'SUPERSEDED_CHECKPOINT';
    }
  }
  tournament.submitAttempt({
    experimentId: id,
    folds: r1Raw[id],
    checkpoint: checkpoint(id, 20, 'r1'),
  });
}

const s1 = tournament.settleRound();
console.log(`\nRound 1 settlement digest ${s1.settlementDigest.slice(0, 16)}...`);
const r1Check = crossCheck(s1, r1Raw);
assertEqual(s1.promotedExperimentIds, r1Check.promoted, 'round 1 promotion list');
assertEqual(s1.retainedCount, r1Check.retained, 'round 1 retained count');
assertEqual(s1.ranking.find((row) => row.experimentId === '6').scoreRatio, '2/5', 'round 1 experiment 6 score');
assertEqual(
  s1.disqualifiedAttempts.map((d) => [d.attemptId, d.reason, d.hadSubmission]),
  [['r1a6', 'SUPERSEDED_BY_REOPEN', true]],
  'round 1 disqualified attempt',
);
assertEqual(sawStaleSubmissionRejected, true, 'disqualified checkpoint resubmission rejected');

// ---- Round 2: promoted experiments only, +40 steps --------------------------
const r1Commands = new Map(s1.continuation.commands.map((c) => [c.experimentId, c]));
const round2 = tournament.openRound({ folds: FOLDS });
assertEqual(round2.experimentIds, s1.promotedExperimentIds, 'round 2 roster equals promotion list');
assertEqual(round2.trainedTo, 60, 'round 2 target step');

const r2Raw = {
  10: folds(9, 700),
  2: folds(8, 900),
  3: folds(7, 600),
};
for (const id of round2.experimentIds) {
  tournament.submitAttempt({
    experimentId: id,
    folds: r2Raw[id],
    checkpoint: checkpoint(id, 60, 'r2'),
    continuedFrom: ref(r1Commands.get(id)),
  });
}

const s2 = tournament.settleRound();
const r2Check = crossCheck(s2, r2Raw);
assertEqual(s2.promotedExperimentIds, r2Check.promoted, 'round 2 promotion list');
assertEqual(
  s2.continuation.commands.map((c) => [c.experimentId, c.additionalSteps]),
  s2.promotedExperimentIds.map((id) => [id, 120]),
  'round 2 continuation deltas',
);

// ---- Round 3: final stage ---------------------------------------------------
const r2Commands = new Map(s2.continuation.commands.map((c) => [c.experimentId, c]));
const round3 = tournament.openRound({ folds: FOLDS });
assertEqual(round3.trainedTo, 180, 'round 3 target step');

const r3Raw = {
  2: folds(8, 960),
};
for (const id of round3.experimentIds) {
  tournament.submitAttempt({
    experimentId: id,
    folds: r3Raw[id],
    checkpoint: checkpoint(id, 180, 'r3'),
    continuedFrom: ref(r2Commands.get(id)),
  });
}

const s3 = tournament.settleRound();
assertEqual(s3.promotedExperimentIds, ['2'], 'round 3 winner');
assertEqual(s3.continuation, null, 'no continuation after final stage');
assertEqual(tournament.phase, 'complete', 'tournament phase');

// ---- Continuation-start cross-check ----------------------------------------
// Every continuation command must start exactly at the promoted attempt's
// checkpoint step and add only the remaining steps. Recompute from the
// *previous* settlement rather than trusting the command itself.
for (const settlement of [s1, s2]) {
  const nextStage = settlement.trainedTo === 20 ? 60 : 180;
  for (const command of settlement.continuation.commands) {
    const promotedRow = settlement.ranking.find(
      (row) => row.experimentId === command.experimentId && row.promoted,
    );
    assertEqual(
      [command.checkpoint.path, command.checkpoint.step, command.additionalSteps],
      [promotedRow.checkpoint.path, promotedRow.checkpoint.step, nextStage - promotedRow.checkpoint.step],
      `round ${settlement.round} continuation start for ${command.experimentId}`,
    );
  }
}

// ---- Digest verification ----------------------------------------------------
assertEqual(
  [s1, s2, s3].every((settlement) => verifySettlementDigest(settlement)),
  true,
  'all settlement digests verify',
);

// ---- Independent audit of every settlement ---------------------------------
for (const settlement of [s1, s2, s3]) {
  const { ok, findings } = auditSettlement(settlement);
  const failed = findings.filter((finding) => !finding.ok);
  assertEqual(ok, true, `round ${settlement.round} independent audit`);
  if (!ok) console.error(failed);
}

// ---- Late submission must not participate ----------------------------------
let lateRejected = null;
try {
  tournament.submitAttempt({
    experimentId: '2',
    folds: folds(10, 1000),
    checkpoint: checkpoint('2', 180, 'late'),
  });
} catch (error) {
  lateRejected = error.code;
}
assertEqual(lateRejected, 'ROUND_CLOSED', 'late submission after final settlement');

console.log(
  process.exitCode === 1
    ? '\nDEMO FAILED: one or more cross-checks disagreed.'
    : '\nDEMO PASSED: all cross-checks agreed.',
);
