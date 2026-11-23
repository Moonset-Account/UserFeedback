import { test } from 'node:test';
import assert from 'node:assert/strict';
import { defineSchema } from '../src/schema.js';
import { planEntityFetch } from '../src/planner.js';

const userEntity = defineSchema({
  entities: {
    User: {
      key: ['id'],
      services: {
        users: { key: ['id'], provides: ['id'], cost: 1, batchLimit: 100 },
        profiles: { key: ['id'], provides: ['name', 'region'], cost: 4, batchLimit: 2 },
        contacts: { key: ['id', 'region'], provides: ['email'], cost: 2, batchLimit: 50 },
      },
    },
  },
}).entities.User;

test('plans a terminating key-fetch sequence chained through a third service', () => {
  // email needs (id, region); region only comes from profiles.
  const plan = planEntityFetch(userEntity, {
    available: ['id'],
    needed: ['email'],
    count: 3,
  });
  assert.equal(plan.ok, true);
  assert.deepEqual(
    plan.stages.map((s) => s.service),
    ['profiles', 'contacts'],
  );
  // profiles: ceil(3/2)*4 = 8; contacts: ceil(3/50)*2 = 2.
  assert.equal(plan.totalCost, 10);
});

test('plan with all fields already available costs nothing and calls nothing', () => {
  const plan = planEntityFetch(userEntity, {
    available: ['id', 'email'],
    needed: ['email'],
    count: 5,
  });
  assert.deepEqual(plan, { ok: true, stages: [], totalCost: 0 });
});

test('picks the cheapest feasible plan given call costs and batch limits', () => {
  const entity = defineSchema({
    entities: {
      Doc: {
        key: ['id'],
        services: {
          bulk: { key: ['id'], provides: ['title'], cost: 5, batchLimit: 10 },
          retail: { key: ['id'], provides: ['title'], cost: 2, batchLimit: 1 },
        },
      },
    },
  }).entities.Doc;

  // count=1: retail 1*2=2 beats bulk 1*5=5.
  const small = planEntityFetch(entity, { available: ['id'], needed: ['title'], count: 1 });
  assert.equal(small.ok, true);
  assert.deepEqual(small.stages.map((s) => s.service), ['retail']);
  assert.equal(small.totalCost, 2);

  // count=12: bulk ceil(12/10)*5=10 beats retail 12*2=24.
  const large = planEntityFetch(entity, { available: ['id'], needed: ['title'], count: 12 });
  assert.equal(large.ok, true);
  assert.deepEqual(large.stages.map((s) => s.service), ['bulk']);
  assert.equal(large.totalCost, 10);
});

test('reports the blocked field chain for mutual dependency with no starting key', () => {
  const entity = defineSchema({
    entities: {
      Knot: {
        key: ['k'],
        services: {
          sa: { key: ['y'], provides: ['x'], cost: 1, batchLimit: 10 },
          sb: { key: ['x'], provides: ['y'], cost: 1, batchLimit: 10 },
        },
      },
    },
  }).entities.Knot;

  const plan = planEntityFetch(entity, { available: [], needed: ['x'], count: 1 });
  assert.equal(plan.ok, false);
  assert.deepEqual(plan.blocked.unreachableNeeded, ['x']);
  const { chain } = plan.blocked;
  assert.ok(chain.length >= 2, 'chain should name the mutually dependent fields');
  assert.equal(chain[0], chain[chain.length - 1], 'chain must be a closed cycle');
  assert.ok(chain.includes('x') && chain.includes('y'));
});

test('reports missing root keys when no service provides a required key field', () => {
  const entity = defineSchema({
    entities: {
      Vault: {
        key: ['k'],
        services: {
          sv: { key: ['secret'], provides: ['z'], cost: 1, batchLimit: 10 },
        },
      },
    },
  }).entities.Vault;

  const plan = planEntityFetch(entity, { available: [], needed: ['z'], count: 1 });
  assert.equal(plan.ok, false);
  assert.deepEqual(plan.blocked.unreachableNeeded, ['z']);
  assert.deepEqual(plan.blocked.missing, ['secret']);
  assert.deepEqual(plan.blocked.chain, []);
});
