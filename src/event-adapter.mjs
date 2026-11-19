/**
 * TLS 1.3 event adapter shim.
 *
 * The adapter above us raises named events; the read layer consumes method
 * calls. This shim is the only place that knows the event vocabulary, so the
 * read layer stays free of transport-shaped naming.
 *
 * Recognised events (plain objects, `type` required):
 *
 *   { type: 'declare', kind: 'length', length: <int>, token?: string }
 *   { type: 'declare', kind: 'untilClose', token?: string }
 *   { type: 'data', bytes: Buffer }          // ordered, authenticated
 *   { type: 'peer-close-notify' }            // peer's close_notify
 *   { type: 'local-stop-writing' }           // our close_notify / half-close
 *   { type: 'disconnect', error?: Error }    // abrupt transport loss
 *
 * Anything else throws a TypeError — silently ignoring an unknown event is how
 * a close_notify gets dropped and a untilClose message never confirms.
 */

import { Buffer } from 'node:buffer';
import { createTlsMessageReader } from './tls-message-reader.mjs';

export const EVENT_TYPES = Object.freeze({
  DECLARE: 'declare',
  DATA: 'data',
  PEER_CLOSE_NOTIFY: 'peer-close-notify',
  LOCAL_STOP_WRITING: 'local-stop-writing',
  DISCONNECT: 'disconnect',
});

/**
 * @param {object} [options] Forwarded to `createTlsMessageReader`.
 */
export function createTlsEventReader(options = {}) {
  const reader = createTlsMessageReader(options);

  /** Apply one adapter event. Returns the declared token for `declare`. */
  function handle(event) {
    if (event === null || typeof event !== 'object') {
      throw new TypeError('an event object is required');
    }

    switch (event.type) {
      case EVENT_TYPES.DECLARE:
        return reader.declare({
          kind: event.kind,
          ...(event.length !== undefined ? { length: event.length } : {}),
          ...(event.token !== undefined ? { token: event.token } : {}),
        });

      case EVENT_TYPES.DATA: {
        const bytes = event.bytes;
        if (!Buffer.isBuffer(bytes)) {
          throw new TypeError("'data' events need a Buffer in `bytes`");
        }
        reader.push(bytes);
        return undefined;
      }

      case EVENT_TYPES.PEER_CLOSE_NOTIFY:
        reader.peerCloseNotify();
        return undefined;

      case EVENT_TYPES.LOCAL_STOP_WRITING:
        reader.localStopWriting();
        return undefined;

      case EVENT_TYPES.DISCONNECT:
        reader.disconnect(event.error);
        return undefined;

      default:
        throw new TypeError(`unknown TLS adapter event type: ${String(event.type)}`);
    }
  }

  /** Apply a batch of events in order; returns whatever `handle` returned. */
  function handleAll(events) {
    const results = [];
    for (const event of events) results.push(handle(event));
    return results;
  }

  return Object.freeze({
    handle,
    handleAll,
    reader,
    declare: reader.declare,
    push: reader.push,
    peerCloseNotify: reader.peerCloseNotify,
    localStopWriting: reader.localStopWriting,
    disconnect: reader.disconnect,
    takeConfirmed: reader.takeConfirmed,
    snapshot: reader.snapshot,
    canRead: reader.canRead,
    canWrite: reader.canWrite,
  });
}

export default createTlsEventReader;
