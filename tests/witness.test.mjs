/**
 * Tests for the evidence and independent replay entry points.
 *
 * These are the acceptance tests for "evidence can be reproduced from scratch by
 * another entry point": they both replay genuine evidence and tamper with it to
 * prove the verifier recomputes rather than trusts.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  AdmissionError,
  analyze,
  defineProgram,
  replayEvidence,
  verifyEvidence,
} from '../src/index.mjs';

const set = (field, value) => ({ kind: 'set', field, value });
const eq = (field, value) => ({ kind: 'eq', field, value });

const cycleSpec = () => ({
  domain: { x: ['a', 'b'] },
  hooks: [
    { id: 'toB', when: eq('x', 'a'), action: set('x', 'b') },
    { id: 'toA', when: eq('x', 'b'), action: set('x', 'a') },
  ],
  accept: eq('x', 'a'),
});

const divergentSpec = () => ({
  domain: { x: ['a', 'b', 'c'] },
  hooks: [
    { id: 'toB', when: eq('x', 'a'), action: set('x', 'b') },
    { id: 'toC', when: eq('x', 'a'), action: set('x', 'c') },
  ],
  accept: eq('x', 'b'),
});

const clone = (value) => JSON.parse(JSON.stringify(value));

test('valid cycle evidence replays and closes object plus pending set', () => {
  const program = defineProgram(cycleSpec());
  const result = analyze(program, { x: 'a' });
  const witness = result.nonterminationWitnesses[0];
  const replay = replayEvidence(program, clone(witness));
  assert.equal(replay.ok, true, JSON.stringify(replay.failures));
  assert.equal(replay.reproduced.closes, true);
  assert.deepEqual(replay.reproduced.startObject, replay.reproduced.endObject);
  assert.deepEqual(replay.reproduced.startPending, replay.reproduced.endPending);
  assert.equal(replay.reproduced.startChanged, replay.reproduced.endChanged);
});

test('cycle evidence with a broken object closure is rejected', () => {
  const program = defineProgram(cycleSpec());
  const witness = clone(analyze(program, { x: 'a' }).nonterminationWitnesses[0]);
  // Drop the closing step so the state no longer returns to the start.
  witness.cycle = witness.cycle.slice(0, -1);
  const replay = replayEvidence(program, witness);
  assert.equal(replay.ok, false);
  assert.ok(
    replay.failures.some((item) => item.code === 'CYCLE_OBJECT_NOT_CLOSED' ||
      item.code === 'INVALID_STEP'),
    JSON.stringify(replay.failures),
  );
});

test('cycle evidence that closes the object but not the pending set is rejected', () => {
  // A writes y, Bab/Bba toggle x across a round boundary. Starting after A has
  // run, the sequence Bab,Bba,Bab,Bba returns to the same object with the same
  // change flag but leaves a different pending set: A has not run yet in the
  // new round, so the repetition is not a legal cycle.
  const program = defineProgram({
    domain: { x: ['a', 'b'], y: ['0', '1'] },
    hooks: [
      { id: 'A', when: eq('y', '0'), action: set('y', '1') },
      { id: 'Bab', when: eq('x', 'a'), action: set('x', 'b') },
      { id: 'Bba', when: eq('x', 'b'), action: set('x', 'a') },
    ],
    accept: eq('y', '1'),
  });
  const witness = {
    version: 1,
    kind: 'nontermination',
    programSignature: analyze(program, { x: 'a', y: '0' }).witnesses[0].programSignature,
    programHookIds: ['A', 'Bab', 'Bba'],
    initialObject: { x: 'a', y: '0' },
    path: [],
    prefix: ['A'],
    cycle: ['Bab', 'Bba', 'Bab', 'Bba'],
  };
  const replay = replayEvidence(program, witness);
  assert.equal(replay.ok, false);
  assert.deepEqual(replay.reproduced.startObject, replay.reproduced.endObject);
  assert.equal(replay.reproduced.startChanged, replay.reproduced.endChanged);
  assert.deepEqual(replay.reproduced.startPending, ['Bab', 'Bba']);
  assert.deepEqual(replay.reproduced.endPending, ['A']);
  assert.ok(
    replay.failures.some((item) => item.code === 'CYCLE_PENDING_NOT_CLOSED'),
    JSON.stringify(replay.failures),
  );
  assert.ok(
    !replay.failures.some((item) => item.code === 'CYCLE_OBJECT_NOT_CLOSED'),
    JSON.stringify(replay.failures),
  );
});

test('cycle evidence that hides a terminating round is rejected', () => {
  const program = defineProgram(cycleSpec());
  const witness = clone(analyze(program, { x: 'a' }).nonterminationWitnesses[0]);
  witness.cycle = witness.cycle.slice(0, 1);
  const replay = replayEvidence(program, witness);
  assert.equal(replay.ok, false);
  assert.ok(replay.failures.length > 0);
});

test('termination evidence claiming a stable round that writes is rejected', () => {
  const program = defineProgram(divergentSpec());
  const witness = clone(analyze(program, { x: 'a' }).terminationOutcomes[0].witness);
  // Claim that the initial state is already stable.
  witness.path = [];
  witness.stableRound = ['toB', 'toC'];
  witness.expected = { decision: 'accepted', object: { x: 'a' } };
  const replay = replayEvidence(program, witness);
  assert.equal(replay.ok, false);
  assert.ok(
    replay.failures.some((item) => item.code === 'STABLE_ROUND_CHANGED'),
    JSON.stringify(replay.failures),
  );
});

test('evidence with a forged expected object or decision is rejected', () => {
  const program = defineProgram(divergentSpec());
  const accepted = analyze(program, { x: 'a' }).terminationOutcomes.find(
    (entry) => entry.decision === 'accepted',
  );
  const forgedObject = clone(accepted.witness);
  forgedObject.expected.object = { x: 'zzz' };
  const replayObject = replayEvidence(program, forgedObject);
  assert.equal(replayObject.ok, false);
  assert.ok(replayObject.failures.some((item) => item.code === 'OBJECT_MISMATCH'));

  const forgedDecision = clone(accepted.witness);
  forgedDecision.expected.decision = 'rejected';
  const replayDecision = replayEvidence(program, forgedDecision);
  assert.equal(replayDecision.ok, false);
  assert.ok(replayDecision.failures.some((item) => item.code === 'DECISION_MISMATCH'));
});

test('evidence for a different program is rejected', () => {
  const original = defineProgram(cycleSpec());
  const witness = clone(analyze(original, { x: 'a' }).nonterminationWitnesses[0]);
  const other = defineProgram({
    domain: { x: ['a', 'b'] },
    hooks: [
      { id: 'toB', when: eq('x', 'a'), action: set('x', 'b') },
      { id: 'toA', when: eq('x', 'b'), action: set('x', 'a') },
    ],
    accept: eq('x', 'b'),
  });
  const replay = replayEvidence(other, witness);
  assert.equal(replay.ok, false);
  assert.ok(replay.failures.some((item) => item.code === 'PROGRAM_MISMATCH'));
});

test('a path round that performs no write is not evidence of progress', () => {
  // At x='a' neither hook matches, so the round makes no write and a run would
  // have stopped there; using it as a "changed" path round must be rejected.
  const program = defineProgram({
    domain: { x: ['a', 'b'] },
    hooks: [
      { id: 'idle1', when: eq('x', 'b'), action: set('x', 'a') },
      { id: 'idle2', when: eq('x', 'b'), action: set('x', 'a') },
    ],
    accept: eq('x', 'a'),
  });
  const witness = {
    version: 1,
    kind: 'termination',
    programSignature: analyze(program, { x: 'a' }).witnesses[0].programSignature,
    programHookIds: ['idle1', 'idle2'],
    initialObject: { x: 'a' },
    path: [['idle1', 'idle2']],
    stableRound: ['idle1', 'idle2'],
    expected: { decision: 'accepted', object: { x: 'a' } },
  };
  const replay = replayEvidence(program, witness);
  assert.equal(replay.ok, false);
  assert.ok(
    replay.failures.some((item) => item.code === 'PATH_ROUND_TERMINATED'),
    JSON.stringify(replay.failures),
  );
});

test('replay rejects malformed evidence objects', () => {
  const program = defineProgram(cycleSpec());
  assert.throws(() => replayEvidence(program, null), (error) => error.code === 'INVALID_EVIDENCE');
  assert.throws(() => replayEvidence(program, { version: 99, kind: 'termination' }),
    (error) => error.code === 'INVALID_EVIDENCE');
  assert.throws(() => replayEvidence(program, { version: 1, kind: 'other' }),
    (error) => error.code === 'INVALID_EVIDENCE');
});

test('verifyEvidence rebuilds the program from a plain spec and replays', () => {
  const spec = cycleSpec();
  const program = defineProgram(spec);
  const witness = clone(analyze(program, { x: 'a' }).nonterminationWitnesses[0]);
  const replay = verifyEvidence(spec, witness);
  assert.equal(replay.ok, true, JSON.stringify(replay.failures));

  const tampered = { ...spec, accept: eq('x', 'b') };
  const rejected = verifyEvidence(tampered, witness);
  assert.equal(rejected.ok, false);
  assert.ok(rejected.failures.some((item) => item.code === 'PROGRAM_MISMATCH'));
});

test('every witness returned by analyze replays through verifyEvidence', () => {
  const specs = [cycleSpec(), divergentSpec()];
  for (const spec of specs) {
    const program = defineProgram(spec);
    const result = analyze(program, { x: 'a' });
    for (const witness of result.witnesses) {
      const replay = verifyEvidence(spec, clone(witness));
      assert.equal(replay.ok, true, JSON.stringify(replay.failures));
    }
  }
});
