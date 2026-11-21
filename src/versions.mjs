import { scoreVersion } from './scoring.mjs';

/**
 * An immutable, fully-loaded adapter version.
 *
 * A version never mutates after construction: publishing swaps references,
 * it never edits weights in place. Any session that captured this object
 * keeps reading the same frozen weights forever, regardless of how many
 * newer versions publish afterwards.
 */
export class AdapterVersion {
  #version;
  #blocks;
  #blockIds;
  #dim;
  #slotCount;
  #generation;

  constructor({ version, blocks, blockIds, slotCount, generation }) {
    this.#version = version;
    this.#blockIds = Object.freeze([...blockIds]);
    this.#slotCount = slotCount;
    this.#generation = generation;
    const frozen = new Map();
    for (const id of this.#blockIds) {
      frozen.set(id, Object.freeze([...blocks.get(id)]));
    }
    this.#blocks = frozen;
    this.#dim = frozen.get(this.#blockIds[0]).length;
    Object.freeze(this);
  }

  /** Numeric version declared by the manifest. */
  get version() {
    return this.#version;
  }

  /** Unique id of this publish generation (a version may be republished). */
  get generation() {
    return this.#generation;
  }

  get blockIds() {
    return this.#blockIds;
  }

  get dim() {
    return this.#dim;
  }

  get slotCount() {
    return this.#slotCount;
  }

  hasBlock(id) {
    return this.#blocks.has(id);
  }

  /** Frozen integer weights for one block (undefined if not in this version). */
  block(id) {
    return this.#blocks.get(id);
  }

  /**
   * Score an input with this version's weights, in ascending block order.
   * Returns `{ version, score, blocks }`, `blocks` being the per-block
   * breakdown `[{ blockId, weights, contribution }]`.
   */
  score(input) {
    const { score, blocks } = scoreVersion(this.#blocks, this.#blockIds, input);
    return { version: this.#version, score, blocks };
  }
}

/**
 * A load in progress: holds a slot reservation, the block ids it must
 * receive, and the chunks received so far. Chunks are accepted only while
 * the attempt is the registry's current attempt, so a chunk belonging to a
 * dead attempt can never be folded into a later retry.
 */
export class LoadAttempt {
  #id;
  #manifest;
  #blockIds;
  #blocks = new Map();
  #status = 'loading';
  #abortReason = null;
  #dim = null;
  #registry;
  #slotOwner;

  constructor({ registry, id, manifest, blockIds, slotOwner }) {
    this.#registry = registry;
    this.#id = id;
    this.#manifest = manifest;
    this.#blockIds = Object.freeze([...blockIds]);
    this.#slotOwner = slotOwner;
  }

  get id() {
    return this.#id;
  }

  /** Slot-pool reservation label held by this attempt while loading. */
  get slotOwner() {
    return this.#slotOwner;
  }

  get version() {
    return this.#manifest.version;
  }

  get manifest() {
    return this.#manifest;
  }

  get blockIds() {
    return this.#blockIds;
  }

  get status() {
    return this.#status;
  }

  get abortReason() {
    return this.#abortReason;
  }

  /** Vector dimension established by the first chunk, or null. */
  get dim() {
    return this.#dim;
  }

  get receivedBlockIds() {
    return [...this.#blocks.keys()].sort((a, b) => a - b);
  }

  get isComplete() {
    return this.#blocks.size === this.#blockIds.length;
  }

  /** Deliver one weight chunk for this attempt; delegates to the registry. */
  deliver(blockId, weights) {
    return this.#registry.deliver(this.#id, blockId, weights);
  }

  /** Abort this attempt, releasing its slots. Delegates to the registry. */
  abort(reason) {
    return this.#registry.abortLoad(this.#id, reason);
  }

  /** Registry-only: accept a validated chunk. */
  _store(blockId, weights) {
    if (this.#dim === null) this.#dim = weights.length;
    this.#blocks.set(blockId, weights);
  }

  _abort(reason) {
    if (this.#status === 'loading') {
      this.#status = 'aborted';
      this.#abortReason = reason;
    }
    this.#blocks.clear();
  }

  _commit() {
    this.#status = 'published';
  }

  _snapshotBlocks() {
    if (!this.isComplete) {
      throw new Error(`attempt ${this.#id} is incomplete`);
    }
    return this.#blocks;
  }
}
