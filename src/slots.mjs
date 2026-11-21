import { SlotExhaustedError } from './errors.mjs';

/**
 * A fixed-size pool of weight-chunk slots shared by the published adapter
 * version and any in-flight load. Reservations are atomic: either every
 * requested slot is granted, or nothing is.
 */
export class SlotPool {
  #capacity;
  #owners = new Map(); // owner -> count

  constructor(capacity) {
    if (!Number.isInteger(capacity) || capacity < 1) {
      throw new RangeError('slot capacity must be a positive integer');
    }
    this.#capacity = capacity;
  }

  get capacity() {
    return this.#capacity;
  }

  get used() {
    let n = 0;
    for (const count of this.#owners.values()) n += count;
    return n;
  }

  get free() {
    return this.#capacity - this.used;
  }

  isReserved(owner) {
    return this.#owners.has(owner);
  }

  /** Atomically reserve `count` slots for `owner`. */
  reserve(owner, count) {
    if (this.#owners.has(owner)) {
      throw new Error(`slots already reserved for ${owner}`);
    }
    if (!Number.isInteger(count) || count < 1) {
      throw new RangeError('reservation count must be a positive integer');
    }
    if (this.free < count) {
      throw new SlotExhaustedError(count, this.free);
    }
    this.#owners.set(owner, count);
    return count;
  }

  /** Release every slot held by `owner`. Returns the number released. */
  release(owner) {
    const count = this.#owners.get(owner);
    if (count === undefined) return 0;
    this.#owners.delete(owner);
    return count;
  }

  /** Move a reservation from one owner label to another. */
  transfer(from, to) {
    if (!this.#owners.has(from)) {
      throw new Error(`no reservation owned by ${from}`);
    }
    if (this.#owners.has(to)) {
      throw new Error(`slots already reserved for ${to}`);
    }
    this.#owners.set(to, this.#owners.get(from));
    this.#owners.delete(from);
  }
}
