/**
 * TLS 1.3 event adapter — message reading layer.
 *
 * Input is already-decrypted, in-order, authenticated TLS 1.3 application data
 * (what remains after the record layer verifies and strips protection). The
 * layer answers exactly one question for the business above it: *which business
 * message is complete now?*
 *
 * Two message classes share one direction of the stream:
 *
 *   - `length`     — declares its own size. Confirmed the moment exactly that
 *                    many bytes have arrived. It never waits for close_notify.
 *   - `untilClose` — consumes every remaining byte of that direction and is
 *                    confirmed only by the peer's close_notify.
 *
 * Events this layer consumes:
 *
 *   - application data       `push(buffer)`        — in order, authenticated.
 *   - peer close_notify      `peerCloseNotify()`   — ends the peer's write
 *                                                    direction only.
 *   - local stop writing     `localStopWriting()`  — ends our write direction
 *                                                    only; reading continues.
 *   - transport disconnect   `disconnect(error?)`  — abrupt loss. Confirmed
 *                                                    messages are kept; an open
 *                                                    message is marked truncated.
 *
 * Invariants, and why they are the invariants:
 *
 *   1. A message is confirmed only for the frame boundary it actually reached.
 *      A length message completed by data is never re-labelled by a later
 *      close_notify, even one in the same tick — "already complete" must not
 *      depend on how events happen to be batched.
 *   2. close_notify never manufactures bytes. An incomplete length message at
 *      close_notify is a protocol fault, reported rather than completed.
 *   3. An abrupt disconnect confirms nothing. It keeps what was already
 *      confirmed and marks the in-flight message `truncated`.
 *   4. Application data must be claimed by a declared message. Unclaimed bytes
 *      are a framing mismatch and are rejected, not buffered in hope that a
 *      declaration shows up later.
 *   5. Neither half-close ends the other direction. After `localStopWriting()`
 *      the reader keeps accepting and confirming inbound messages; after
 *      `peerCloseNotify()` the business can still finish and flush its own
 *      response (writes remain possible).
 */

import { Buffer } from 'node:buffer';

export const MESSAGE_KINDS = Object.freeze({
  LENGTH: 'length',
  UNTIL_CLOSE: 'untilClose',
});

/** How a confirmed message reached completion. */
export const COMPLETED_BY = Object.freeze({
  APPLICATION_DATA: 'application_data',
  CLOSE_NOTIFY: 'close_notify',
});

/** Why an in-flight message never completed. */
export const TRUNCATION_REASONS = Object.freeze({
  TRANSPORT_DISCONNECT: 'transport_disconnect',
  TRANSPORT_ERROR: 'transport_error',
  CLOSE_NOTIFY_INCOMPLETE: 'close_notify_incomplete',
  BUFFER_LIMIT: 'buffer_limit',
});

/** Stable codes for rejected transitions and protocol faults. */
export const REJECTION_CODES = Object.freeze({
  PEER_CLOSED: 'PEER_CLOSED',
  ALREADY_TRUNCATED: 'ALREADY_TRUNCATED',
  UNEXPECTED_DATA: 'UNEXPECTED_DATA',
  INCOMPLETE_AT_CLOSE: 'INCOMPLETE_AT_CLOSE',
  BUFFER_LIMIT: 'BUFFER_LIMIT',
  DUPLICATE_TOKEN: 'DUPLICATE_TOKEN',
  UNTIL_CLOSE_TERMINAL: 'UNTIL_CLOSE_TERMINAL',
});

const DEFAULT_MAX_BUFFERED = 1024 * 1024;
const EMPTY = Buffer.alloc(0);

export class TlsMessageReaderError extends Error {
  constructor(code, message, details) {
    super(message);
    this.name = 'TlsMessageReaderError';
    this.code = code;
    if (details) Object.assign(this, details);
  }
}

function rejection(code, message, details) {
  return new TlsMessageReaderError(code, message, details);
}

function normalizeDescriptor(descriptor, fallbackToken) {
  if (descriptor === null || typeof descriptor !== 'object') {
    throw new TypeError('a message descriptor object is required');
  }

  let token = fallbackToken;
  if (descriptor.token !== undefined) {
    if (typeof descriptor.token !== 'string' || descriptor.token.length === 0) {
      throw new TypeError('descriptor.token must be a non-empty string when given');
    }
    token = descriptor.token;
  }

  if (descriptor.kind === MESSAGE_KINDS.LENGTH) {
    const { length } = descriptor;
    if (!Number.isSafeInteger(length) || length < 0) {
      throw new TypeError(`length messages need a non-negative safe integer length, got ${String(length)}`);
    }
    return Object.freeze({ kind: MESSAGE_KINDS.LENGTH, length, token });
  }

  if (descriptor.kind === MESSAGE_KINDS.UNTIL_CLOSE) {
    return Object.freeze({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token });
  }

  throw new TypeError(`unsupported message kind: ${String(descriptor.kind)}`);
}

/**
 * Creates a reader for one TLS connection direction.
 *
 * @param {object} [options]
 * @param {number} [options.maxBuffered] Bytes a single message may buffer
 *   before `BUFFER_LIMIT` is raised. Defaults to 1 MiB.
 */
export function createTlsMessageReader(options = {}) {
  const maxBuffered = options.maxBuffered ?? DEFAULT_MAX_BUFFERED;
  if (!Number.isSafeInteger(maxBuffered) || maxBuffered < 1) {
    throw new TypeError('maxBuffered must be a positive safe integer');
  }

  /** Descriptors declared but not yet started, in stream order. */
  const descriptors = [];
  /** The message currently being assembled, or null. */
  let active = null;

  const confirmed = [];
  const completedByCloseNotify = [];
  const truncated = [];
  const skippedDescriptors = [];
  const usedTokens = new Set();

  let autoTokenCounter = 0;

  let localWriteStopped = false;
  let peerClosed = false;
  let closeNotifyCount = 0;
  let transportDisconnected = false;
  let transportError = null;

  function nextAutoToken() {
    for (;;) {
      autoTokenCounter += 1;
      const candidate = `msg-${autoTokenCounter}`;
      if (!usedTokens.has(candidate)) return candidate;
    }
  }

  function completeActive(completedBy) {
    const { descriptor, chunks, size } = active;
    const data = chunks.length === 0 ? EMPTY : chunks.length === 1 ? chunks[0] : Buffer.concat(chunks, size);
    const record = Object.freeze({
      token: descriptor.token,
      kind: descriptor.kind,
      data,
      bytes: size,
      completedBy,
    });
    confirmed.push(record);
    active = null;
    return record;
  }

  function recordTruncated(reason) {
    if (!active) return null;
    const { descriptor, size } = active;
    const record = Object.freeze({
      token: descriptor.token,
      kind: descriptor.kind,
      reason,
      bytesSeen: size,
      ...(descriptor.kind === MESSAGE_KINDS.LENGTH ? { expectedLength: descriptor.length } : {}),
    });
    truncated.push(record);
    active = null;
    return record;
  }

  function discardDescriptors() {
    while (descriptors.length > 0) skippedDescriptors.push(descriptors.shift());
  }

  /** Start the next declared message, if none is active. */
  function startNext() {
    const descriptor = descriptors.shift();
    active = { descriptor, chunks: [], size: 0 };
    if (descriptor.kind === MESSAGE_KINDS.LENGTH && descriptor.length === 0) {
      // A zero-length frame is complete the instant its turn arrives.
      completeActive(COMPLETED_BY.APPLICATION_DATA);
    }
  }

  /**
   * Advance through declarations that need no further bytes (zero-length
   * messages), stopping at the first one that does. Declarations are ordered,
   * so this only ever moves forward.
   */
  function settle() {
    while (active === null && descriptors.length > 0 && descriptors[0].kind === MESSAGE_KINDS.LENGTH) {
      if (descriptors[0].length !== 0) break;
      startNext();
    }
  }

  /**
   * Frame `data` into the schema. Either every byte is consumed into messages,
   * or UNEXPECTED_DATA is thrown for the bytes no declaration claims.
   */
  function frameBytes(data) {
    let remainder = data;

    while (remainder.length > 0) {
      if (active === null) {
        if (descriptors.length === 0) {
          throw rejection(
            REJECTION_CODES.UNEXPECTED_DATA,
            `${remainder.length} application-data byte(s) are not claimed by any declared message`,
            { bytes: remainder.length },
          );
        }
        startNext();
        continue;
      }

      if (active.descriptor.kind === MESSAGE_KINDS.UNTIL_CLOSE) {
        active.chunks.push(Buffer.from(remainder));
        active.size += remainder.length;
        remainder = EMPTY;
        if (active.size > maxBuffered) {
          const { token } = active.descriptor;
          const bytesSeen = active.size;
          recordTruncated(TRUNCATION_REASONS.BUFFER_LIMIT);
          throw rejection(REJECTION_CODES.BUFFER_LIMIT, `untilClose message exceeded maxBuffered (${maxBuffered})`, {
            token,
            bytesSeen,
            maxBuffered,
          });
        }
        break;
      }

      const need = active.descriptor.length - active.size;
      if (remainder.length < need) {
        active.chunks.push(Buffer.from(remainder));
        active.size += remainder.length;
        remainder = EMPTY;
        break;
      }

      if (need > 0) active.chunks.push(Buffer.from(remainder.subarray(0, need)));
      active.size += need;
      remainder = remainder.subarray(need);
      completeActive(COMPLETED_BY.APPLICATION_DATA);
    }

    settle();
  }

  function assertInboundOpen(what) {
    if (transportDisconnected) {
      throw rejection(REJECTION_CODES.ALREADY_TRUNCATED, `cannot accept ${what}: the transport already disconnected`, {
        transportError,
      });
    }
    if (peerClosed) {
      throw rejection(REJECTION_CODES.PEER_CLOSED, `cannot accept ${what}: the peer already sent close_notify`, {
        closeNotifyCount,
      });
    }
  }

  const reader = {
    /**
     * Declare the next message of this direction and return its token.
     * Declarations are strictly ordered and append to the schema queue.
     */
    declare(descriptor) {
      const normalized = normalizeDescriptor(descriptor, nextAutoToken());

      if (transportDisconnected) {
        skippedDescriptors.push(normalized);
        throw rejection(REJECTION_CODES.ALREADY_TRUNCATED, `cannot declare "${normalized.token}": transport disconnected`, {
          token: normalized.token,
          transportError,
        });
      }
      if (peerClosed) {
        skippedDescriptors.push(normalized);
        throw rejection(REJECTION_CODES.PEER_CLOSED, `cannot declare "${normalized.token}": peer sent close_notify`, {
          token: normalized.token,
          closeNotifyCount,
        });
      }
      if (usedTokens.has(normalized.token)) {
        throw rejection(REJECTION_CODES.DUPLICATE_TOKEN, `token "${normalized.token}" is already in use`, {
          token: normalized.token,
        });
      }

      const tail = descriptors.length > 0 ? descriptors[descriptors.length - 1] : active ? active.descriptor : null;
      if (tail && tail.kind === MESSAGE_KINDS.UNTIL_CLOSE) {
        // untilClose takes the rest of the direction, so nothing can follow it.
        skippedDescriptors.push(normalized);
        throw rejection(
          REJECTION_CODES.UNTIL_CLOSE_TERMINAL,
          `cannot declare "${normalized.token}" after untilClose message "${tail.token}"`,
          { token: normalized.token, after: tail.token },
        );
      }

      usedTokens.add(normalized.token);
      descriptors.push(normalized);
      settle();
      return normalized.token;
    },

    /** Feed authenticated, in-order application data. */
    push(data) {
      if (!Buffer.isBuffer(data)) {
        throw new TypeError('application data must be a Buffer');
      }
      if (data.length === 0) return reader;

      assertInboundOpen('application data');

      if (active === null && descriptors.length === 0) {
        throw rejection(
          REJECTION_CODES.UNEXPECTED_DATA,
          'application data arrived without a declared message to claim it',
          { bytes: data.length },
        );
      }

      frameBytes(data);
      return reader;
    },

    /**
     * The peer sent close_notify: its write direction is over. This is the only
     * event that can confirm an untilClose message.
     *
     * An open length message short of its declared size is a protocol fault:
     * it is recorded as truncated and reported as INCOMPLETE_AT_CLOSE.
     */
    peerCloseNotify() {
      if (transportDisconnected) {
        throw rejection(REJECTION_CODES.ALREADY_TRUNCATED, 'close_notify arrived after the transport disconnected', {
          transportError,
        });
      }
      if (peerClosed) {
        throw rejection(REJECTION_CODES.PEER_CLOSED, 'the peer already sent close_notify');
      }

      peerClosed = true;
      closeNotifyCount += 1;

      let fault = null;

      // Zero-length frames that were already declared settle before we decide
      // what close_notify completes.
      settle();

      if (active) {
        if (active.descriptor.kind === MESSAGE_KINDS.UNTIL_CLOSE) {
          completedByCloseNotify.push(completeActive(COMPLETED_BY.CLOSE_NOTIFY));
        } else {
          const { descriptor, size } = active;
          recordTruncated(TRUNCATION_REASONS.CLOSE_NOTIFY_INCOMPLETE);
          fault = rejection(
            REJECTION_CODES.INCOMPLETE_AT_CLOSE,
            `close_notify arrived with "${descriptor.token}" incomplete (${size}/${descriptor.length} bytes)`,
            { token: descriptor.token, bytesSeen: size, expectedLength: descriptor.length },
          );
        }
      } else if (descriptors.length > 0 && descriptors[0].kind === MESSAGE_KINDS.UNTIL_CLOSE) {
        // Declared but never started: the remainder of the direction is empty,
        // so close_notify completes it as a zero-byte message.
        startNext();
        completedByCloseNotify.push(completeActive(COMPLETED_BY.CLOSE_NOTIFY));
      } else if (descriptors.length > 0) {
        // A declared length message that received no bytes at all. It is just
        // as incomplete as a partially received one; silently skipping it would
        // let the business mistake "never arrived" for "nothing to read".
        // (Zero-length declarations already settled above.)
        const descriptor = descriptors.shift();
        fault = rejection(
          REJECTION_CODES.INCOMPLETE_AT_CLOSE,
          `close_notify arrived before "${descriptor.token}" started (0/${descriptor.length} bytes)`,
          { token: descriptor.token, bytesSeen: 0, expectedLength: descriptor.length },
        );
        truncated.push(
          Object.freeze({
            token: descriptor.token,
            kind: descriptor.kind,
            reason: TRUNCATION_REASONS.CLOSE_NOTIFY_INCOMPLETE,
            bytesSeen: 0,
            expectedLength: descriptor.length,
          }),
        );
      }

      discardDescriptors();

      if (fault) throw fault;
      return reader;
    },

    /**
     * We stopped writing (our close_notify or a local half-close). Outbound
     * traffic ends here; inbound messages keep arriving and keep being
     * confirmed.
     */
    localStopWriting() {
      localWriteStopped = true;
      return reader;
    },

    /** The transport died (reset, timeout, unexpected EOF). */
    disconnect(error) {
      if (transportDisconnected) return reader;
      transportDisconnected = true;
      transportError = error ?? null;

      // Already-confirmed messages stay confirmed; the in-flight one is cut.
      recordTruncated(transportError ? TRUNCATION_REASONS.TRANSPORT_ERROR : TRUNCATION_REASONS.TRANSPORT_DISCONNECT);
      discardDescriptors();
      return reader;
    },

    /** Remove and return every message confirmed since the last call. */
    takeConfirmed() {
      return confirmed.splice(0, confirmed.length);
    },

    /** Read-only view of the reader's state. Never mutates. */
    snapshot() {
      const open = active
        ? Object.freeze({
            token: active.descriptor.token,
            kind: active.descriptor.kind,
            bytesSeen: active.size,
            ...(active.descriptor.kind === MESSAGE_KINDS.LENGTH
              ? { expectedLength: active.descriptor.length }
              : {}),
          })
        : null;

      return Object.freeze({
        confirmed: Object.freeze(confirmed.slice()),
        completedByCloseNotify: Object.freeze(completedByCloseNotify.slice()),
        truncated: Object.freeze(truncated.slice()),
        skippedDescriptors: Object.freeze(skippedDescriptors.slice()),
        pendingDescriptors: Object.freeze(descriptors.slice()),
        open,
        localWriteStopped,
        peerClosed,
        closeNotifyCount,
        transportDisconnected,
        transportError,
      });
    },

    /** Inbound application data is still acceptable. */
    canRead() {
      return !peerClosed && !transportDisconnected;
    },

    /** The business can still send its own response. */
    canWrite() {
      return !localWriteStopped && !transportDisconnected;
    },
  };

  return reader;
}

export default createTlsMessageReader;
