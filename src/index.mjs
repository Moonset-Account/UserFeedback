/**
 * Public entry point.
 *
 * `createTlsMessageReader` is the read layer itself: declarative schema queue +
 * push(bytes) + lifecycle events. `createTlsEventReader` is the thin adapter
 * that turns the TLS 1.3 adapter's named events into those calls.
 */

export {
  createTlsMessageReader,
  TlsMessageReaderError,
  MESSAGE_KINDS,
  COMPLETED_BY,
  TRUNCATION_REASONS,
  REJECTION_CODES,
} from './tls-message-reader.mjs';

export { createTlsEventReader, EVENT_TYPES } from './event-adapter.mjs';

export { default } from './tls-message-reader.mjs';
