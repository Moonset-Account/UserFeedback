/**
 * Tests for program/domain validation and the two entry points that execute the
 * hook interpreter directly (runRound / runRounds).
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  AdmissionError,
  MAX_HOOKS,
  defineProgram,
  runRound,
  runRounds,
} from '../src/index.mjs';

const basicSpec = () => ({
  domain: {
    status: ['new', 'approved', 'denied'],
    owner: ['none', 'alice'],
    flag: ['on', 'off'],
  },
  hooks: [
    {
      id: 'approve',
      when: { kind: 'eq', field: 'status', value: 'new' },
      action: { kind: 'set', field: 'status', value: 'approved' },
    },
    {
      id: 'assign',
      when: { kind: 'eq', field: 'status', value: 'approved' },
      action: { kind: 'set', field: 'owner', value: 'alice' },
    },
  ],
  accept: {
    kind: 'all',
    of: [
      { kind: 'eq', field: 'status', value: 'approved' },
      { kind: 'eq', field: 'owner', value: 'alice' },
    ],
  },
});

test('defines a valid program and normalises hook ids', () => {
  const program = defineProgram(basicSpec());
  assert.deepEqual(program.hookIds, ['approve', 'assign']);
  const implicit = defineProgram({
    domain: { x: ['a'] },
    hooks: [{ when: { kind: 'eq', field: 'x', value: 'a' }, action: { kind: 'delete', field: 'x' } }],
    accept: { kind: 'missing', field: 'x' },
  });
  assert.deepEqual(implicit.hookIds, ['h0']);
});

test('rejects more than the maximum number of hooks', () => {
  const domain = { f: ['0', '1'] };
  const hooks = Array.from({ length: MAX_HOOKS + 1 }, (_, index) => ({
    id: `h${index}`,
    when: { kind: 'eq', field: 'f', value: '0' },
    action: { kind: 'set', field: 'f', value: '1' },
  }));
  assert.throws(
    () => defineProgram({ domain, hooks, accept: { kind: 'eq', field: 'f', value: '1' } }),
    (error) => error instanceof AdmissionError && error.code === 'TOO_MANY_HOOKS',
  );
  assert.doesNotThrow(() =>
    defineProgram({
      domain,
      hooks: hooks.slice(0, MAX_HOOKS),
      accept: { kind: 'eq', field: 'f', value: '1' },
    }),
  );
});

test('rejects set values outside the declared domain, unknown fields, duplicates and malformed conditions', () => {
  const base = { domain: { x: ['a', 'b'] }, accept: { kind: 'eq', field: 'x', value: 'a' } };
  assert.throws(
    () =>
      defineProgram({
        ...base,
        hooks: [{ when: { kind: 'eq', field: 'x', value: 'a' }, action: { kind: 'set', field: 'x', value: 'zzz' } }],
      }),
    (error) => error.code === 'VALUE_OUT_OF_DOMAIN',
  );
  assert.throws(
    () =>
      defineProgram({
        ...base,
        hooks: [{ when: { kind: 'eq', field: 'nope', value: 'a' }, action: { kind: 'delete', field: 'x' } }],
      }),
    (error) => error.code === 'UNKNOWN_FIELD',
  );
  assert.throws(
    () =>
      defineProgram({
        ...base,
        hooks: [
          { id: 'same', when: { kind: 'eq', field: 'x', value: 'a' }, action: { kind: 'delete', field: 'x' } },
          { id: 'same', when: { kind: 'eq', field: 'x', value: 'a' }, action: { kind: 'delete', field: 'x' } },
        ],
      }),
    (error) => error.code === 'DUPLICATE_HOOK_ID',
  );
  assert.throws(
    () =>
      defineProgram({
        ...base,
        hooks: [{ when: { kind: 'greater', field: 'x', value: 1 }, action: { kind: 'delete', field: 'x' } }],
      }),
    (error) => error.code === 'INVALID_CONDITION',
  );
  assert.throws(
    () => defineProgram({ domain: { x: ['a', 'a'] }, hooks: [], accept: { kind: 'eq', field: 'x', value: 'a' } }),
    (error) => error.code === 'DUPLICATE_DOMAIN_VALUE',
  );
  assert.throws(
    () => defineProgram({ domain: { x: ['a'] }, hooks: [] }),
    (error) => error.code === 'PROGRAM_INVALID',
  );
});

test('rejects an initial object with undeclared fields or out-of-domain values', () => {
  const program = defineProgram(basicSpec());
  for (const bad of [{ ghost: 'x' }, { status: 'archived' }, null, [], 'string']) {
    assert.throws(
      () => runRound(program, bad, ['approve', 'assign']),
      (error) => error.code === 'INITIAL_OBJECT_INVALID',
    );
  }
  // Missing fields are legal.
  assert.doesNotThrow(() => runRound(program, {}, ['approve', 'assign']));
});

test('runRound executes each hook once in the given order against the evolving object', () => {
  const program = defineProgram(basicSpec());
  const result = runRound(program, { status: 'new' }, ['assign', 'approve']);
  assert.equal(result.changed, true);
  assert.equal(result.effectiveWrites, 1);
  // assign ran before approve matched, so owner stays absent.
  assert.deepEqual(result.object, { status: 'approved' });
  assert.deepEqual(
    result.trace.map((step) => [step.hook, step.matched, step.changed]),
    [
      ['assign', false, false],
      ['approve', true, true],
    ],
  );
  // Trace carries the within-round pending sets, used as cycle evidence later.
  assert.deepEqual(result.trace[0].pendingBefore, ['assign', 'approve']);
  assert.deepEqual(result.trace[0].pendingAfter, ['approve']);
  assert.deepEqual(result.trace[1].pendingAfter, []);
});

test('runRound rejects orders that are not permutations of all hooks', () => {
  const program = defineProgram(basicSpec());
  const badOrders = [
    ['approve'],
    ['approve', 'approve'],
    ['approve', 'ghost'],
    'approve,assign',
  ];
  for (const order of badOrders) {
    assert.throws(
      () => runRound(program, { status: 'new' }, order),
      (error) => error.code === 'INVALID_ORDER',
    );
  }
});

test('a round that writes values back to their originals still counts as changed', () => {
  const program = defineProgram({
    domain: { x: ['a', 'b'] },
    hooks: [
      { id: 'up', when: { kind: 'eq', field: 'x', value: 'a' }, action: { kind: 'set', field: 'x', value: 'b' } },
      { id: 'down', when: { kind: 'eq', field: 'x', value: 'b' }, action: { kind: 'set', field: 'x', value: 'a' } },
    ],
    accept: { kind: 'eq', field: 'x', value: 'a' },
  });
  const result = runRound(program, { x: 'a' }, ['up', 'down']);
  assert.deepEqual(result.object, { x: 'a' });
  assert.equal(result.changed, true);
  assert.equal(result.effectiveWrites, 2);

  const noop = defineProgram({
    domain: { x: ['a'] },
    hooks: [{ id: 'keep', when: { kind: 'eq', field: 'x', value: 'a' }, action: { kind: 'set', field: 'x', value: 'a' } }],
    accept: { kind: 'eq', field: 'x', value: 'a' },
  });
  const still = runRound(noop, { x: 'a' }, ['keep']);
  assert.deepEqual(still.object, { x: 'a' });
  assert.equal(still.changed, false);
  assert.equal(still.effectiveWrites, 0);
});

test('delete is only an effective write when the field is present', () => {
  const program = defineProgram({
    domain: { x: ['a'] },
    hooks: [
      { id: 'drop', when: { kind: 'eq', field: 'x', value: 'a' }, action: { kind: 'delete', field: 'x' } },
      { id: 'fill', when: { kind: 'missing', field: 'x' }, action: { kind: 'set', field: 'x', value: 'a' } },
    ],
    accept: { kind: 'missing', field: 'x' },
  });
  const dropped = runRound(program, { x: 'a' }, ['drop', 'fill']);
  assert.deepEqual(dropped.object, { x: 'a' });
  assert.equal(dropped.changed, true);
  const missing = runRound(program, {}, ['fill', 'drop']);
  assert.deepEqual(missing.object, {});
  assert.equal(missing.changed, true);
  const untouched = runRound(program, {}, ['drop', 'fill']);
  // drop is a no-op on a missing field; fill writes then nothing else runs.
  assert.equal(untouched.effectiveWrites, 1);
});

test('conditions support eq, missing, in and boolean composition', () => {
  const program = defineProgram({
    domain: { tier: ['gold', 'silver'], region: ['eu', 'us'], note: ['x'] },
    hooks: [
      {
        id: 'combo',
        when: {
          kind: 'all',
          of: [
            { kind: 'in', field: 'tier', values: ['gold', 'silver'] },
            { kind: 'any', of: [{ kind: 'eq', field: 'region', value: 'eu' }, { kind: 'missing', field: 'note' }] },
            { kind: 'not', of: { kind: 'eq', field: 'tier', value: 'silver' } },
          ],
        },
        action: { kind: 'set', field: 'note', value: 'x' },
      },
    ],
    accept: { kind: 'eq', field: 'note', value: 'x' },
  });
  assert.equal(runRound(program, { tier: 'gold', region: 'us' }, ['combo']).changed, true);
  assert.equal(runRound(program, { tier: 'gold', region: 'eu' }, ['combo']).changed, true);
  assert.equal(runRound(program, { tier: 'silver', region: 'us' }, ['combo']).changed, false);
});

test('runRounds chains rounds and reports each round result', () => {
  const program = defineProgram(basicSpec());
  const result = runRounds(program, { status: 'new' }, [
    ['approve', 'assign'],
    ['approve', 'assign'],
  ]);
  assert.equal(result.rounds.length, 2);
  assert.equal(result.rounds[0].changed, true);
  assert.equal(result.rounds[1].changed, false);
  assert.deepEqual(result.object, { status: 'approved', owner: 'alice' });
});

test('programs with structured JSON values compare structurally', () => {
  const program = defineProgram({
    domain: { cfg: [{ mode: 'off', level: 1 }, { mode: 'on', level: 2 }] },
    hooks: [
      {
        id: 'enable',
        when: { kind: 'eq', field: 'cfg', value: { level: 1, mode: 'off' } },
        action: { kind: 'set', field: 'cfg', value: { mode: 'on', level: 2 } },
      },
    ],
    accept: { kind: 'eq', field: 'cfg', value: { mode: 'on', level: 2 } },
  });
  const result = runRound(program, { cfg: { mode: 'off', level: 1 } }, ['enable']);
  assert.deepEqual(result.object, { cfg: { mode: 'on', level: 2 } });
  assert.equal(result.changed, true);
});
