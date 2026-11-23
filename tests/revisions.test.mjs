import { test } from 'node:test';
import assert from 'node:assert/strict';
import { defineSchema } from '../src/schema.js';
import { executeQuery } from '../src/executor.js';
import { assessLateReceipt } from '../src/revisions.js';
import { TimeoutError } from '../src/errors.js';

const schema = defineSchema({
  entities: {
    User: {
      key: ['id'],
      services: {
        profiles: { key: ['id'], provides: ['name'], cost: 1, batchLimit: 10 },
        contacts: { key: ['id'], provides: ['email'], cost: 1, batchLimit: 10 },
        social: { key: ['id'], provides: ['bestFriend', 'colleague'], cost: 1, batchLimit: 10 },
      },
      links: {
        bestFriend: { target: 'User', many: false },
        colleague: { target: 'User', many: false },
      },
    },
  },
});

const listQuery = {
  name: 'users',
  type: 'User',
  list: true,
  keys: [{ id: '1' }, { id: '2' }],
  select: [{ field: 'name' }, { field: 'email' }],
};

test('batch timeout checkpoints partial state; resume reuses still-co-readable records', async () => {
  // Run 1: profiles answers at revision 2, contacts times out.
  const run1 = await executeQuery({
    schema,
    query: listQuery,
    reads: {
      profiles: async (keys) =>
        keys.map((k) => ({ key: k, fields: { name: `user-${k.id}` }, revision: 2 })),
      contacts: async () => {
        throw new TimeoutError('contacts batch timed out');
      },
    },
    coReadable: [{ profiles: 2, contacts: 2 }],
    timeoutPolicy: 'checkpoint',
  });

  assert.equal(run1.done, false);
  assert.equal(run1.data, null);
  const stored = run1.checkpoint.records.User;
  assert.equal(Object.keys(stored).length, 2, 'partial responses are archived');
  assert.deepEqual(stored['{"id":"1"}'].revisions, { profiles: 2 });
  assert.deepEqual(run1.checkpoint.pending, [
    { entity: 'User', service: 'contacts', keys: [{ id: '1' }, { id: '2' }] },
  ]);

  // Resume: contacts has rolled to revision 3; profiles@2 is still part of
  // a declared co-readable vector, so the archived names remain provably
  // valid and must NOT be re-read. Only the pending key fetch is re-planned.
  const resumed = await executeQuery({
    schema,
    query: listQuery,
    reads: {
      profiles: async () => {
        throw new Error('profiles must not be re-read: archived records are still co-readable');
      },
      contacts: async (keys) =>
        keys.map((k) => ({ key: k, fields: { email: `u${k.id}@x.test` }, revision: 3 })),
    },
    coReadable: [{ profiles: 2, contacts: 3 }],
    checkpoint: run1.checkpoint,
    timeoutPolicy: 'checkpoint',
  });

  assert.equal(resumed.done, true);
  assert.deepEqual(resumed.data, [
    { name: 'user-1', email: 'u1@x.test' },
    { name: 'user-2', email: 'u2@x.test' },
  ]);
  assert.equal(resumed.stats.reused.length, 2, 'both archived records reused');
  assert.deepEqual(
    resumed.stats.reads.map((r) => r.service),
    ['contacts'],
    'only the previously pending service is re-fetched',
  );
  assert.deepEqual(resumed.vectors['users.0'], { profiles: 2, contacts: 3 });
});

test('resume drops archived records whose revisions are no longer co-readable', async () => {
  const run1 = await executeQuery({
    schema,
    query: listQuery,
    reads: {
      profiles: async (keys) =>
        keys.map((k) => ({ key: k, fields: { name: `old-${k.id}` }, revision: 2 })),
      contacts: async () => {
        throw new TimeoutError();
      },
    },
    coReadable: [{ profiles: 2, contacts: 2 }],
    timeoutPolicy: 'checkpoint',
  });
  assert.equal(run1.done, false);

  // Resume: profiles has rolled to 3 and no declared vector contains
  // profiles@2 any more, so the archived names are not provably valid.
  const resumed = await executeQuery({
    schema,
    query: listQuery,
    reads: {
      profiles: async (keys) =>
        keys.map((k) => ({ key: k, fields: { name: `new-${k.id}` }, revision: 3 })),
      contacts: async (keys) =>
        keys.map((k) => ({ key: k, fields: { email: `u${k.id}@x.test` }, revision: 3 })),
    },
    coReadable: [{ profiles: 3, contacts: 3 }],
    checkpoint: run1.checkpoint,
  });

  assert.equal(resumed.stats.reused.length, 0);
  assert.equal(resumed.stats.dropped.length, 2, 'stale archived records are discarded');
  assert.deepEqual(
    resumed.stats.reads.map((r) => r.service).sort(),
    ['contacts', 'profiles'],
    'dropped fields are re-planned and re-fetched',
  );
  assert.deepEqual(resumed.data, [
    { name: 'new-1', email: 'u1@x.test' },
    { name: 'new-2', email: 'u2@x.test' },
  ]);
});

test('successful batch carrying an expired component key fails the branch, not the query', async () => {
  // social answers at revision 2, but the bestFriend key it hands back was
  // extracted at revision 1 — no declared vector contains social@1, so the
  // linked entity can never be stitched into a consistent snapshot.
  const result = await executeQuery({
    schema,
    query: {
      name: 'me',
      type: 'User',
      key: { id: '1' },
      select: [
        { field: 'name' },
        {
          field: 'bestFriend',
          nonNull: false,
          node: { type: 'User', select: [{ field: 'name' }] },
        },
      ],
    },
    reads: {
      profiles: async (keys) =>
        keys.map((k) => ({ key: k, fields: { name: `user-${k.id}` }, revision: 2 })),
      social: async (keys) =>
        keys.map((k) => ({
          key: k,
          fields: { bestFriend: { key: { id: '2' }, keyRevision: 1 } },
          revision: 2,
        })),
    },
    coReadable: [{ social: 2, profiles: 2 }],
  });

  assert.equal(result.data.name, 'user-1', 'other branches continue');
  assert.equal(result.data.bestFriend, null);
  assert.equal(result.errors.length, 1);
  assert.deepEqual(result.errors[0].path, ['me', 'bestFriend']);
  assert.match(result.errors[0].message, /stitch conflict/);
  assert.match(result.errors[0].message, /social/);
});

test('a parent field that needs two incompatible revisions is never stitched into existence', async () => {
  // name arrives at profiles@2, email at contacts@3; the declared vectors
  // only allow (2,2) or (3,3). Assembling the entity would fabricate a
  // combination that never existed.
  const result = await executeQuery({
    schema,
    query: {
      name: 'me',
      type: 'User',
      key: { id: '1' },
      select: [{ field: 'name' }, { field: 'email' }],
    },
    reads: {
      profiles: async (keys) =>
        keys.map((k) => ({ key: k, fields: { name: 'user-1' }, revision: 2 })),
      contacts: async (keys) =>
        keys.map((k) => ({ key: k, fields: { email: 'u1@x.test' }, revision: 3 })),
    },
    coReadable: [
      { profiles: 2, contacts: 2 },
      { profiles: 3, contacts: 3 },
    ],
  });

  assert.equal(result.data, null, 'the whole branch nulls out per the null hierarchy');
  assert.equal(result.errors.length, 1);
  assert.deepEqual(result.errors[0].path, ['me']);
  assert.match(result.errors[0].message, /stitch conflict/);
  assert.match(result.errors[0].message, /never existed/);
});

test('late old-batch receipt: evidence of usability for two aliases', async () => {
  const query = {
    name: 'me',
    type: 'User',
    key: { id: '0' },
    select: [
      {
        field: 'bestFriend',
        alias: 'bf',
        node: { type: 'User', select: [{ field: 'name' }, { field: 'email' }] },
      },
      {
        field: 'colleague',
        alias: 'col',
        node: { type: 'User', select: [{ field: 'name' }, { field: 'email' }] },
      },
    ],
  };
  const socialRead = async (keys) =>
    keys.map((k) => ({
      key: k,
      fields: { bestFriend: { id: '1' }, colleague: { id: '2' } },
      revision: 2,
    }));

  // Run 1: the contacts batch for user 1 times out.
  const run1 = await executeQuery({
    schema,
    query,
    reads: {
      social: socialRead,
      profiles: async (keys) =>
        keys.map((k) => ({ key: k, fields: { name: `user-${k.id}` }, revision: 2 })),
      contacts: async () => {
        throw new TimeoutError('contacts batch for user 1 timed out');
      },
    },
    coReadable: [{ social: 2, profiles: 2, contacts: 2 }],
    timeoutPolicy: 'checkpoint',
  });
  assert.equal(run1.done, false);
  assert.deepEqual(run1.checkpoint.pending, [
    { entity: 'User', service: 'contacts', keys: [{ id: '1' }] },
  ]);

  // The old batch's receipt eventually arrives, still stamped contacts@2.
  const lateReceipt = {
    service: 'contacts',
    revision: 2,
    items: [{ key: { id: '1' }, fields: { email: 'late-1@x.test' }, revision: 2 }],
  };

  // Resume: user 1 is still served at contacts@2, but user 2's branch
  // commits contacts@3 (both vectors remain co-readable).
  const resumed = await executeQuery({
    schema,
    query,
    reads: {
      social: socialRead,
      profiles: async (keys) =>
        keys.map((k) => ({ key: k, fields: { name: `user-${k.id}` }, revision: 2 })),
      contacts: async (keys) =>
        keys.map((k) => ({
          key: k,
          fields: { email: `u${k.id}@x.test` },
          revision: k.id === '1' ? 2 : 3,
        })),
    },
    coReadable: [
      { social: 2, profiles: 2, contacts: 2 },
      { social: 2, profiles: 2, contacts: 3 },
    ],
    checkpoint: run1.checkpoint,
  });

  assert.equal(resumed.done, true);
  assert.equal(resumed.data.bf.email, 'u1@x.test');
  assert.equal(resumed.data.col.email, 'u2@x.test');
  assert.deepEqual(resumed.vectors['me.bf'], { profiles: 2, contacts: 2 });
  assert.deepEqual(resumed.vectors['me.col'], { profiles: 2, contacts: 3 });

  const evidence = assessLateReceipt({
    receipt: lateReceipt,
    branches: {
      'me.bf': resumed.vectors['me.bf'],
      'me.col': resumed.vectors['me.col'],
    },
    coReadable: [
      { social: 2, profiles: 2, contacts: 2 },
      { social: 2, profiles: 2, contacts: 3 },
    ],
  });

  // Alias bf committed contacts@2: the receipt agrees with the committed
  // snapshot, so it is usable there.
  assert.equal(evidence.aliases['me.bf'].usable, true);
  assert.deepEqual(evidence.aliases['me.bf'].evidence.receiptVector, { contacts: 2 });
  // Alias col committed contacts@3: the same receipt would force one
  // service to contribute two revisions — never a real snapshot.
  assert.equal(evidence.aliases['me.col'].usable, false);
  assert.match(evidence.aliases['me.col'].reason, /conflict/);
  assert.match(evidence.aliases['me.col'].reason, /contacts@3/);
  assert.equal(evidence.aliases['me.col'].evidence.mergedVector, null);
});
