import { test } from 'node:test';
import assert from 'node:assert/strict';
import { defineSchema } from '../src/schema.js';
import { executeQuery } from '../src/executor.js';

/**
 * Interleaving acceptance scenario.
 *
 * User 7 appears twice in one response:
 *  - inside `friends`: a nullable list with nullable items, selecting a
 *    nullable `email`;
 *  - inside `buddy.cohorts`: a non-null nested list (non-null list AND
 *    non-null items) under a nullable `buddy`, selecting a non-null `email`.
 *
 * The `social` service hands out columns of downstream keys (friends /
 * buddy / cohorts) but its composite extraction key is (id, region), so the
 * planner must pull `region` from the `profiles` service first — a third
 * service in the key chain. `contacts` fails for User 7 exactly once; the
 * shared record must surface that single failure at BOTH response paths
 * with the correct null-hierarchy propagation at each.
 */
const schema = defineSchema({
  entities: {
    User: {
      key: ['id'],
      services: {
        profiles: { key: ['id'], provides: ['name', 'region'], cost: 1, batchLimit: 10 },
        social: { key: ['id', 'region'], provides: ['friends', 'buddy', 'cohorts'], cost: 1, batchLimit: 10 },
        contacts: { key: ['id', 'region'], provides: ['email'], cost: 1, batchLimit: 10 },
      },
      links: {
        friends: { target: 'User', many: true },
        buddy: { target: 'User', many: false },
        cohorts: { target: 'User', many: true },
      },
    },
  },
});

const REGION = { 1: 'r1', 4: 'r4', 7: 'r7', 8: 'r8', 9: 'r9' };

const reads = {
  profiles: async (keys) =>
    keys.map((k) => ({
      key: k,
      fields: { name: `user-${k.id}`, region: REGION[k.id] },
    })),
  social: async (keys) =>
    keys.map((k) => {
      if (k.id === '1') {
        return {
          key: k,
          fields: { friends: [{ id: '7' }, { id: '8' }], buddy: { id: '4' } },
        };
      }
      if (k.id === '4') {
        return { key: k, fields: { cohorts: [{ id: '7' }, { id: '9' }] } };
      }
      return { key: k, fields: { friends: [], buddy: null, cohorts: [] } };
    }),
  contacts: async (keys) =>
    keys.map((k) =>
      k.id === '7'
        ? { key: k, error: { message: 'contacts store lost user 7' } }
        : { key: k, fields: { email: `u${k.id}@x.test` } },
    ),
};

const query = {
  name: 'me',
  type: 'User',
  key: { id: '1' },
  select: [
    { field: 'name' },
    {
      field: 'friends',
      list: true,
      nonNull: false,
      itemNonNull: false,
      node: { type: 'User', select: [{ field: 'email' }] },
    },
    {
      field: 'buddy',
      nonNull: false,
      node: {
        type: 'User',
        select: [
          {
            field: 'cohorts',
            list: true,
            nonNull: true, // non-null nested list...
            itemNonNull: true, // ...with non-null items
            node: { type: 'User', select: [{ field: 'email', nonNull: true }] },
          },
        ],
      },
    },
  ],
};

test('same entity in nullable list and non-null nested list: reads, key sources, paths', async () => {
  const result = await executeQuery({ schema, query, reads });

  // --- response shape -------------------------------------------------
  assert.equal(result.data.name, 'user-1', 'root branch continues');
  assert.deepEqual(
    result.data.friends,
    [{ email: null }, { email: 'u8@x.test' }],
    'nullable list keeps the failed item position as null',
  );
  assert.equal(
    result.data.buddy,
    null,
    'non-null email inside non-null items inside non-null list propagates ' +
      'up to the nearest nullable position (buddy)',
  );

  // --- every affected response path, with aliases/indices -------------
  assert.deepEqual(
    result.errors.map((e) => ({ path: e.path, message: e.message })),
    [
      { path: ['me', 'friends', 0, 'email'], message: 'contacts store lost user 7' },
      { path: ['me', 'buddy', 'cohorts', 0, 'email'], message: 'contacts store lost user 7' },
    ],
    'one underlying item error, both affected response paths reported',
  );

  // --- actual read counts ----------------------------------------------
  const readsOf = (svc) => result.stats.reads.filter((r) => r.service === svc);
  const idsOf = (svc) => readsOf(svc).flatMap((r) => r.keys.map((k) => k.id));

  assert.deepEqual(
    idsOf('contacts').sort(),
    ['7', '8', '9'],
    'user 7 fetched from contacts exactly once despite two occurrences',
  );
  assert.deepEqual(
    idsOf('profiles').sort(),
    ['1', '4', '7', '8', '9'],
    'user 7 profile fetched exactly once as well',
  );
  assert.deepEqual(idsOf('social').sort(), ['1', '4']);

  // --- key sources ------------------------------------------------------
  // social and contacts both require (id, region); the region component
  // can only have come from profiles reads (the third service in the chain).
  for (const read of readsOf('social')) {
    for (const key of read.keys) {
      assert.equal(key.region, REGION[key.id], `social key for ${key.id} carries profiles region`);
    }
  }
  for (const read of readsOf('contacts')) {
    for (const key of read.keys) {
      assert.equal(key.region, REGION[key.id], `contacts key for ${key.id} carries profiles region`);
    }
  }
  // The plan for the root node must chain profiles before social.
  const rootPlan = result.stats.plans.find((p) => p.entity === 'User');
  assert.ok(rootPlan.ok);
  assert.ok(
    rootPlan.stages.indexOf('profiles') < rootPlan.stages.indexOf('social'),
    'profiles must run before social so the composite key can be assembled',
  );
});
