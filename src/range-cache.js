// Edge node byte-range cache.
//
// A representation is identified by (resource path, content encoding, ETag
// validator). Partial responses carry a total length and a CLOSED byte
// interval [start, end].
//
// Strong-validator fragments of the same path + encoding + ETag may be stitched
// together. Overlapping bytes must be identical; a length mismatch or a byte
// conflict invalidates the representation until a trusted complete response
// rebuilds it.
//
// Weak-validator fragments are stored individually. They never stitch: a query
// only hits a weak fragment when that single fragment fully covers the
// requested range; when several qualify, the most recently stored one wins.

const IDENTITY = '';

/**
 * Parse an ETag header value.
 * @param {string} value
 * @returns {{weak: boolean, opaque: string}}
 */
export function parseETag(value) {
  if (typeof value !== 'string') {
    throw new TypeError('etag must be a string');
  }
  const trimmed = value.trim();
  let weak = false;
  let body = trimmed;
  if (body.startsWith('W/') || body.startsWith('w/')) {
    weak = true;
    body = body.slice(2);
  }
  if (body.length < 2 || body[0] !== '"' || body[body.length - 1] !== '"') {
    throw new RangeError(`malformed ETag: ${value}`);
  }
  const opaque = body.slice(1, -1);
  // RFC 7232: opaque-tag = DQUOTE *etagc DQUOTE, etagc excludes DQUOTE and 0x7F.
  for (let i = 0; i < opaque.length; i += 1) {
    const code = opaque.charCodeAt(i);
    if (code === 0x22 || code === 0x7f) {
    throw new RangeError(`malformed ETag: ${value}`);
    }
  }
  return { weak, opaque };
}

function normalizeEncoding(encoding) {
  if (encoding === undefined || encoding === null) return IDENTITY;
  if (typeof encoding !== 'string') {
    throw new TypeError('encoding must be a string');
  }
  return encoding.toLowerCase();
}

function asBuffer(data) {
  if (Buffer.isBuffer(data)) return data;
  if (data instanceof Uint8Array) return Buffer.from(data);
  if (typeof data === 'string') return Buffer.from(data, 'utf8');
  throw new TypeError('data must be a Buffer, Uint8Array or string');
}

function validateRange(start, end, totalLength, dataLength) {
  if (!Number.isInteger(start) || !Number.isInteger(end)) {
    throw new RangeError('start and end must be integers');
  }
  if (start < 0 || end < start) {
    throw new RangeError('range must satisfy 0 <= start <= end');
  }
  if (!Number.isInteger(totalLength) || totalLength <= 0) {
    throw new RangeError('totalLength must be a positive integer');
  }
  if (end >= totalLength) {
    throw new RangeError('range [start, end] must lie within totalLength');
  }
  if (dataLength !== end - start + 1) {
    throw new RangeError('data length does not match closed interval [start, end]');
  }
}

function makeKey(path, encoding, opaque) {
  return `${path}\u0000${encoding}\u0000${opaque}`;
}

// A strong-validator representation: one sparse byte space of `totalLength`
// bytes plus a coverage bitmap. Once invalidated it accepts no further
// fragments and answers no queries until a trusted complete response rebuilds.
class StrongRepresentation {
  constructor(totalLength) {
    this.totalLength = totalLength;
    this.bytes = Buffer.alloc(totalLength);
    this.present = new Uint8Array(totalLength);
    this.coveredCount = 0;
    this.invalid = false;
    this.invalidReason = null;
    this.rebuilt = false;
  }

  // Write a closed interval. Returns 'stored', 'merged' or 'invalidated'.
  write(start, end, chunk) {
    if (this.invalid) return 'rejected-invalid';
    let allMatched = true;
    for (let offset = start; offset <= end; offset += 1) {
      const value = chunk[offset - start];
      if (this.present[offset]) {
        if (this.bytes[offset] !== value) {
          this.invalidate(`byte conflict at offset ${offset}`);
          return 'invalidated';
        }
      } else {
        allMatched = false;
        this.bytes[offset] = value;
        this.present[offset] = 1;
        this.coveredCount += 1;
      }
    }
    return allMatched ? 'merged' : 'stored';
  }

  invalidate(reason) {
    this.invalid = true;
    this.invalidReason = reason;
    this.present.fill(0);
    this.coveredCount = 0;
    // Drop the cached payload; the buffer stays allocated to preserve length.
    this.bytes.fill(0);
  }

  // Rebuild from a trusted full-length response.
  rebuild(fullData) {
    this.bytes = Buffer.from(fullData);
    this.present = new Uint8Array(this.totalLength).fill(1);
    this.coveredCount = this.totalLength;
    this.invalid = false;
    this.invalidReason = null;
    this.rebuilt = true;
  }

  // Scan a closed request range into alternating present/missing runs.
  scan(requestStart, requestEnd) {
    const hits = [];
    const gaps = [];
    let index = requestStart;
    while (index <= requestEnd) {
      const covered = this.present[index] === 1;
      const runStart = index;
      while (index <= requestEnd && (this.present[index] === 1) === covered) {
        index += 1;
      }
      const runEnd = index - 1;
      if (covered) {
        hits.push({ start: runStart, end: runEnd });
      } else {
        gaps.push({ start: runStart, end: runEnd });
      }
    }
    return { hits, gaps };
  }

  state() {
    return {
      totalLength: this.totalLength,
      coveredCount: this.coveredCount,
      complete: this.coveredCount === this.totalLength,
      invalid: this.invalid,
      invalidReason: this.invalidReason,
      rebuilt: this.rebuilt,
    };
  }
}

export class RangeCache {
  constructor() {
    /** @type {Map<string, StrongRepresentation>} */
    this._strong = new Map();
    /** @type {Map<string, Array<{seq:number,totalLength:number,start:number,end:number,data:Buffer}>} */
    this._weak = new Map();
    this._seq = 0;
  }

  /**
   * Store a partial (or complete) response fragment.
   *
   * Options:
   *   etag:          ETag header value (required)
   *   encoding:      content-encoding; undefined/null/'identity' mean identity
   *   totalLength:   full representation length in bytes (required)
   *   start, end:    closed byte interval of `data` (required for fragments)
   *   trusted:       only meaningful together with a full-length payload;
   *                  rebuilds an invalidated strong representation
   *
   * @returns {{status: string, reason?: string}}
   */
  put(path, data, options) {
    if (typeof path !== 'string' || path.length === 0) {
      throw new TypeError('path must be a non-empty string');
    }
    if (!options || typeof options !== 'object') {
      throw new TypeError('options are required');
    }
    const { weak, opaque } = parseETag(options.etag);
    const encoding = normalizeEncoding(options.encoding);
    const totalLength = options.totalLength;
    const payload = asBuffer(data);
    const key = makeKey(path, encoding, opaque);

    const isFullPayload = payload.length === totalLength;
    const start = options.start ?? (isFullPayload ? 0 : undefined);
    const end = options.end ?? (isFullPayload ? totalLength - 1 : undefined);
    validateRange(start, end, totalLength, payload.length);

    if (weak) {
      return this._putWeak(key, totalLength, start, end, payload);
    }
    return this._putStrong(key, totalLength, start, end, payload, {
      trusted: options.trusted === true,
      isFullPayload,
    });
  }

  _putWeak(key, totalLength, start, end, data) {
    const list = this._weak.get(key) ?? [];
    this._seq += 1;
    list.push({
      seq: this._seq,
      totalLength,
      start,
      end,
      data: Buffer.from(data),
    });
    this._weak.set(key, list);
    return { status: 'stored', validator: 'weak' };
  }

  _putStrong(key, totalLength, start, end, data, { trusted, isFullPayload }) {
    const existing = this._strong.get(key);

    // A trusted complete response rebuilds the representation regardless of
    // its previous state (it is the only way out of invalidation).
    if (isFullPayload && trusted) {
      const rep = existing ?? new StrongRepresentation(totalLength);
      if (rep.totalLength !== totalLength) {
        throw new RangeError(
          'trusted full response totalLength differs from the cached representation length',
        );
      }
      rep.rebuild(data);
      this._strong.set(key, rep);
      return { status: 'rebuilt', validator: 'strong' };
    }

    if (!existing) {
      const rep = new StrongRepresentation(totalLength);
      const writeStatus = rep.write(start, end, data);
      this._strong.set(key, rep);
      return { status: writeStatus, validator: 'strong' };
    }

    if (existing.totalLength !== totalLength) {
      existing.invalidate(
        `totalLength conflict: cached ${existing.totalLength} vs incoming ${totalLength}`,
      );
      return {
        status: 'invalidated',
        reason: existing.invalidReason,
        validator: 'strong',
      };
    }

    if (existing.invalid) {
      return {
        status: 'rejected-invalid',
        reason: existing.invalidReason,
        validator: 'strong',
      };
    }

    const writeStatus = existing.write(start, end, data);
    return {
      status: writeStatus,
      reason: writeStatus === 'invalidated' ? existing.invalidReason : undefined,
      validator: 'strong',
    };
  }

  /**
   * Query a closed byte range.
   *
   * Returns { hit, encoding, etag, totalLength, hits, gaps, data }.
   * `hit` is true only when the whole range is covered. `data` is the cached
   * bytes concatenated in request-range order (null when nothing is cached).
   * Strong runs may be partial; weak entries hit only on full single-fragment
   * coverage.
   */
  query(path, options) {
    if (typeof path !== 'string' || path.length === 0) {
      throw new TypeError('path must be a non-empty string');
    }
    if (!options || typeof options !== 'object') {
      throw new TypeError('options are required');
    }
    const { weak, opaque } = parseETag(options.etag);
    const encoding = normalizeEncoding(options.encoding);
    const { start, end } = options;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start) {
      throw new RangeError('range must satisfy 0 <= start <= end with integer bounds');
    }
    const key = makeKey(path, encoding, opaque);

    if (weak) {
      return this._queryWeak(key, options.etag.trim(), encoding, start, end);
    }
    return this._queryStrong(key, options.etag.trim(), encoding, start, end);
  }

  _queryStrong(key, etag, encoding, start, end) {
    const rep = this._strong.get(key);
    const base = {
      hit: false,
      validator: 'strong',
      encoding,
      etag,
      totalLength: rep ? rep.totalLength : null,
      hits: [],
      gaps: [{ start, end }],
      data: null,
    };
    if (!rep || rep.invalid) {
      base.reason = rep?.invalid ? rep.invalidReason : 'no cached representation';
      return base;
    }
    if (end >= rep.totalLength) {
      base.reason = 'range exceeds representation totalLength';
      base.totalLength = rep.totalLength;
      return base;
    }

    const { hits, gaps } = rep.scan(start, end);
    const chunks = hits.map((run) =>
      Buffer.from(rep.bytes.subarray(run.start, run.end + 1)),
    );
    return {
      hit: gaps.length === 0,
      validator: 'strong',
      encoding,
      etag,
      totalLength: rep.totalLength,
      hits,
      gaps,
      data: chunks.length ? Buffer.concat(chunks) : null,
    };
  }

  _queryWeak(key, etag, encoding, start, end) {
    const list = this._weak.get(key) ?? [];
    const base = {
      hit: false,
      validator: 'weak',
      encoding,
      etag,
      totalLength: null,
      hits: [],
      gaps: [{ start, end }],
      data: null,
    };

    // Latest stored fragment that fully covers the request range wins.
    let winner = null;
    for (let i = list.length - 1; i >= 0; i -= 1) {
      const fragment = list[i];
      if (
        fragment.start <= start &&
        fragment.end >= end &&
        fragment.totalLength > end
      ) {
        winner = fragment;
        break;
      }
    }
    if (!winner) return base;

    const slice = Buffer.from(
      winner.data.subarray(start - winner.start, end - winner.start + 1),
    );
    return {
      hit: true,
      validator: 'weak',
      encoding,
      etag,
      totalLength: winner.totalLength,
      hits: [{ start, end }],
      gaps: [],
      data: slice,
    };
  }

  /**
   * Inspect a strong representation's state (primarily for verification).
   * Returns null when the representation has never been seen.
   */
  getState(path, options) {
    const { weak, opaque } = parseETag(options.etag);
    if (weak) return null;
    const encoding = normalizeEncoding(options.encoding);
    const rep = this._strong.get(makeKey(path, encoding, opaque));
    return rep ? rep.state() : null;
  }
}

export default RangeCache;
