import test from 'node:test';
import assert from 'node:assert/strict';

import { auditSettlement } from '../src/audit.js';
import { Tournament } from '../src/index.js';
import { runFirstRound, FOLDS, foldReport } from './helpers.mjs';

test('audit accepts a settlement produced from 10:1 folds', () => {
  const { settlement } = runFirstRound({
    experiments: ['a', 'b', 'c'],
    totals: { a: 1000, b: 900, c: 800 },
  });
  const { ok, findings } = auditSettlement(settlement);
  assert.equal(ok, true, JSON.stringify(findings.filter((f) => !f.ok), null, 2));
  assert.equal(findings.length, 9);
});

test('audit accepts a final-stage settlement with no continuation', () => {
  const tournament = new Tournament({ stages: [20] });
  tournament.openRound({ experiments: ['a', 'b'], folds: FOLDS });
  tournament.submitAttempt({
    experimentId: 'a',
    folds: foldReport(10, 900),
    checkpoint: { path: '/c/a', step: 20, digest: 'a' },
  });
  tournament.submitAttempt({
    experimentId: 'b',
    folds: foldReport(10, 800),
    checkpoint: { path: '/c/b', step: 20, digest: 'b' },
  });
  const settlement = tournament.settleRound();
  const { ok, findings } = auditSettlement(settlement);
  assert.equal(ok, true, JSON.stringify(findings.filter((f) => !f.ok), null, 2));
});

test('audit detects a tampered promotion list', () => {
  const { settlement } = runFirstRound({
    experiments: ['a', 'b', 'c', 'd'],
    totals: { a: 1000, b: 900, c: 800, d: 700 },
  });
  const tampered = { ...settlement, promotedExperimentIds: ['c', 'd'] };
  const { ok, findings } = auditSettlement(tampered);
  assert.equal(ok, false);
  assert.ok(findings.some((f) => f.check === 'promotion list is the top retainedCount rows' && !f.ok));
});

test('audit detects a doctored continuation start step', () => {
  const { settlement } = runFirstRound({
    experiments: ['a', 'b', 'c', 'd'],
    totals: { a: 1000, b: 900, c: 800, d: 700 },
  });
  const tampered = JSON.parse(JSON.stringify(settlement));
  // Claim the run resumed from step 15 and added 45 instead of 20 + 40.
  tampered.continuation.commands[0].checkpoint.step = 15;
  tampered.continuation.commands[0].additionalSteps = 45;
  const { ok, findings } = auditSettlement(tampered);
  assert.equal(ok, false);
  assert.ok(findings.some((f) => f.check.includes('starts at the promoted checkpoint') && !f.ok));
});

test('audit detects a doctored score row', () => {
  const { settlement } = runFirstRound({
    experiments: ['a', 'b', 'c'],
    totals: { a: 1000, b: 900, c: 800 },
  });
  const tampered = JSON.parse(JSON.stringify(settlement));
  tampered.ranking[0].scoreRatio = '1/1';
  const { ok, findings } = auditSettlement(tampered);
  assert.equal(ok, false);
});
