export class SlotExhaustedError extends Error {
  constructor(needed, free) {
    super(`cannot admit load: needs ${needed} slots, ${free} free`);
    this.name = 'SlotExhaustedError';
    this.needed = needed;
    this.free = free;
  }
}

export class LoadInFlightError extends Error {
  constructor(attemptId, version) {
    super(
      `cannot start a new load: attempt ${attemptId} for version ${version} is still in flight`,
    );
    this.name = 'LoadInFlightError';
    this.attemptId = attemptId;
    this.version = version;
  }
}

export class StaleChunkError extends Error {
  constructor(attemptId, blockId) {
    super(
      `chunk block ${blockId} arrived for non-current attempt ${attemptId}; ignored`,
    );
    this.name = 'StaleChunkError';
    this.attemptId = attemptId;
    this.blockId = blockId;
  }
}

export class DuplicateChunkError extends Error {
  constructor(attemptId, blockId) {
    super(`chunk block ${blockId} delivered twice for attempt ${attemptId}`);
    this.name = 'DuplicateChunkError';
    this.attemptId = attemptId;
    this.blockId = blockId;
  }
}

export class UnknownBlockError extends Error {
  constructor(attemptId, blockId) {
    super(`chunk block ${blockId} was not declared by attempt ${attemptId}`);
    this.name = 'UnknownBlockError';
    this.attemptId = attemptId;
    this.blockId = blockId;
  }
}

export function validateVersionManifest(manifest) {
  if (manifest === null || typeof manifest !== 'object') {
    throw new TypeError('version manifest must be an object');
  }
  if (!Number.isInteger(manifest.version) || manifest.version < 0) {
    throw new TypeError('version manifest.version must be a non-negative integer');
  }
  if (!Number.isInteger(manifest.slotCount) || manifest.slotCount < 1) {
    throw new TypeError('version manifest.slotCount must be a positive integer');
  }
  if (!Array.isArray(manifest.blocks) || manifest.blocks.length === 0) {
    throw new TypeError('version manifest.blocks must be a non-empty array');
  }
  const seen = new Set();
  for (const id of manifest.blocks) {
    if (!Number.isInteger(id) || id < 0) {
      throw new TypeError('version manifest.blocks must contain non-negative integers');
    }
    if (seen.has(id)) {
      throw new TypeError(`version manifest.blocks contains duplicate block ${id}`);
    }
    seen.add(id);
  }
  return manifest;
}

export function validateVector(vector, label = 'vector') {
  if (!Array.isArray(vector)) {
    throw new TypeError(`${label} must be an array`);
  }
  for (const x of vector) {
    if (typeof x !== 'number' || !Number.isFinite(x)) {
      throw new TypeError(`${label} must contain only finite numbers`);
    }
  }
  return vector;
}
