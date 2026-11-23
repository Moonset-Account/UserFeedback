import { test } from 'node:test';
import assert from 'node:assert/strict';
import { defineSchema } from '../src/schema.js';
import { executeQuery } from '../src/executor.js';

const schema = defineSchema({
  entities: {
    User: {
      key: ['id'],
      services: {
        profiles: { key: ['id'], provides: ['name', 'region'], cost: 4, batchLimit: 2 },
        contacts: { key: ['id', 'region'], provides: ['email'], cost: 2, batchLimit: 50 },
      },
    },
  },
});

const DB = {
  1: { name: 'ada', region: 'eu', email: 'ada@x.test' },
  2: { name: 'bo', region: 'us', email: 'bo@x.test' },
  3: { name: 'cy', region: 'eu', email: 'cy@x.test' },
};

function makeReads() {
  return {
    // Returns items out of order on purpose: the executor must scatter
    // them back to the right response positions.
    profiles: async (keys) =>
      [...keys].reverse().map((k) => ({
        key: k,
        fields: { name: DB[k.id].name, region: DB[k.id].region },
      })),
    contacts: async (keys) =>
      [...keys].reverse().map((k) => ({
        key: k,
        fields: { email: DB[k.id].email },
      })),
  };
}

test('batches, dedups and scatters out-of-order results to response positions', async () => {
  const reads = makeReads();
  const result = await executeQuery({
    schema,
    query: {
      name: 'users',
      type: 'User',
      list: true,
      keys: [{ id: '1' }, { id: '2' }, { id: '2' }, { id: '3' }], // duplicate id
      select: [
        { field: 'name', alias: 'n' },
        { field: 'email' },
      ],
    },
    reads,
  });

  assert.equal(result.done, true);
  assert.deepEqual(result.data, [
    { n: 'ada', email: 'ada@x.test' },
    { n: 'bo', email: 'bo@x.test' },
    { n: 'bo', email: 'bo@x.test' },
    { n: 'cy', email: 'cy@x.test' },
  ]);

  const profilesReads = result.stats.reads.filter((r) => r.service === 'profiles');
  const contactsReads = result.stats.reads.filter((r) => r.service === 'contacts');

  // profiles batchLimit is 2 over 3 unique ids -> exactly 2 batches.
  assert.equal(profilesReads.length, 2);
  assert.deepEqual(
    profilesReads.flatMap((r) => r.keys.map((k) => k.id)).sort(),
    ['1', '2', '3'],
    'duplicate id must be fetched once',
  );
  // contacts: one batch, and its composite keys carry the region that only
  // profiles could have supplied (key source evidence).
  assert.equal(contactsReads.length, 1);
  assert.deepEqual(
    contactsReads[0].keys.map((k) => `${k.id}@${k.region}`).sort(),
    ['1@eu', '2@us', '3@eu'],
  );
  // planner evidence: contacts ran after profiles.
  assert.deepEqual(result.stats.plans[0].stages, ['profiles', 'contacts']);
});

test('per-item error nulls a nullable field and keeps the original response path', async () => {
  const reads = makeReads();
  reads.contacts = async (keys) =>
    keys.map((k) =>
      k.id === '2'
        ? { key: k, error: { message: 'mailbox exploded' } }
        : { key: k, fields: { email: DB[k.id].email } },
    );

  const result = await executeQuery({
    schema,
    query: {
      name: 'users',
      type: 'User',
      list: true,
      keys: [{ id: '1' }, { id: '2' }, { id: '3' }],
      select: [{ field: 'name' }, { field: 'email' }],
    },
    reads,
  });

  assert.deepEqual(result.data, [
    { name: 'ada', email: 'ada@x.test' },
    { name: 'bo', email: null },
    { name: 'cy', email: 'cy@x.test' },
  ]);
  assert.deepEqual(result.errors, [
    { path: ['users', 1, 'email'], message: 'mailbox exploded' },
  ]);
});

test('error on a non-null field propagates to the nearest nullable position', async () => {
  const reads = makeReads();
  reads.profiles = async (keys) =>
    keys.map((k) =>
      k.id === '2'
        ? { key: k, error: { message: 'profile gone' } }
        : { key: k, fields: { name: DB[k.id].name, region: DB[k.id].region } },
    );

  const result = await executeQuery({
    schema,
    query: {
      name: 'users',
      type: 'User',
      list: true,
      itemNonNull: false, // list items are nullable
      keys: [{ id: '1' }, { id: '2' }, { id: '3' }],
      select: [{ field: 'name', nonNull: true }],
    },
    reads,
  });

  // Item 2 nulls out entirely; siblings are unaffected.
  assert.deepEqual(result.data, [{ name: 'ada' }, null, { name: 'cy' }]);
  assert.deepEqual(result.errors, [
    { path: ['users', 1, 'name'], message: 'profile gone' },
  ]);
});

test('error inside a non-null-item list nulls the whole list field, not the siblings', async () => {
  const strictSchema = defineSchema({
    entities: {
      User: {
        key: ['id'],
        services: {
          profiles: { key: ['id'], provides: ['name'], cost: 1, batchLimit: 10 },
          social: { key: ['id'], provides: ['friends'], cost: 1, batchLimit: 10 },
        },
        links: {
          friends: { target: 'User', many: true },
        },
      },
    },
  });
  const reads = {
    profiles: async (keys) =>
      keys.map((k) =>
        k.id === '9'
          ? { key: k, error: { message: 'no such profile' } }
          : { key: k, fields: { name: `user-${k.id}` } },
      ),
    social: async (keys) =>
      keys.map((k) => ({ key: k, fields: { friends: [{ id: '9' }, { id: '1' }] } })),
  };

  const result = await executeQuery({
    schema: strictSchema,
    query: {
      name: 'me',
      type: 'User',
      key: { id: '1' },
      select: [
        { field: 'name' },
        {
          field: 'friends',
          list: true,
          nonNull: false, // the list itself is nullable...
          itemNonNull: true, // ...but its items are not
          node: { type: 'User', select: [{ field: 'name', nonNull: true }] },
        },
      ],
    },
    reads,
  });

  assert.equal(result.data.name, 'user-1', 'other branches continue');
  assert.equal(result.data.friends, null, 'non-null item failure nulls the nullable list');
  assert.deepEqual(result.errors, [
    { path: ['me', 'friends', 0, 'name'], message: 'no such profile' },
  ]);
});

test('unresolvable fields surface the blocked chain as a field error', async () => {
  const blockedSchema = defineSchema({
    entities: {
      Knot: {
        key: ['k'],
        services: {
          sa: { key: ['y'], provides: ['x'], cost: 1, batchLimit: 10 },
          sb: { key: ['x'], provides: ['y'], cost: 1, batchLimit: 10 },
        },
      },
    },
  });
  const result = await executeQuery({
    schema: blockedSchema,
    query: {
      name: 'knot',
      type: 'Knot',
      key: { k: '1' },
      select: [{ field: 'x' }],
    },
    reads: { sa: async () => [], sb: async () => [] },
  });

  assert.deepEqual(result.data, { x: null });
  assert.equal(result.errors.length, 1);
  assert.equal(result.errors[0].path.join('.'), 'knot.x');
  assert.match(result.errors[0].message, /dependency cycle: [xy] -> [xy] -> [xy]/);
  assert.ok(result.errors[0].blocked.chain.length >= 2);
  // No service call may happen for a blocked plan.
  assert.deepEqual(result.stats.reads, []);
});
