// Bounded, insert-ordered record table.
//
// There is deliberately no "expire" operation: a record leaves only by being
// evicted from the far end of the insertion order, and only when the caller's
// policy confirms it is reclaimable (outside its retry window, not executing,
// not awaiting confirmation). An unresolved effect is never forgotten.

export class Store {
  #capacity;
  #map = new Map(); // businessKey -> record, insertion order

  constructor({ capacity }) {
    if (!Number.isSafeInteger(capacity) || capacity <= 0) {
      throw new RangeError('capacity must be a positive safe integer');
    }
    this.#capacity = capacity;
  }

  get capacity() {
    return this.#capacity;
  }

  get size() {
    return this.#map.size;
  }

  get(key) {
    return this.#map.get(key);
  }

  insert(record) {
    if (this.#map.has(record.businessKey)) {
      throw new Error(`duplicate business key ${record.businessKey}`);
    }
    if (this.#map.size >= this.#capacity) {
      throw new Error('store is full; caller must make room first');
    }
    this.#map.set(record.businessKey, record);
  }

  values() {
    return [...this.#map.values()];
  }

  [Symbol.iterator]() {
    return this.#map.values();
  }

  /** Lowest business key that is reclaimable at `now` and passes `canReclaim`. */
  evictionVictim(now, canReclaim) {
    for (const record of this.#map.values()) {
      if (record.retryDeadline <= now && canReclaim(record)) {
        return record;
      }
    }
    return null;
  }

  purge(record) {
    if (this.#map.get(record.businessKey) !== record) {
      throw new Error(`cannot purge non-resident record ${record.businessKey}`);
    }
    this.#map.delete(record.businessKey);
  }
}
