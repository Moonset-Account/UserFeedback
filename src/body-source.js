'use strict';

/**
 * Build a reopenable in-memory content source.
 *
 * A content source is the body abstraction used by the auth challenge layer:
 *   - open(): returns a fresh async iterable of Uint8Array chunks and, for a
 *     finite source, the total byte length. Every retry must call open()
 *     again; chunks emitted by a previous open() are never reused.
 *   - reopenable: true marks that the caller can safely replay the body.
 *
 * `chunks` may be a string, a Uint8Array, or an array of those.
 */
export function createReopenableSource(chunks, { label = 'reopenable-body' } = {}) {
  const frames = normalize(chunks);
  let openCount = 0;

  return {
    label,
    reopenable: true,
    idempotencyKey: undefined, // set by caller when the server knows the key
    get openCount() {
      return openCount;
    },
    get byteLength() {
      return frames.reduce((sum, c) => sum + c.byteLength, 0);
    },
    async *open() {
      openCount += 1;
      // Yield copies so a consumer cannot mutate the stored frames between
      // attempts.
      for (const frame of frames) {
        yield frame.slice();
      }
    },
  };
}

/**
 * Wrap a one-shot source (e.g. a stream that can only be consumed once).
 * open() works exactly once; a second call throws, proving to tests that the
 * client must never silently replay a non-reopenable body.
 */
export function createOneShotSource(chunks, { label = 'oneshot-body' } = {}) {
  const frames = normalize(chunks);
  let consumed = false;
  let openCount = 0;

  return {
    label,
    reopenable: false,
    get openCount() {
      return openCount;
    },
    get byteLength() {
      return frames.reduce((sum, c) => sum + c.byteLength, 0);
    },
    async *open() {
      if (consumed) {
        throw new Error(`content source "${label}" can only be opened once`);
      }
      openCount += 1;
      // Only mark consumed once a real frame is requested. An empty one-shot
      // source yields nothing, so opening it again cannot replay any byte and
      // stays allowed (the zero-byte boundary case).
      let yielded = false;
      for (const frame of frames) {
        yielded = true;
        yield frame.slice();
      }
      if (yielded) consumed = true;
    },
  };
}

/**
 * Empty source: open() yields nothing, byteLength 0. Used for bodyless
 * requests and for boundary tests around "no bytes sent yet".
 */
export function createEmptySource() {
  return createReopenableSource([], { label: 'empty-body' });
}

function normalize(chunks) {
  const list = Array.isArray(chunks) ? chunks : [chunks];
  return list.map((c) => {
    if (c instanceof Uint8Array) return c;
    if (typeof c === 'string') return new TextEncoder().encode(c);
    throw new TypeError('body chunks must be string or Uint8Array');
  });
}

/**
 * Drain an opened source into an array of chunks and count the bytes. The
 * transport uses this in tests to observe exactly how many bytes the client
 * sent on each attempt.
 */
export async function collectSource(openedIterable) {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of openedIterable) {
    chunks.push(chunk);
    bytes += chunk.byteLength;
  }
  return { chunks, bytes };
}
