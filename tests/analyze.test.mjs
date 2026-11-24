/**
 * Tests for the convergence analysis. Covers the three failure kinds, mixed
 * failures, the "write back to original" distinction, and a randomised
 * cross-check against a naive permutation-enumerating reference.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  analyze,
  buildComponents,
  canonicalJson,
  defineProgram,
  replayEvidence,
  verifyEvidence,
} from '../src/index.mjs';
import { naiveAnalyze } from './reference.mjs';

const set = (field, value) => ({ kind: 'set', field, value });
const eq = (field, value) => ({ kind: 'eq', field, value });
const missing = (field) => ({ kind: 'missing', field });
const del = (field) => ({ kind: 'delete', field });

test('admits a program whose every order converges to the same accepted object', () => {
  const program = defineProgram({
    domain: { status: ['new', 'approved'], owner: ['none', 'alice'] },
    hooks: [
      { id: 'approve', when: eq('status', 'new'), action: set('status', 'approved') },
      { id: 'assign', when: eq('status', 'approved'), action: set('owner', 'alice') },
    ],
    accept: { kind: 'all', of: [eq('status', 'approved'), eq('owner', 'alice')] },
  });
  const result = analyze(program, { status: 'new', owner: 'none' });
  assert.equal(result.verdict, 'admissible');
  assert.equal(result.admissible, true);
  assert.equal(result.admission, 'accepted');
  assert.equal(result.allRunsTerminate, true);
  assert.equal(result.uniformTermination, true);
  assert.deepEqual(result.findings, []);
  assert.deepEqual(result.terminationOutcomes.map((entry) => entry.object), [
    { owner: 'alice', status: 'approved' },
  ]);
  for (const witness of result.witnesses) {
    assert.equal(replayEvidence(program, witness).ok, true);
  }
});

test('a program already stable at the initial object is admissible in one stable round', () => {
  const program = defineProgram({
    domain: { x: ['a'] },
    hooks: [{ id: 'keep', when: eq('x', 'a'), action: set('x', 'a') }],
    accept: eq('x', 'a'),
  });
  const result = analyze(program, { x: 'a' });
  assert.equal(result.verdict, 'admissible');
  const witness = result.witnesses[0];
  assert.deepEqual(witness.path, []);
  assert.deepEqual(witness.stableRound, ['keep']);
});

test('reports inconsistent results when different orders settle on different objects', () => {
  const program = defineProgram({
    domain: { x: ['a', 'b', 'c'] },
    hooks: [
      { id: 'toB', when: eq('x', 'a'), action: set('x', 'b') },
      { id: 'toC', when: eq('x', 'a'), action: set('x', 'c') },
    ],
    accept: eq('x', 'b'),
  });
  const result = analyze(program, { x: 'a' });
  assert.equal(result.verdict, 'inconsistent-results');
  assert.equal(result.allRunsTerminate, true);
  assert.equal(result.uniformTermination, false);
  const objects = result.terminationOutcomes
    .map((entry) => JSON.stringify(entry.object))
    .sort();
  assert.deepEqual(objects, [JSON.stringify({ x: 'b' }), JSON.stringify({ x: 'c' })]);
  const decisions = new Set(result.terminationOutcomes.map((entry) => entry.decision));
  assert.deepEqual([...decisions].sort(), ['accepted', 'rejected']);

  // Each ending has its own replayable evidence.
  assert.equal(result.findings.length, 1);
  assert.equal(result.findings[0].kind, 'inconsistent-results');
  assert.equal(result.findings[0].witnesses.length, 2);
  const reproduced = result.findings[0].witnesses.map((witness) => {
    const replay = verifyEvidence(
      {
        domain: { x: ['a', 'b', 'c'] },
        hooks: [
          { id: 'toB', when: eq('x', 'a'), action: set('x', 'b') },
          { id: 'toC', when: eq('x', 'a'), action: set('x', 'c') },
        ],
        accept: eq('x', 'b'),
      },
      JSON.parse(JSON.stringify(witness)),
    );
    assert.equal(replay.ok, true, JSON.stringify(replay.failures));
    return replay.reproduced;
  });
  assert.deepEqual(
    reproduced.map((entry) => `${entry.decision}:${JSON.stringify(entry.object)}`).sort(),
    ['accepted:{"x":"b"}', 'rejected:{"x":"c"}'],
  );
});

test('two accepted but different final objects are inconsistent results', () => {
  // Both endings are accepted, but they are different objects, so the caller
  // would not know which object is admitted. This is still inconsistent.
  const program = defineProgram({
    domain: { x: ['a', 'b', 'c'], ok: ['yes'] },
    hooks: [
      { id: 'toB', when: eq('x', 'a'), action: set('x', 'b') },
      { id: 'toC', when: eq('x', 'a'), action: set('x', 'c') },
    ],
    accept: { kind: 'any', of: [eq('x', 'b'), eq('x', 'c')] },
  });
  const result = analyze(program, { x: 'a' });
  assert.equal(result.verdict, 'inconsistent-results');
  assert.equal(result.uniformTermination, false);
  assert.equal(result.terminationOutcomes.length, 2);
  assert.deepEqual(
    result.terminationOutcomes.map((entry) => entry.decision),
    ['accepted', 'accepted'],
  );
  assert.deepEqual(
    result.terminationOutcomes.map((entry) => entry.object),
    [{ x: 'b' }, { x: 'c' }],
  );
});

test('missing fields in the initial object are first-class states', () => {
  const program = defineProgram({
    domain: { x: ['a', 'b'], y: ['on'] },
    hooks: [
      { id: 'fill', when: missing('y'), action: set('y', 'on') },
      { id: 'toB', when: { kind: 'all', of: [eq('y', 'on'), eq('x', 'a')] }, action: set('x', 'b') },
    ],
    accept: { kind: 'all', of: [eq('x', 'b'), eq('y', 'on')] },
  });
  const result = analyze(program, { x: 'a' });
  assert.equal(result.verdict, 'admissible');
  assert.deepEqual(result.terminationOutcomes[0].object, { x: 'b', y: 'on' });
});

test('reports a stable rejection when every order converges to the same rejected object', () => {
  const program = defineProgram({
    domain: { x: ['a', 'b'] },
    hooks: [{ id: 'toB', when: eq('x', 'a'), action: set('x', 'b') }],
    accept: eq('x', 'a'),
  });
  const result = analyze(program, { x: 'a' });
  assert.equal(result.verdict, 'stable-rejection');
  assert.equal(result.admissible, false);
  assert.equal(result.allRunsTerminate, true);
  assert.equal(result.uniformTermination, true);
  assert.deepEqual(result.terminationOutcomes.map((entry) => entry.object), [{ x: 'b' }]);
  assert.deepEqual(result.findings.map((finding) => finding.kind), ['stable-rejection']);
  assert.equal(replayEvidence(program, result.findings[0].witnesses[0]).ok, true);
});

test('reports nontermination when some order never reaches an unchanged round', () => {
  const program = defineProgram({
    domain: { x: ['a', 'b'] },
    hooks: [
      { id: 'toB', when: eq('x', 'a'), action: set('x', 'b') },
      { id: 'toA', when: eq('x', 'b'), action: set('x', 'a') },
    ],
    accept: eq('x', 'a'),
  });
  const result = analyze(program, { x: 'a' });
  assert.equal(result.verdict, 'nontermination');
  assert.equal(result.allRunsTerminate, false);
  assert.equal(result.nonterminationWitnesses.length, 1);
  assert.equal(result.findings[0].kind, 'nontermination');

  const witness = result.nonterminationWitnesses[0];
  const replay = replayEvidence(program, witness);
  assert.equal(replay.ok, true, JSON.stringify(replay.failures));
  assert.equal(replay.reproduced.closes, true);
  assert.deepEqual(replay.reproduced.startObject, replay.reproduced.endObject);
  assert.deepEqual(replay.reproduced.startPending, replay.reproduced.endPending);
});

test('a round that restores the original value is not a terminating round', () => {
  const program = defineProgram({
    domain: { x: ['a', 'b'] },
    hooks: [
      { id: 'up', when: eq('x', 'a'), action: set('x', 'b') },
      { id: 'down', when: eq('x', 'b'), action: set('x', 'a') },
    ],
    accept: eq('x', 'a'),
  });
  const result = analyze(program, { x: 'a' });
  // Object returns to {x:'a'} after every round, but every round writes twice,
  // so no round is stable and no run ever converges.
  assert.equal(result.verdict, 'nontermination');
  assert.equal(result.terminationOutcomes.length, 0);

  // Contrast: a single sample with a genuinely stable round is distinguishable.
  const stableProgram = defineProgram({
    domain: { x: ['a', 'b'] },
    hooks: [{ id: 'up', when: eq('x', 'a'), action: set('x', 'b') }],
    accept: eq('x', 'b'),
  });
  const stable = analyze(stableProgram, { x: 'a' });
  assert.equal(stable.verdict, 'admissible');
  assert.equal(stable.terminationOutcomes.length, 1);
});

test('separate evidence is produced for each failure kind present at the same time', () => {
  // From 'a': taking A first settles on b (accepted); taking E first settles on
  // e (rejected); taking B first reaches the c <-> d cycle that never stops.
  const program = defineProgram({
    domain: { x: ['a', 'b', 'c', 'd', 'e'] },
    hooks: [
      { id: 'A', when: eq('x', 'a'), action: set('x', 'b') },
      { id: 'B', when: eq('x', 'a'), action: set('x', 'c') },
      { id: 'C', when: eq('x', 'c'), action: set('x', 'd') },
      { id: 'D', when: eq('x', 'd'), action: set('x', 'c') },
      { id: 'E', when: eq('x', 'a'), action: set('x', 'e') },
    ],
    accept: eq('x', 'b'),
  });
  const result = analyze(program, { x: 'a' });
  assert.equal(result.verdict, 'multiple-failures');
  assert.equal(result.allRunsTerminate, false);
  const kinds = result.findings.map((finding) => finding.kind).sort();
  assert.deepEqual(kinds, ['inconsistent-results', 'nontermination']);
  for (const finding of result.findings) {
    assert.ok(finding.witnesses.length >= 1);
    for (const witness of finding.witnesses) {
      const replay = replayEvidence(program, witness);
      assert.equal(replay.ok, true, JSON.stringify(replay.failures));
    }
  }
  const outcomes = result.terminationOutcomes
    .map((entry) => `${entry.decision}:${JSON.stringify(entry.object)}`)
    .sort();
  assert.deepEqual(outcomes, ['accepted:{"x":"b"}', 'rejected:{"x":"e"}']);
});

test('stable rejection and nontermination are reported as separate findings with separate evidence', () => {
  // 'toC' settles on a rejected terminal c; the loop1/loop2 pair never converges.
  const program = defineProgram({
    domain: { x: ['a', 'b', 'c'] },
    hooks: [
      { id: 'loop1', when: eq('x', 'a'), action: set('x', 'b') },
      { id: 'loop2', when: eq('x', 'b'), action: set('x', 'a') },
      { id: 'toC', when: eq('x', 'a'), action: set('x', 'c') },
    ],
    accept: eq('x', 'nope'),
  });
  const result = analyze(program, { x: 'a' });
  assert.equal(result.verdict, 'multiple-failures');
  const kinds = result.findings.map((finding) => finding.kind).sort();
  assert.deepEqual(kinds, ['nontermination', 'stable-rejection']);
  const nontermination = result.findings.find((finding) => finding.kind === 'nontermination');
  const rejection = result.findings.find((finding) => finding.kind === 'stable-rejection');
  assert.equal(nontermination.witnesses.length, 1);
  assert.equal(rejection.witnesses.length, 1);
  assert.equal(nontermination.witnesses[0].kind, 'nontermination');
  assert.equal(rejection.witnesses[0].kind, 'termination');
  for (const witness of [...nontermination.witnesses, ...rejection.witnesses]) {
    assert.equal(replayEvidence(program, witness).ok, true);
  }
  assert.deepEqual(rejection.outcomes, [{ decision: 'rejected', object: { x: 'c' } }]);
});

test('a program with no hooks is rejected when accept fails on the initial object', () => {
  const program = defineProgram({
    domain: { x: ['a'] },
    hooks: [],
    accept: eq('x', 'b'),
  });
  const result = analyze(program, { x: 'a' });
  assert.equal(result.verdict, 'stable-rejection');
  assert.deepEqual(result.terminationOutcomes[0].object, { x: 'a' });
  assert.equal(result.terminationOutcomes[0].decision, 'rejected');
});

test('an empty hook set is trivially admissible after the empty stable round', () => {
  const program = defineProgram({
    domain: { x: ['a'] },
    hooks: [],
    accept: eq('x', 'a'),
  });
  const result = analyze(program, { x: 'a' });
  assert.equal(result.verdict, 'admissible');
  assert.deepEqual(result.terminationOutcomes[0].object, { x: 'a' });
  assert.equal(replayEvidence(program, result.witnesses[0]).ok, true);
});

test('independent hooks do not multiply stored states by their permutations', () => {
  const fields = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'];
  const program = defineProgram({
    domain: Object.fromEntries(fields.map((field) => [field, ['0', '1']])),
    hooks: fields.map((field) => ({
      id: `set_${field}`,
      when: eq(field, '0'),
      action: set(field, '1'),
    })),
    accept: { kind: 'all', of: fields.map((field) => eq(field, '1')) },
  });
  const result = analyze(program, Object.fromEntries(fields.map((field) => [field, '0'])));
  assert.equal(result.verdict, 'admissible');
  assert.equal(result.stats.allPermutations, 40320);
  // The step-level search stores at most one state per (object, pending set),
  // so with 2^8 objects the state count stays far below 8!.
  assert.ok(
    result.stats.reachableStates < 2000,
    `expected far fewer than 40320 states, got ${result.stats.reachableStates}`,
  );
  assert.deepEqual(
    buildComponents(program),
    fields.map((field) => [`set_${field}`]),
  );
});

test('analysis limit is enforced instead of looping forever', () => {
  const fields = ['a', 'b', 'c', 'd'];
  const program = defineProgram({
    domain: Object.fromEntries(fields.map((field) => [field, ['0', '1']])),
    hooks: fields.map((field) => ({
      id: `set_${field}`,
      when: eq(field, '0'),
      action: set(field, '1'),
    })),
    accept: { kind: 'all', of: fields.map((field) => eq(field, '1')) },
  });
  assert.throws(
    () => analyze(program, Object.fromEntries(fields.map((f) => [f, '0'])), { maxStates: 3 }),
    (error) => error.code === 'ANALYSIS_LIMIT',
  );
});

test('random small programs agree with the naive permutation reference', () => {
  let seed = 0x5eed;
  const random = () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 0x100000000;
  };
  const pick = (items) => items[Math.floor(random() * items.length)];

  for (let iteration = 0; iteration < 120; iteration += 1) {
    const fieldNames = ['p', 'q', 'r'];
    const domain = {
      p: ['0', '1'],
      q: ['0', '1', 'x'],
      r: ['0', '1'],
    };
    const hookCount = 1 + Math.floor(random() * 3);
    const hooks = Array.from({ length: hookCount }, (_, index) => {
      const field = pick(fieldNames);
      const action =
        random() < 0.6
          ? set(field, pick(domain[field]))
          : del(field);
      const roll = random();
      const when =
        roll < 0.45
          ? eq(field, pick(domain[field]))
          : roll < 0.7
            ? missing(field)
            : { kind: 'in', field, values: [pick(domain[field])] };
      return { id: `h${index}`, when, action };
    });
    const acceptRoll = random();
    const accept =
      acceptRoll < 0.4
        ? eq('p', pick(domain.p))
        : acceptRoll < 0.7
          ? { kind: 'all', of: [eq('p', '1'), eq('q', '1')] }
          : { kind: 'any', of: [eq('p', '1'), missing('r')] };
    const spec = { domain, hooks, accept };
    const program = defineProgram(spec);
    const initial = {};
    for (const field of fieldNames) {
      if (random() < 0.5) initial[field] = pick(domain[field]);
    }
    const actual = analyze(program, initial);
    const expected = naiveAnalyze(program, structuredClone(initial));

    const actualOutcomes = actual.terminationOutcomes
      .map((entry) => `${entry.decision}|${canonicalJson(entry.object)}`)
      .sort();
    const expectedOutcomes = expected.terminalObjects
      .map((entry) => `${entry.decision}|${canonicalJson(entry.object)}`)
      .sort();
    assert.deepEqual(
      actualOutcomes,
      expectedOutcomes,
      `terminal outcomes mismatch for ${JSON.stringify(spec)} from ${JSON.stringify(initial)}`,
    );
    assert.equal(
      actual.allRunsTerminate,
      !expected.hasCycle,
      `cycle mismatch for ${JSON.stringify(spec)} from ${JSON.stringify(initial)}`,
    );
    if (expected.hasCycle) {
      assert.notEqual(actual.verdict, 'admissible');
    } else {
      assert.notEqual(actual.verdict, 'nontermination');
    }
    for (const witness of actual.witnesses) {
      const replay = replayEvidence(program, witness);
      assert.equal(replay.ok, true, JSON.stringify(replay.failures));
    }
  }
});
