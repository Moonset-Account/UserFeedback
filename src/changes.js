/**
 * Validation/normalisation of a commit payload.
 *
 * A commit is an array of writes:
 *
 *   { key: 'app/db/host', value: 'db-1' }
 *   { key: 'app/legacy/flag', deleted: true }
 *
 * Writes are recorded as intent, not as a diff against the current state: a
 * write to an existing value, or a delete of a missing key, still becomes part
 * of the transaction's change list. When the same key appears more than once
 * in a single transaction, the last write wins and only that final write is
 * recorded (a transaction is atomic, intermediate states are not observable).
 */

import { assertKey } from './keys.js';

export function normalizeChanges(changes) {
  if (!Array.isArray(changes)) {
    throw new TypeError('commit(changes) expects an array of changes');
  }

  const byKey = new Map();
  for (const raw of changes) {
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new TypeError('each change must be an object such as { key, value }');
    }

    const key = assertKey(raw.key);

    if (raw.deleted !== undefined && typeof raw.deleted !== 'boolean') {
      throw new TypeError(`change for key "${key}" has a non-boolean "deleted" flag`);
    }

    const deleted = raw.deleted === true;
    const hasValue = Object.prototype.hasOwnProperty.call(raw, 'value');

    if (deleted && hasValue) {
      throw new TypeError(`change for key "${key}" cannot be both a delete and a value write`);
    }
    if (!deleted && !hasValue) {
      throw new TypeError(`change for key "${key}" must provide "value" or "deleted: true"`);
    }

    byKey.set(key, deleted ? { key, deleted: true } : { key, deleted: false, value: raw.value });
  }

  return byKey;
}
