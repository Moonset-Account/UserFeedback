// Incremental HTTP/1.1 response reader.
//
// The caller registers the sequence of request methods sent on one keep-alive
// connection, then feeds raw response bytes (possibly split across arbitrary
// boundaries) via push(). Completed responses are handed back through the
// onResponse callback. end() marks a clean connection close; an early close
// leaves every previously completed response intact and exposes the request
// that was still in flight through `unfinished`.

import { Buffer } from 'node:buffer';

const CR = 0x0d;
const LF = 0x0a;

// Parser states.
const STATUS = 'status';
const HEADERS = 'headers';
const BODY_FIXED = 'body-fixed';
const CHUNK_SIZE = 'chunk-size';
const CHUNK_DATA = 'chunk-data';
const CHUNK_DATA_CRLF = 'chunk-data-crlf';
const CHUNK_TRAILER = 'chunk-trailer';

/**
 * Case-insensitive lookup of a header value.
 * Returns the first match or undefined. Works on both response headers and
 * the [name, value] tuple arrays produced by the reader.
 * @param {Array<[string, string]>} headers
 * @param {string} name
 * @returns {string|undefined}
 */
export function findHeader(headers, name) {
  const wanted = name.toLowerCase();
  for (const [key, value] of headers) {
    if (key.toLowerCase() === wanted) return value;
  }
  return undefined;
}

export class HttpResponseReader {
  #buffer = Buffer.alloc(0);
  #requests = []; // registered request methods, upper-cased
  #requestIndex = 0; // request the next response belongs to
  #state = STATUS;
  #entry = null; // response currently being assembled
  #stateBytesLeft = 0; // remaining bytes for BODY_FIXED / CHUNK_DATA
  #stateChunks = []; // raw data chunks for the current fixed/chunked body
  #interimForCurrent = []; // 1xx responses belonging to the in-flight request
  #ended = false;
  #onResponse;
  #onInterim;

  /**
   * @param {object} [options]
   * @param {(response: object) => void} [options.onResponse]
   *        Called once per final response, in request order.
   * @param {(response: object) => void} [options.onInterim]
   *        Called for every 1xx (100/103) interim response. The request is
   *        not consumed by an interim response.
   */
  constructor(options = {}) {
    this.#onResponse = options.onResponse;
    this.#onInterim = options.onInterim;
  }

  /** Methods of the registered requests, in pipeline order. */
  get requests() {
    return [...this.#requests];
  }

  /** Number of final responses already completed. */
  get completedCount() {
    return this.#requestIndex;
  }

  /**
   * The registered request that was still in flight when end() was called
   * early, or null when the connection ended cleanly (or before any data).
   * A request counts as unfinished as soon as its final response starts
   * (status line received) before it completes, and any later registered
   * request whose response never started is reported too via results gaps.
   * @returns {{index: number, method: string}|null}
   */
  get unfinished() {
    if (this.#entry !== null) {
      return { index: this.#entry.requestIndex, method: this.#entry.method };
    }
    if (this.#ended && this.#requestIndex < this.#requests.length) {
      return {
        index: this.#requestIndex,
        method: this.#requests[this.#requestIndex],
      };
    }
    return null;
  }

  /**
   * Register a request method before its response bytes arrive.
   * @param {string} method HTTP method, e.g. 'HEAD' or 'GET'
   * @returns {number} index assigned to this request
   */
  registerRequest(method) {
    if (this.#ended) throw new Error('cannot register requests after end()');
    if (typeof method !== 'string' || method.length === 0) {
      throw new TypeError('method must be a non-empty string');
    }
    this.#requests.push(method.toUpperCase());
    return this.#requests.length - 1;
  }

  /**
   * Feed raw response bytes. Any iterable of bytes (Buffer/Uint8Array/TypedArray)
   * is accepted. Bytes may cut through status lines, header lines, chunk-size
   * lines or anywhere else.
   * @param {Uint8Array|Buffer} chunk
   */
  push(chunk) {
    if (this.#ended) throw new Error('push() called after end()');
    if (chunk === undefined || chunk === null || chunk.length === 0) return;
    this.#buffer = this.#buffer.length === 0
      ? Buffer.from(chunk)
      : Buffer.concat([this.#buffer, Buffer.from(chunk)]);
    this.#pump();
  }

  /**
   * Mark the end of the connection. Throws when bytes remain that do not form
   * a complete response; completed responses are always preserved.
   */
  end() {
    if (this.#ended) throw new Error('end() called more than once');
    this.#ended = true;
    const pending = this.#entry !== null
      || this.#buffer.length > 0
      || this.#requestIndex < this.#requests.length;
    if (pending) {
      const target = this.#entry
        ? `request #${this.#entry.requestIndex} (${this.#entry.method})`
        : `request #${this.#requestIndex} (${this.#requests[this.#requestIndex] ?? '?'})`;
      throw new Error(`connection closed with an incomplete response for ${target}`);
    }
  }

  // Remove and return one CRLF-terminated line (without CRLF), or null if the
  // buffer does not contain a complete line yet.
  #takeLine() {
    const idx = this.#buffer.indexOf('\r\n');
    if (idx === -1) return null;
    const line = this.#buffer.subarray(0, idx);
    this.#buffer = this.#buffer.subarray(idx + 2);
    return line;
  }

  #pump() {
    while (this.#buffer.length > 0) {
      if (this.#state === STATUS) {
        const line = this.#takeLine();
        if (line === null) return; // wait for the rest of the status line
        if (this.#requestIndex >= this.#requests.length) {
          throw new Error('received a response for an unregistered request');
        }
        const text = line.toString('latin1');
        const match = /^HTTP\/\d+\.\d+ (\d{3})(?: (.*))?$/.exec(text);
        if (!match) throw new Error(`malformed status line: ${JSON.stringify(text)}`);
        this.#entry = {
          requestIndex: this.#requestIndex,
          method: this.#requests[this.#requestIndex],
          statusCode: Number(match[1]),
          reasonPhrase: match[2] ?? '',
          headers: [],
          body: new Uint8Array(0),
          trailers: [],
          interim: this.#interimForCurrent,
        };
        this.#state = HEADERS;
      } else if (this.#state === HEADERS) {
        const line = this.#takeLine();
        if (line === null) return; // header line / final CRLF split across feeds
        if (line.length === 0) {
          // Empty line: header section finished. Every path inside either
          // completes the response or switches to a body-parsing state.
          this.#afterHeaders();
        } else {
          const text = line.toString('latin1');
          const colon = text.indexOf(':');
          if (colon === -1) throw new Error(`malformed header line: ${JSON.stringify(text)}`);
          const name = text.slice(0, colon);
          // OWS handling: optional whitespace immediately around the value.
          const value = text.slice(colon + 1).replace(/^[ \t]+|[ \t]+$/g, '');
          this.#entry.headers.push([name, value]);
        }
      } else if (this.#state === BODY_FIXED) {
        const take = Math.min(this.#stateBytesLeft, this.#buffer.length);
        this.#stateChunks.push(this.#buffer.subarray(0, take));
        this.#buffer = this.#buffer.subarray(take);
        this.#stateBytesLeft -= take;
        if (this.#stateBytesLeft === 0) {
          this.#entry.body = Buffer.concat(this.#stateChunks);
          this.#finishResponse();
        }
      } else if (this.#state === CHUNK_SIZE) {
        const line = this.#takeLine();
        if (line === null) return; // hex length line split across feeds
        const text = line.toString('latin1');
        const hex = text.split(';', 1)[0].trim();
        if (!/^[0-9a-fA-F]+$/.test(hex)) {
          throw new Error(`malformed chunk size line: ${JSON.stringify(text)}`);
        }
        const size = Number.parseInt(hex, 16);
        if (size === 0) {
          this.#state = CHUNK_TRAILER;
        } else {
          this.#stateBytesLeft = size;
          this.#state = CHUNK_DATA;
        }
      } else if (this.#state === CHUNK_DATA) {
        const take = Math.min(this.#stateBytesLeft, this.#buffer.length);
        this.#stateChunks.push(this.#buffer.subarray(0, take));
        this.#buffer = this.#buffer.subarray(take);
        this.#stateBytesLeft -= take;
        if (this.#stateBytesLeft === 0) this.#state = CHUNK_DATA_CRLF;
      } else if (this.#state === CHUNK_DATA_CRLF) {
        // Chunk data is followed by a mandatory CRLF.
        if (this.#buffer.length < 2) return;
        if (this.#buffer[0] !== CR || this.#buffer[1] !== LF) {
          throw new Error('missing CRLF after chunk data');
        }
        this.#buffer = this.#buffer.subarray(2);
        this.#state = CHUNK_SIZE;
      } else if (this.#state === CHUNK_TRAILER) {
        const line = this.#takeLine();
        if (line === null) return; // trailer line / terminating CRLF split
        if (line.length === 0) {
          this.#entry.body = Buffer.concat(this.#stateChunks);
          this.#finishResponse();
        } else {
          const text = line.toString('latin1');
          const colon = text.indexOf(':');
          if (colon === -1) throw new Error(`malformed trailer line: ${JSON.stringify(text)}`);
          const name = text.slice(0, colon);
          const value = text.slice(colon + 1).replace(/^[ \t]+|[ \t]+$/g, '');
          this.#entry.trailers.push([name, value]);
        }
      }
    }
  }

  #afterHeaders() {
    const entry = this.#entry;

    // 1xx: interim response, delivered to the current request but the request
    // is NOT consumed — stay on the same request index. Snapshot the interim
    // list so the interim entry does not alias the accumulating array.
    if (entry.statusCode >= 100 && entry.statusCode < 200) {
      entry.interim = entry.interim.slice();
      this.#interimForCurrent.push(entry);
      this.#onInterim?.(entry);
      this.#entry = null;
      this.#state = STATUS;
      return;
    }

    // HEAD responses, 204 and 304 have no message body regardless of any
    // Content-Length they carry: the response completes at the end of headers.
    const head = entry.method === 'HEAD'
      || entry.statusCode === 204
      || entry.statusCode === 304;
    if (head) {
      this.#finishResponse();
      return;
    }

    const transferEncoding = findHeader(entry.headers, 'transfer-encoding');
    const isChunked = transferEncoding !== undefined
      && /(?:^|,)\s*chunked\s*(?:$|,)/i.test(transferEncoding);

    if (isChunked) {
      this.#stateChunks = [];
      this.#state = CHUNK_SIZE;
      return;
    }

    const contentLength = findHeader(entry.headers, 'content-length');
    if (contentLength !== undefined) {
      const length = Number(contentLength);
      if (!Number.isInteger(length) || length < 0) {
        throw new Error(`invalid Content-Length: ${JSON.stringify(contentLength)}`);
      }
      if (length === 0) {
        this.#finishResponse();
        return;
      }
      this.#stateChunks = [];
      this.#stateBytesLeft = length;
      this.#state = BODY_FIXED;
      return;
    }

    // No transfer framing at all: treat as an empty body (the framing header
    // itself is still preserved in entry.headers).
    this.#finishResponse();
  }

  #finishResponse() {
    const entry = this.#entry;
    this.#entry = null;
    this.#stateChunks = [];
    this.#stateBytesLeft = 0;
    this.#interimForCurrent = [];
    this.#state = STATUS;
    this.#requestIndex += 1; // final response consumes one request
    this.#onResponse?.(entry);
  }
}

export default HttpResponseReader;
