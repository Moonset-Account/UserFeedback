import {
  DuplicateChunkError,
  LoadInFlightError,
  StaleChunkError,
  UnknownBlockError,
  validateVector,
} from './errors.mjs';
import { requiredBlockIds, requiredSlotCount } from './manifest.mjs';
import { SlotPool } from './slots.mjs';
import { AdapterVersion, LoadAttempt } from './versions.mjs';

/**
 * Owns the single published adapter version, the (at most one) in-flight
 * load, and every open session.
 *
 * Lifecycle:
 *   beginLoad(manifest)   reserves all slotCount slots atomically; returns a
 *                         LoadAttempt. Sessions still see the old version.
 *   attempt.deliver(...)  stores chunks; the final chunk auto-publishes.
 *   attempt.abort()       releases the attempt's slots, published version
 *                         untouched.
 *   openSession()         pins the currently published version.
 *
 * When a newer version publishes, the old one is retired. Its slots are
 * released only when the last session pinning it ends; until then its frozen
 * weights remain readable by those pinned sessions.
 */
export class AdapterRegistry {
  #pool;
  #published = null;
  #attempt = null;
  #nextAttemptId = 1;
  #nextGeneration = 0;
  #nextSessionId = 1;
  #sessions = new Set();
  /** version object -> { pins, label } for every live (published or retired) version. */
  #registrations = new Map();
  #retired = [];
  #publishLog = [];
  #events = [];

  constructor({ slots }) {
    if (!Number.isInteger(slots) || slots < 1) {
      throw new RangeError('registry slots must be a positive integer');
    }
    this.#pool = new SlotPool(slots);
  }

  get published() {
    return this.#published;
  }

  get publishedVersion() {
    return this.#published === null ? null : this.#published.version;
  }

  /** Generation index of the published version (a version may be republished). */
  get publishedGeneration() {
    return this.#published === null ? null : this.#published.generation;
  }

  get activeAttempt() {
    return this.#attempt;
  }

  get pool() {
    return this.#pool;
  }

  get sessionCount() {
    return this.#sessions.size;
  }

  /** Retired versions still held alive by pinned sessions. */
  get retiredVersions() {
    return this.#retired.map((r) => ({
      version: r.version.version,
      generation: r.version.generation,
      pins: this.#registrations.get(r.version).pins,
    }));
  }

  get events() {
    return [...this.#events];
  }

  publishLog() {
    return [...this.#publishLog];
  }

  pinsFor(version) {
    const reg = this.#registrations.get(version);
    return reg === undefined ? 0 : reg.pins;
  }

  #emit(type, detail) {
    this.#events.push(Object.freeze({ type, ...detail }));
  }

  /**
   * Admit a load. Every slot the version declares is reserved in one atomic
   * step: a load that cannot fit never starts, and the published version
   * keeps serving. Only one load may be in flight.
   */
  beginLoad(manifest) {
    const blockIds = requiredBlockIds(manifest);
    const slotCount = requiredSlotCount(manifest);
    if (this.#attempt !== null) {
      throw new LoadInFlightError(this.#attempt.id, this.#attempt.version);
    }
    const owner = `load:${this.#nextAttemptId}`;
    this.#pool.reserve(owner, slotCount);
    const attempt = new LoadAttempt({
      registry: this,
      id: this.#nextAttemptId++,
      manifest,
      blockIds,
      slotOwner: owner,
    });
    this.#attempt = attempt;
    this.#emit('load-started', {
      attemptId: attempt.id,
      version: attempt.version,
      slotCount,
    });
    return attempt;
  }

  /** Abort an in-flight load. Returns false when the id is not current. */
  abortLoad(attemptId, reason = 'aborted by caller') {
    const attempt = this.#attempt;
    if (attempt === null || attempt.id !== attemptId) return false;
    this.#fail(attempt, reason);
    return true;
  }

  /** Deliver a chunk by attempt id. */
  deliver(attemptId, blockId, weights) {
    const attempt = this.#attempt;
    if (attempt === null || attempt.id !== attemptId) {
      // Reject before touching state: a late chunk from a dead attempt can
      // never be applied to the current attempt.
      throw new StaleChunkError(attemptId, blockId);
    }
    return this.#deliverTo(attempt, blockId, weights);
  }

  #deliverTo(attempt, blockId, weights) {
    if (!attempt.blockIds.includes(blockId)) {
      const err = new UnknownBlockError(attempt.id, blockId);
      this.#fail(attempt, err.message);
      throw err;
    }
    if (attempt.receivedBlockIds.includes(blockId)) {
      const err = new DuplicateChunkError(attempt.id, blockId);
      this.#fail(attempt, err.message);
      throw err;
    }
    if (!Array.isArray(weights) || weights.length === 0) {
      const err = new TypeError(`block ${blockId} weights must be a non-empty array`);
      this.#fail(attempt, err.message);
      throw err;
    }
    for (const w of weights) {
      if (!Number.isInteger(w)) {
        const err = new TypeError(`block ${blockId} weights must be integers`);
        this.#fail(attempt, err.message);
        throw err;
      }
    }
    if (attempt.dim !== null && weights.length !== attempt.dim) {
      const err = new RangeError(
        `block ${blockId} dimension ${weights.length} != ${attempt.dim}`,
      );
      this.#fail(attempt, err.message);
      throw err;
    }
    attempt._store(blockId, weights);
    if (attempt.isComplete) this.#complete(attempt);
    return true;
  }

  #fail(attempt, reason) {
    if (this.#attempt !== attempt) return;
    attempt._abort(reason);
    this.#pool.release(attempt.slotOwner);
    this.#attempt = null;
    this.#emit('load-aborted', {
      attemptId: attempt.id,
      version: attempt.version,
      reason,
    });
  }

  #complete(attempt) {
    if (this.#attempt !== attempt) return;
    const blocks = attempt._snapshotBlocks();
    attempt._commit();
    const generation = this.#nextGeneration++;
    const next = new AdapterVersion({
      version: attempt.version,
      blocks,
      blockIds: attempt.blockIds,
      slotCount: attempt.manifest.slotCount,
      generation,
    });
    const previous = this.#published;
    // The attempt's reservation becomes the published generation's.
    this.#pool.transfer(attempt.slotOwner, `published:${generation}`);
    this.#registrations.set(next, { pins: 0, label: `published:${generation}` });
    this.#attempt = null;
    this.#published = next;
    if (previous !== null) {
      this.#retired.push({ version: previous });
      this.#emit('version-retired', {
        version: previous.version,
        generation: previous.generation,
        pins: this.pinsFor(previous),
      });
      this.#maybeReclaim(previous);
    }
    this.#publishLog.push(
      Object.freeze({ version: next.version, generation: next.generation }),
    );
    this.#emit('version-published', {
      version: next.version,
      generation: next.generation,
      slotCount: next.slotCount,
      previous: previous === null ? null : previous.version,
    });
  }

  /** Release a retired version's slots once no session pins it. */
  #maybeReclaim(version) {
    const reg = this.#registrations.get(version);
    if (reg === undefined || reg.pins > 0) return false;
    this.#pool.release(reg.label);
    this.#registrations.delete(version);
    const i = this.#retired.findIndex((r) => r.version === version);
    if (i !== -1) this.#retired.splice(i, 1);
    this.#emit('version-reclaimed', {
      version: version.version,
      generation: version.generation,
    });
    return true;
  }

  #unpin(session) {
    if (!this.#sessions.delete(session)) return;
    const version = session.adapter;
    const reg = this.#registrations.get(version);
    if (reg === undefined) return;
    reg.pins -= 1;
    this.#emit('session-ended', {
      sessionId: session.id,
      version: version.version,
      pins: reg.pins,
    });
    if (version !== this.#published) this.#maybeReclaim(version);
  }

  /**
   * Open a session bound to the version published right now. The binding is
   * permanent: later publishes cannot change what this session scores with.
   */
  openSession() {
    if (this.#published === null) {
      throw new Error('no published adapter version');
    }
    const session = new Session(this, this.#nextSessionId++, this.#published);
    this.#registrations.get(this.#published).pins += 1;
    this.#sessions.add(session);
    return session;
  }

  /** Registry hook used by Session.end(). */
  _endSession(session) {
    return this.#unpin(session);
  }
}

/**
 * A scoring session. Captures one AdapterVersion at open time and uses only
 * that version's frozen weights for every score, regardless of how many
 * newer versions publish in between.
 */
export class Session {
  #id;
  #registry;
  #version;
  #closed = false;
  #scoreCount = 0;

  constructor(registry, id, version) {
    this.#registry = registry;
    this.#id = id;
    this.#version = version;
    Object.seal(this);
  }

  get id() {
    return this.#id;
  }

  /** Numeric version pinned at open time. */
  get version() {
    return this.#version.version;
  }

  /** Generation pinned at open time (distinguishes republished versions). */
  get generation() {
    return this.#version.generation;
  }

  get closed() {
    return this.#closed;
  }

  get scoreCount() {
    return this.#scoreCount;
  }

  /** The pinned AdapterVersion object (frozen). */
  get adapter() {
    return this.#version;
  }

  score(input) {
    if (this.#closed) throw new Error(`session ${this.#id} is closed`);
    validateVector(input, 'input');
    const result = this.#version.score(input);
    this.#scoreCount += 1;
    return { sessionId: this.#id, generation: this.#version.generation, ...result };
  }

  end() {
    if (this.#closed) return false;
    this.#closed = true;
    this.#registry._endSession(this);
    return true;
  }
}
