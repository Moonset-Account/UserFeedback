import test from 'node:test';
import assert from 'node:assert/strict';

import { createConfigCenter } from '../src/index.js';

test('initial poll yields a consistent snapshot, then later commits arrive one transaction at a time', () => {
  const center = createConfigCenter();
  center.commit([
    { key: 'app/db/host', value: 'db-1' },
    { key: 'app/db/port', value: 5432 },
    { key: 'other/token', value: 'secret' },
  ]);

  const sub = center.subscribe({ prefix: 'app' });

  const first = sub.poll();
  assert.equal(first.type, 'snapshot');
  assert.equal(first.reason, 'initial');
  assert.equal(first.revision, 1);
  assert.equal(first.snapshot.revision, 1);
  assert.deepEqual(first.snapshot.entries, [
    { key: 'app/db/host', value: 'db-1' },
    { key: 'app/db/port', value: 5432 },
  ]);
  assert.deepEqual(first.transactions, []);

  center.commit([{ key: 'app/db/port', value: 5433 }]);
  center.commit([{ key: 'app/feature/x', value: true }]);

  const second = sub.poll();
  assert.equal(second.type, 'changes');
  assert.equal(second.since, 1);
  assert.equal(second.revision, 3);
  assert.deepEqual(second.transactions, [
    { revision: 2, changes: [{ key: 'app/db/port', deleted: false, value: 5433 }] },
    { revision: 3, changes: [{ key: 'app/feature/x', deleted: false, value: true }] },
  ]);
  assert.equal(sub.cursor, 3);

  assert.deepEqual(sub.poll(), {
    type: 'changes',
    reason: null,
    revision: 3,
    since: 3,
    prefix: 'app',
    snapshot: null,
    transactions: [],
  });
});

test('a multi-key commit is delivered whole in a single transaction entry', () => {
  const center = createConfigCenter();
  const sub = center.subscribe({ prefix: 'svc' });
  sub.poll(); // initial empty snapshot at revision 0

  center.commit([
    { key: 'svc/a', value: 1 },
    { key: 'svc/b', value: 2 },
    { key: 'unrelated/c', value: 3 },
    { key: 'svc/d', value: 4 },
  ]);

  const batch = sub.poll();
  assert.equal(batch.type, 'changes');
  assert.equal(batch.transactions.length, 1, 'no splitting of a transaction');
  assert.equal(batch.transactions[0].revision, 1);
  assert.deepEqual(batch.transactions[0].changes, [
    { key: 'svc/a', deleted: false, value: 1 },
    { key: 'svc/b', deleted: false, value: 2 },
    { key: 'svc/d', deleted: false, value: 4 },
  ]);
});

test('deletions are visible as explicit delete markers', () => {
  const center = createConfigCenter();
  center.commit([{ key: 'app/a', value: 'x' }]);
  center.commit([{ key: 'app/b', value: 'y' }]);

  const sub = center.subscribe({ prefix: 'app', since: 2 });
  center.commit([
    { key: 'app/a', deleted: true },
    { key: 'app/b', value: 'y2' },
  ]);

  const batch = sub.poll();
  assert.deepEqual(batch.transactions, [
    {
      revision: 3,
      changes: [
        { key: 'app/a', deleted: true },
        { key: 'app/b', deleted: false, value: 'y2' },
      ],
    },
  ]);

  assert.deepEqual(center.snapshot('app'), {
    revision: 3,
    prefix: 'app',
    entries: [{ key: 'app/b', value: 'y2' }],
  });
});

test('a subscriber advances past transactions that only touch other prefixes', () => {
  const center = createConfigCenter();
  center.commit([{ key: 'app/a', value: 1 }]);

  const sub = center.subscribe({ prefix: 'app', since: 1 });

  center.commit([{ key: 'other/x', value: 'ignored' }]);
  center.commit([{ key: 'other/y', value: 'ignored' }]);

  const quiet = sub.poll();
  assert.equal(quiet.type, 'changes');
  assert.equal(quiet.since, 1);
  assert.equal(quiet.revision, 3);
  assert.deepEqual(quiet.transactions, []);
  assert.equal(sub.cursor, 3);

  center.commit([{ key: 'app/a', value: 2 }]);

  const batch = sub.poll();
  assert.equal(batch.since, 3);
  assert.equal(batch.revision, 4);
  assert.deepEqual(batch.transactions, [
    { revision: 4, changes: [{ key: 'app/a', deleted: false, value: 2 }] },
  ]);
});

test('a multi-key commit with a repeated key reports only the final write once', () => {
  const center = createConfigCenter();
  center.commit([{ key: 'app/a', value: 'before' }]);

  const sub = center.subscribe({ prefix: 'app', since: 1 });
  center.commit([
    { key: 'app/a', value: 'middle' },
    { key: 'app/b', value: 'b1' },
    { key: 'app/a', value: 'after' },
  ]);

  const batch = sub.poll();
  assert.deepEqual(batch.transactions, [
    {
      revision: 2,
      changes: [
        { key: 'app/a', deleted: false, value: 'after' },
        { key: 'app/b', deleted: false, value: 'b1' },
      ],
    },
  ]);
  assert.deepEqual(center.snapshot('app').entries, [
    { key: 'app/a', value: 'after' },
    { key: 'app/b', value: 'b1' },
  ]);
});

test('snapshot transport may pause; a reconnecter can tell the initial value from two successors', () => {
  const center = createConfigCenter();
  center.commit([{ key: 'app/db/host', value: 'v0' }]);

  // First connection: read the snapshot, then stall before asking for changes.
  const first = center.subscribe({ prefix: 'app' });
  const snapshot = first.snapshot();
  assert.deepEqual(snapshot.entries, [{ key: 'app/db/host', value: 'v0' }]);
  const baseRevision = snapshot.revision;

  // Two more transactions to the same key while the snapshot is in flight.
  center.commit([{ key: 'app/db/host', value: 'v1' }]);
  center.commit([{ key: 'app/db/host', value: 'v2' }]);

  // The stalled client resumes with its last consumed revision.
  const resumed = center.subscribe({ prefix: 'app', since: baseRevision });
  const batch = resumed.poll();

  assert.equal(batch.type, 'changes');
  assert.equal(batch.since, baseRevision);
  assert.deepEqual(batch.transactions, [
    { revision: 2, changes: [{ key: 'app/db/host', deleted: false, value: 'v1' }] },
    { revision: 3, changes: [{ key: 'app/db/host', deleted: false, value: 'v2' }] },
  ]);

  // Reconciling snapshot + ordered transactions reproduces the live state.
  const state = new Map(snapshot.entries.map((entry) => [entry.key, entry.value]));
  for (const transaction of batch.transactions) {
    for (const change of transaction.changes) {
      if (change.deleted) state.delete(change.key);
      else state.set(change.key, change.value);
    }
  }
  assert.deepEqual([...state], [['app/db/host', 'v2']]);

  // The stalled subscription also catches up correctly if it is resumed directly.
  assert.deepEqual(first.poll().transactions, batch.transactions);
});

test('a cursor below the trimmed line is reset with a snapshot at the current revision', () => {
  const center = createConfigCenter({ maxHistory: 2 });
  for (let index = 1; index <= 5; index += 1) {
    center.commit([{ key: `app/k${index}`, value: index }]);
  }
  assert.equal(center.revision, 5);
  assert.equal(center.trimmedThrough, 3);
  assert.equal(center.historySize, 2);

  const sub = center.subscribe({ prefix: 'app', since: 1 });
  const reset = sub.poll();

  assert.equal(reset.type, 'reset');
  assert.equal(reset.reason, 'history-trimmed');
  assert.equal(reset.revision, 5);
  assert.equal(reset.snapshot.revision, 5);
  assert.deepEqual(reset.snapshot.entries, [
    { key: 'app/k1', value: 1 },
    { key: 'app/k2', value: 2 },
    { key: 'app/k3', value: 3 },
    { key: 'app/k4', value: 4 },
    { key: 'app/k5', value: 5 },
  ]);
  assert.deepEqual(reset.transactions, []);
  assert.equal(sub.cursor, 5);

  // Subsequent transactions are delivered on top of the reset snapshot.
  center.commit([{ key: 'app/k6', value: 6 }]);
  const after = sub.poll();
  assert.equal(after.type, 'changes');
  assert.equal(after.since, 5);
  assert.equal(after.revision, 6);
  assert.deepEqual(after.transactions, [
    { revision: 6, changes: [{ key: 'app/k6', deleted: false, value: 6 }] },
  ]);
});

test('a cursor exactly on the trimmed line is still served incrementally', () => {
  const center = createConfigCenter({ maxHistory: 1 });
  center.commit([{ key: 'app/a', value: 1 }]);
  center.commit([{ key: 'app/a', value: 2 }]);
  assert.equal(center.trimmedThrough, 1);

  const sub = center.subscribe({ prefix: 'app', since: 1 });
  const batch = sub.poll();
  assert.equal(batch.type, 'changes');
  assert.deepEqual(batch.transactions, [
    { revision: 2, changes: [{ key: 'app/a', deleted: false, value: 2 }] },
  ]);
});

test('a reset snapshot and its revision are of the same commit', () => {
  const center = createConfigCenter({ maxHistory: 0 });
  center.commit([{ key: 'app/a', value: 1 }]);
  center.commit([{ key: 'app/b', value: 2 }]);

  const sub = center.subscribe({ prefix: 'app', since: 0 });
  const reset = sub.poll();

  assert.equal(reset.type, 'reset');
  assert.equal(reset.reason, 'history-trimmed');
  assert.equal(reset.revision, 2);
  assert.equal(reset.snapshot.revision, 2);
  assert.deepEqual(reset.snapshot.entries, [
    { key: 'app/a', value: 1 },
    { key: 'app/b', value: 2 },
  ]);
});

test('prefix matching respects whole path segments', () => {
  const center = createConfigCenter();
  center.commit([
    { key: 'app', value: 'root' },
    { key: 'app/x', value: 1 },
    { key: 'apple', value: 'no' },
    { key: 'application/y', value: 'no' },
  ]);

  assert.deepEqual(
    center.snapshot('app').entries.map((entry) => entry.key),
    ['app', 'app/x'],
  );
  assert.deepEqual(
    center.snapshot('app/').entries.map((entry) => entry.key),
    ['app', 'app/x'],
  );
  assert.deepEqual(center.snapshot('').entries.map((entry) => entry.key), [
    'app',
    'app/x',
    'apple',
    'application/y',
  ]);

  const sub = center.subscribe({ prefix: 'app', since: 0 });
  const [transaction] = sub.poll().transactions;
  assert.deepEqual(
    transaction.changes.map((change) => change.key),
    ['app', 'app/x'],
  );
});

test('a custom separator switches prefix semantics', () => {
  const center = createConfigCenter({ separator: '.' });
  center.commit([
    { key: 'app.db.host', value: 'h' },
    { key: 'appx.db', value: 'no' },
  ]);

  assert.deepEqual(
    center.snapshot('app').entries.map((entry) => entry.key),
    ['app.db.host'],
  );
});

test('raw string prefixes are available via an empty separator', () => {
  const center = createConfigCenter({ separator: '' });
  center.commit([
    { key: 'app/db', value: 1 },
    { key: 'apple', value: 2 },
  ]);

  assert.deepEqual(
    center.snapshot('app').entries.map((entry) => entry.key),
    ['app/db', 'apple'],
  );
});

test('snapshots are isolated from later commits and from caller mutation', () => {
  const center = createConfigCenter();
  center.commit([{ key: 'app/config', value: { nested: { enabled: true } } }]);

  const sub = center.subscribe({ prefix: 'app' });
  const snap = sub.snapshot();
  snap.entries[0].value.nested.enabled = false;

  center.commit([{ key: 'app/config', value: { nested: { enabled: false } } }]);

  const fresh = center.snapshot('app');
  assert.equal(fresh.entries[0].value.nested.enabled, false);
  assert.equal(fresh.revision, 2);

  const batch = sub.poll();
  assert.equal(batch.transactions[0].changes[0].value.nested.enabled, false);
});

test('batch payloads are isolated from the kernel state', () => {
  const center = createConfigCenter();
  center.commit([{ key: 'app/list', value: [1, 2] }]);

  const sub = center.subscribe({ prefix: 'app', since: 0 });
  const batch = sub.poll();
  batch.transactions[0].changes[0].value.push(3);

  assert.deepEqual(center.snapshot('app').entries, [
    { key: 'app/list', value: [1, 2] },
  ]);
});

test('independent subscribers keep independent cursors', () => {
  const center = createConfigCenter();
  center.commit([{ key: 'app/a', value: 1 }]);

  const slow = center.subscribe({ prefix: 'app' });
  const fast = center.subscribe({ prefix: 'app' });

  slow.poll();
  fast.poll();

  center.commit([{ key: 'app/a', value: 2 }]);
  center.commit([{ key: 'app/a', value: 3 }]);

  // Fast one drains everything at once.
  assert.equal(fast.poll().transactions.length, 2);

  // Slow one is unaffected by the other subscriber's progress.
  const slowBatch = slow.poll();
  assert.equal(slowBatch.transactions.length, 2);
  assert.equal(slowBatch.revision, 3);
});

test('subscription lifecycle errors are explicit', () => {
  const center = createConfigCenter();
  center.commit([{ key: 'app/a', value: 1 }]);

  const sub = center.subscribe({ prefix: 'app' });
  sub.poll();
  assert.throws(() => sub.snapshot(), /has a cursor/);

  const snapOnly = center.subscribe({ prefix: 'app' });
  snapOnly.snapshot();
  assert.throws(() => snapOnly.snapshot(), /has a cursor/);

  const unknown = center.subscribe({ prefix: 'app', since: 99 });
  const reset = unknown.poll();
  assert.equal(reset.type, 'reset');
  assert.equal(reset.reason, 'unknown-revision');
  assert.equal(reset.revision, 1);
  assert.equal(reset.snapshot.revision, 1);

  sub.close();
  assert.throws(() => sub.poll(), /closed/);

  assert.throws(() => center.subscribe({ prefix: 'app', since: -1 }), /non-negative integer/);
  assert.throws(() => center.subscribe({ prefix: 'app', since: 1.5 }), /non-negative integer/);
});

test('invalid commits are rejected atomically', () => {
  const center = createConfigCenter();
  center.commit([{ key: 'app/a', value: 1 }]);

  assert.throws(() => center.commit('nope'), /array of changes/);
  assert.throws(() => center.commit([{ key: 'app/a' }]), /must provide "value"/);
  assert.throws(
    () => center.commit([{ key: 'app/a', value: 1, deleted: true }]),
    /cannot be both/,
  );
  assert.throws(() => center.commit([{ key: '', value: 1 }]), /non-empty string/);
  assert.throws(
    () => center.commit([{ key: 'app/a', value: 1 }, { key: 2, value: 2 }]),
    /non-empty string/,
  );

  // Nothing from the rejected batch was applied.
  assert.equal(center.revision, 1);
  assert.deepEqual(center.snapshot('app').entries, [{ key: 'app/a', value: 1 }]);
});

test('an unbounded history never resets', () => {
  const center = createConfigCenter({ maxHistory: Infinity });
  for (let index = 1; index <= 50; index += 1) {
    center.commit([{ key: 'app/k', value: index }]);
  }
  assert.equal(center.trimmedThrough, 0);
  assert.equal(center.historySize, 50);

  const sub = center.subscribe({ prefix: 'app', since: 0 });
  const batch = sub.poll();
  assert.equal(batch.type, 'changes');
  assert.equal(batch.transactions.length, 50);
  assert.deepEqual(
    batch.transactions.map((transaction) => transaction.changes[0].value),
    Array.from({ length: 50 }, (_, index) => index + 1),
  );
  assert.equal(batch.revision, 50);
});

test('the first poll after construction still pauses for a client that wants to read the snapshot in place', () => {
  const center = createConfigCenter();
  center.commit([{ key: 'app/a', value: 1 }]);

  const sub = center.subscribe({ prefix: 'app' });
  center.commit([{ key: 'app/a', value: 2 }]);

  const first = sub.poll();
  assert.equal(first.type, 'snapshot');
  assert.equal(first.revision, 2);
  assert.deepEqual(first.snapshot.entries, [{ key: 'app/a', value: 2 }]);

  assert.deepEqual(sub.poll().transactions, []);
});
