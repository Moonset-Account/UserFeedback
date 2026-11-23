/**
 * Error types used by the federated executor.
 *
 * TimeoutError: thrown (or returned) by a batch read callback to signal that
 * the whole batch did not answer in time. The executor either converts it
 * into per-item errors (timeoutPolicy: 'error') or aborts and emits a
 * resumable checkpoint (timeoutPolicy: 'checkpoint').
 *
 * StitchError: raised when the fields required to assemble one entity
 * occurrence would have to be stitched from revision reads that no declared
 * co-readable revision vector covers — i.e. the assembled entity never
 * existed under any single consistent snapshot.
 */
export class TimeoutError extends Error {
  constructor(message = 'batch read timed out') {
    super(message);
    this.name = 'TimeoutError';
  }
}

export class StitchError extends Error {
  constructor(message, evidence = undefined) {
    super(message);
    this.name = 'StitchError';
    this.evidence = evidence;
  }
}
