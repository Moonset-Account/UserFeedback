/**
 * Prefix subscriptions.
 *
 * A subscription is a cursor over the global revision stream plus a prefix
 * filter. It is deliberately split into two steps so that a client may read a
 * snapshot, spend arbitrary time applying it, and only then ask for the first
 * batch of changes:
 *
 *   const sub = center.subscribe({ prefix: 'app' });
 *   const snap = sub.snapshot();       // revision R0; cursor parked at R0
 *   ... apply snap, maybe slowly ...
 *   const batch = sub.poll();          // transactions R0+1 .. head
 *
 * `snapshot()` only pins the cursor; the snapshot itself is materialised from
 * the live map, so commits that happen while the client applies it are not lost.
 * A fresh subscription may also simply call `poll()` first, which returns the
 * initial snapshot and sets the cursor in one step.
 *
 * If instead the client already knows its last consumed revision (reconnect),
 * it passes `since` and skips `snapshot()`:
 *
 *   const sub = center.subscribe({ prefix: 'app', since: lastRevision });
 *
 * `poll()` returns one of three shapes:
 *
 *   { type: 'snapshot', reason: 'initial', revision, snapshot, transactions: [] }
 *   { type: 'reset',    reason: 'history-trimmed' | 'unknown-revision',
 *     revision, snapshot, transactions: [] }
 *   { type: 'changes',  revision, since, transactions: [{ revision, changes }] }
 *
 * `snapshot.revision` always equals the batch `revision`, and `revision` is
 * the cursor to persist; the client brings it back as `since` on reconnect.
 *
 * Every entry of `transactions` is one whole transaction. All of that
 * transaction's changes matching the prefix are present in a single entry, so
 * a multi-key commit is never split, and a key written twice in one commit
 * appears once with the final value. Deletions appear as `{ key, deleted: true }`.
 */

import { buildSnapshot, INTERNALS, materializeChange } from './internals.js';
import { matchesPrefix, normalizePrefix } from './keys.js';

export function createSubscription(center, options) {
  if (options === null || typeof options !== 'object' || Array.isArray(options)) {
    throw new TypeError('subscribe(options) expects an options object');
  }

  const state = INTERNALS.get(center);
  if (!state) {
    throw new TypeError('subscribe() must be called on a config center created by createConfigCenter()');
  }

  const prefix = normalizePrefix(options.prefix, state.separator);
  const initialCursor = resolveSince(options.since);

  let cursor = initialCursor;
  let closed = false;

  function assertOpen() {
    if (closed) throw new Error('subscription is closed');
  }

  function snapshot() {
    assertOpen();
    if (cursor !== null) {
      throw new Error(
        'snapshot() is not available after the subscription has a cursor; ' +
          'use center.snapshot(prefix) or open a new subscription without "since"',
      );
    }
    const snap = buildSnapshot(state, prefix);
    cursor = snap.revision;
    return snap;
  }

  function poll() {
    assertOpen();
    const head = state.headRevision;

    if (cursor === null) {
      const snap = buildSnapshot(state, prefix);
      cursor = snap.revision;
      return {
        type: 'snapshot',
        reason: 'initial',
        revision: cursor,
        since: null,
        prefix,
        snapshot: snap,
        transactions: [],
      };
    }

    if (cursor > head) {
      return reset('unknown-revision', cursor);
    }

    if (cursor < state.trimmedThrough) {
      return reset('history-trimmed', cursor);
    }

    const previous = cursor;
    const transactions = [];
    for (const transaction of state.log) {
      if (transaction.revision <= previous) continue;
      const changes = [];
      for (const change of transaction.changes.values()) {
        if (matchesPrefix(prefix, change.key, state.separator)) {
          changes.push(materializeChange(state, change));
        }
      }
      if (changes.length > 0) {
        transactions.push({ revision: transaction.revision, changes });
      }
    }

    // Advance to head even when nothing matched: transactions for other
    // prefixes still move the global stream forward.
    cursor = head;

    return {
      type: 'changes',
      reason: null,
      revision: cursor,
      since: previous,
      prefix,
      snapshot: null,
      transactions,
    };
  }

  function reset(reason, staleCursor) {
    const snap = buildSnapshot(state, prefix);
    cursor = snap.revision;
    return {
      type: 'reset',
      reason,
      revision: cursor,
      since: staleCursor,
      prefix,
      snapshot: snap,
      transactions: [],
    };
  }

  function close() {
    closed = true;
  }

  return {
    get prefix() {
      return prefix;
    },
    /** Last revision this subscription has consumed, or null before the first poll. */
    get cursor() {
      return cursor;
    },
    get closed() {
      return closed;
    },
    snapshot,
    poll,
    close,
  };
}

function resolveSince(value) {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new TypeError('options.since must be a non-negative integer revision');
  }
  return value;
}
