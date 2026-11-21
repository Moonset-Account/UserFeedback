/**
 * Independent audit of a settlement.
 *
 * This module deliberately shares no ranking code with the coordinator: it
 * re-derives the promotion list and continuation start from the per-fold
 * counts carried in the settlement, using exact integer arithmetic, and then
 * compares the result with what the coordinator reported.
 *
 * Anyone can use it to check "两折大小相差十倍时，晋级名单和实际续训起点都应
 * 可以独立核对" without trusting the coordinator's internals.
 */
import { CoordinatorError } from './errors.js';

function bigintGcd(a, b) {
  let x = a < 0n ? -a : a;
  let y = b < 0n ? -b : b;
  while (y !== 0n) {
    [x, y] = [y, x % y];
  }
  return x;
}

function reduce(correct, samples) {
  const g = bigintGcd(BigInt(correct), BigInt(samples));
  return `${BigInt(correct) / g}/${BigInt(samples) / g}`;
}

function idCompare(a, b) {
  const na = /^\d+$/.test(a) ? BigInt(a) : null;
  const nb = /^\d+$/.test(b) ? BigInt(b) : null;
  if (na !== null && nb !== null && na !== nb) return na < nb ? -1 : 1;
  if (na !== null && nb === null) return -1;
  if (na === null && nb !== null) return 1;
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * @typedef {object} AuditFinding
 * @property {string} check   what was checked
 * @property {boolean} ok
 * @property {unknown} expected
 * @property {unknown} actual
 */

/**
 * @param {object} settlement the frozen settlement returned by settleRound()
 * @returns {{ ok: boolean, findings: AuditFinding[] }}
 */
export function auditSettlement(settlement) {
  const findings = [];
  const push = (check, expected, actual) => {
    findings.push({
      check,
      ok: JSON.stringify(expected) === JSON.stringify(actual),
      expected,
      actual,
    });
  };

  if (settlement === null || typeof settlement !== 'object') {
    throw new CoordinatorError('INVALID_SETTLEMENT', 'settlement must be an object');
  }

  const ranked = settlement.ranking.filter((row) => row.rank !== null);

  // 1. Every ranked row's declared score must equal its own fold counts.
  const recomputed = ranked.map((row) => {
    const correct = row.folds.reduce((sum, fold) => sum + fold.correct, 0);
    const samples = row.folds.reduce((sum, fold) => sum + fold.samples, 0);
    return { ...row, recomputedCorrect: correct, recomputedSamples: samples };
  });
  push(
    'each score equals sum(correct)/sum(samples) over its folds',
    ranked.map((row) => row.scoreRatio),
    recomputed.map((row) => reduce(row.recomputedCorrect, row.recomputedSamples)),
  );

  // 2. Ranking order is exact-score descending, then id ascending.
  const sorted = [...recomputed].sort((x, y) => {
    const left = BigInt(x.recomputedCorrect) * BigInt(y.recomputedSamples);
    const right = BigInt(y.recomputedCorrect) * BigInt(x.recomputedSamples);
    if (left !== right) return left > right ? -1 : 1;
    return idCompare(x.experimentId, y.experimentId);
  });
  push(
    'ranking order',
    sorted.map((row) => row.experimentId),
    ranked.map((row) => row.experimentId),
  );

  // 3. Retention quota is max(1, floor(n/2)).
  const expectedRetained = ranked.length === 0 ? 0 : Math.max(1, Math.floor(ranked.length / 2));
  push('retention quota max(1, floor(n/2))', expectedRetained, settlement.retainedCount);

  // 4. The promotion list is the first `retainedCount` ranked experiments.
  push(
    'promotion list is the top retainedCount rows',
    sorted.slice(0, expectedRetained).map((row) => row.experimentId),
    settlement.promotedExperimentIds,
  );

  // 5. Each promoted row is flagged promoted, and no other row is.
  push(
    'promoted flags',
    sorted.slice(0, expectedRetained).map((row) => row.experimentId),
    ranked.filter((row) => row.promoted).map((row) => row.experimentId),
  );

  // 6. Continuation: one command per promoted experiment, in promotion order,
  //    starting exactly at the promoted checkpoint step and adding only the
  //    remaining steps to the next target.
  if (settlement.continuation === null) {
    push('no continuation on the final stage', null, settlement.continuation);
  } else {
    const target = settlement.continuation.targetStep;
    const promotedRows = sorted.slice(0, expectedRetained);
    push(
      'continuation commands match promotion order',
      promotedRows.map((row) => row.experimentId),
      settlement.continuation.commands.map((command) => command.experimentId),
    );
    push(
      'each command starts at the promoted checkpoint and adds only the remainder',
      promotedRows.map((row) => ({
        experimentId: row.experimentId,
        startStep: row.checkpoint.step,
        path: row.checkpoint.path,
        digest: row.checkpoint.digest,
        additionalSteps: target - row.checkpoint.step,
      })),
      settlement.continuation.commands.map((command) => ({
        experimentId: command.experimentId,
        startStep: command.checkpoint.step,
        path: command.checkpoint.path,
        digest: command.checkpoint.digest,
        additionalSteps: command.additionalSteps,
      })),
    );
    push(
      'every continuation reaches the next stage exactly',
      promotedRows.map(() => target),
      settlement.continuation.commands.map(
        (command) => command.checkpoint.step + command.additionalSteps,
      ),
    );
    push(
      'continuation commands carry no absolute or duplicated step target',
      promotedRows.map(() => true),
      settlement.continuation.commands.map(
        (command) =>
          !('targetStep' in command) && !('startStep' in command) && !('fromStep' in command),
      ),
    );
  }

  return { ok: findings.every((finding) => finding.ok), findings };
}
