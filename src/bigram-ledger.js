/**
 * Retractable bigram candidate ledger for managed terminals.
 *
 * Model
 * -----
 * The caller supplies an ordered stream of tokens. Every token references a
 * source segment id; each segment carries a caller-provided session id and
 * privacy flag.
 *
 * - Every pair of *adjacent* tokens in the raw stream is considered once, in
 *   stream order.
 * - A pair contributes one count to its bigram candidate only when both
 *   endpoint segments are public at the time the pair is appended and both
 *   belong to the same session.
 * - Segment boundaries themselves are not barriers: the last token of one
 *   segment and the first token of the next are adjacent and may form a
 *   cross-segment bigram.
 * - A session change or a private segment between two tokens breaks the pair.
 *   A private segment is a barrier, not a filter: tokens on its two sides
 *   never become adjacent just because its tokens were withheld.
 * - Marking a segment private after the fact retracts every historical
 *   contribution whose endpoint belongs to that segment, including
 *   contributions formed across segment boundaries. Contributions to the same
 *   bigram from other public sources stay intact.
 * - Retraction never recomputes adjacency, so no new pair is created by
 *   removing a segment.
 *
 * Events are recorded so a ledger can be rebuilt with `replay(events)`.
 * `buildFromScratch()` constructs the same ledger from the final privacy
 * flags directly; the two must agree.
 */

export const EVENT_DEFINE_SEGMENT = 'define-segment';
export const EVENT_APPEND_TOKENS = 'append-tokens';
export const EVENT_MARK_PRIVATE = 'mark-private';

/**
 * @typedef {object} SegmentDefinition
 * @property {string} id
 * @property {string} session
 * @property {boolean} [private]
 * @property {string[]} [tokens]
 */

/**
 * @typedef {object} TokenInput
 * @property {string} text
 * @property {string} segment
 */

function assertNonEmptyString(value, label) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new TypeError(`${label} must be a non-empty string`);
  }
}

function assertDefinitionShape(definition) {
  if (definition === null || typeof definition !== 'object') {
    throw new TypeError('segment definition must be an object');
  }
  assertNonEmptyString(definition.id, 'segment.id');
  assertNonEmptyString(definition.session, 'segment.session');
}

function normalizePrivateFlag(value) {
  if (value === undefined) return false;
  if (typeof value !== 'boolean') {
    throw new TypeError('segment.private must be a boolean when provided');
  }
  return value;
}

function compareCandidates(a, b) {
  if (a.bigram[0] !== b.bigram[0]) return a.bigram[0] < b.bigram[0] ? -1 : 1;
  if (a.bigram[1] !== b.bigram[1]) return a.bigram[1] < b.bigram[1] ? -1 : 1;
  return 0;
}

export class BigramLedger {
  /** @type {Map<string, { id: string, session: string, private: boolean }>} */
  #segments = new Map();
  /** @type {{ text: string, segmentId: string }[]} */
  #stream = [];
  /** @type {{ prev: string, next: string, segPrev: string, segNext: string, active: boolean }[]} */
  #contributions = [];
  /** @type {Map<string, Map<string, number>>} */
  #counts = new Map();
  /** @type {object[]} */
  #events = [];

  /** Number of registered segments. */
  get segmentCount() {
    return this.#segments.size;
  }

  /** Number of tokens appended so far. */
  get tokenCount() {
    return this.#stream.length;
  }

  /**
   * Register segment metadata without appending tokens.
   * The same id may not be registered twice.
   */
  defineSegment(definition) {
    assertDefinitionShape(definition);
    const { id, session } = definition;
    const isPrivate = normalizePrivateFlag(definition.private);
    if (this.#segments.has(id)) {
      throw new Error(`duplicate segment id: ${id}`);
    }
    this.#segments.set(id, { id, session, private: isPrivate });
    this.#events.push({
      type: EVENT_DEFINE_SEGMENT,
      id,
      session,
      private: isPrivate,
    });
    return this;
  }

  /**
   * Append ordered tokens to the stream. Each token must reference an
   * already-registered segment. Adjacency with the previous stream tail is
   * evaluated immediately; retracting a segment later never re-evaluates it.
   *
   * @param {TokenInput[]} tokens
   */
  append(tokens) {
    if (!Array.isArray(tokens)) {
      throw new TypeError('tokens must be an array');
    }
    const normalized = tokens.map((entry, index) => {
      if (entry === null || typeof entry !== 'object') {
        throw new TypeError(`tokens[${index}] must be an object`);
      }
      assertNonEmptyString(entry.text, `tokens[${index}].text`);
      assertNonEmptyString(entry.segment, `tokens[${index}].segment`);
      if (!this.#segments.has(entry.segment)) {
        throw new Error(`unknown segment for tokens[${index}]: ${entry.segment}`);
      }
      return { text: entry.text, segmentId: entry.segment };
    });

    let previous = this.#stream.at(-1) ?? null;
    for (const current of normalized) {
      if (previous !== null) {
        this.#considerPair(previous, current);
      }
      this.#stream.push(current);
      previous = current;
    }

    this.#events.push({
      type: EVENT_APPEND_TOKENS,
      tokens: normalized.map((token) => ({ text: token.text, segment: token.segmentId })),
    });
    return this;
  }

  /**
   * Convenience: register a segment and append its tokens contiguously.
   *
   * @param {SegmentDefinition} definition
   */
  addSegment(definition) {
    assertDefinitionShape(definition);
    if (definition.tokens !== undefined && !Array.isArray(definition.tokens)) {
      throw new TypeError('segment.tokens must be an array');
    }
    const { tokens = [] } = definition;
    this.defineSegment({
      id: definition.id,
      session: definition.session,
      private: definition.private,
    });
    this.append(tokens.map((text, index) => {
      assertNonEmptyString(text, `segment.tokens[${index}]`);
      return { text, segment: definition.id };
    }));
    return this;
  }

  /**
   * Convenience: ingest contiguous segment blocks in order.
   *
   * @param {SegmentDefinition[]} definitions
   */
  ingest(definitions) {
    if (!Array.isArray(definitions)) {
      throw new TypeError('segments must be an array');
    }
    for (const definition of definitions) {
      this.addSegment(definition);
    }
    return this;
  }

  /**
   * Convenience: ingest a flat transcript whose tokens already carry source
   * segment ids.
   *
   * @param {{ segments: SegmentDefinition[], tokens: TokenInput[] }} transcript
   */
  ingestTranscript(transcript) {
    if (transcript === null || typeof transcript !== 'object') {
      throw new TypeError('transcript must be an object');
    }
    const { segments, tokens } = transcript;
    if (!Array.isArray(segments)) {
      throw new TypeError('transcript.segments must be an array');
    }
    if (!Array.isArray(tokens)) {
      throw new TypeError('transcript.tokens must be an array');
    }
    for (const segment of segments) {
      this.defineSegment({
        id: segment.id,
        session: segment.session,
        private: segment.private,
      });
    }
    this.append(tokens);
    return this;
  }

  /**
   * Mark a segment private. All active contributions involving it are
   * retracted; adjacency is not recomputed, so neighbours are never joined.
   * Repeating the call is a no-op.
   */
  markPrivate(segmentId) {
    assertNonEmptyString(segmentId, 'segmentId');
    const segment = this.#segments.get(segmentId);
    if (segment === undefined) {
      throw new Error(`unknown segment: ${segmentId}`);
    }
    if (segment.private) {
      return this;
    }
    segment.private = true;

    for (const contribution of this.#contributions) {
      if (!contribution.active) continue;
      if (contribution.segPrev !== segmentId && contribution.segNext !== segmentId) continue;
      contribution.active = false;
      this.#bump(contribution.prev, contribution.next, -1);
    }

    this.#events.push({ type: EVENT_MARK_PRIVATE, id: segmentId });
    return this;
  }

  /** Current count for a bigram, regardless of zero rows. */
  countFor(prev, next) {
    return this.#counts.get(prev)?.get(next) ?? 0;
  }

  /** True when the segment exists and is currently private. */
  isPrivate(segmentId) {
    const segment = this.#segments.get(segmentId);
    if (segment === undefined) {
      throw new Error(`unknown segment: ${segmentId}`);
    }
    return segment.private;
  }

  /**
   * Candidate bigrams with positive counts, sorted deterministically by
   * (first token, second token).
   *
   * @returns {{ bigram: [string, string], count: number }[]}
   */
  query() {
    const candidates = [];
    for (const [prev, row] of this.#counts) {
      for (const [next, count] of row) {
        if (count > 0) {
          candidates.push({ bigram: [prev, next], count });
        }
      }
    }
    candidates.sort(compareCandidates);
    return candidates;
  }

  /** Copy of the recorded event log. */
  events() {
    return this.#events.map((event) => {
      if (event.type === EVENT_APPEND_TOKENS) {
        return {
          type: event.type,
          tokens: event.tokens.map((token) => ({ ...token })),
        };
      }
      return { ...event };
    });
  }

  #considerPair(previous, current) {
    const prevSegment = this.#segments.get(previous.segmentId);
    const nextSegment = this.#segments.get(current.segmentId);
    // Privacy is read live: a segment retracted earlier cannot contribute a
    // pair with a segment appended later.
    if (prevSegment.private || nextSegment.private) return;
    if (prevSegment.session !== nextSegment.session) return;

    this.#contributions.push({
      prev: previous.text,
      next: current.text,
      segPrev: previous.segmentId,
      segNext: current.segmentId,
      active: true,
    });
    this.#bump(previous.text, current.text, 1);
  }

  #bump(prev, next, delta) {
    let row = this.#counts.get(prev);
    if (row === undefined) {
      row = new Map();
      this.#counts.set(prev, row);
    }
    const nextCount = (row.get(next) ?? 0) + delta;
    if (nextCount > 0) {
      row.set(next, nextCount);
    } else {
      row.delete(next);
    }
    if (row.size === 0) {
      this.#counts.delete(prev);
    }
  }
}

/**
 * Rebuild a ledger by replaying a recorded event log in order.
 *
 * @param {object[]} events
 */
export function replay(events) {
  if (!Array.isArray(events)) {
    throw new TypeError('events must be an array');
  }
  const ledger = new BigramLedger();
  for (const [index, event] of events.entries()) {
    if (event === null || typeof event !== 'object') {
      throw new TypeError(`events[${index}] must be an object`);
    }
    switch (event.type) {
      case EVENT_DEFINE_SEGMENT:
        ledger.defineSegment({
          id: event.id,
          session: event.session,
          private: event.private,
        });
        break;
      case EVENT_APPEND_TOKENS:
        ledger.append(event.tokens);
        break;
      case EVENT_MARK_PRIVATE:
        ledger.markPrivate(event.id);
        break;
      default:
        throw new Error(`unknown event type at events[${index}]: ${String(event.type)}`);
    }
  }
  return ledger;
}

/**
 * Build a ledger directly from the final privacy flags, without any
 * retraction step. This is the reference answer that a replayed ledger must
 * match.
 *
 * @param {{ segments: SegmentDefinition[], tokens: TokenInput[] }} transcript
 * @param {{ privateSegmentIds?: string[] }} [options]
 */
export function buildFromScratch(transcript, options = {}) {
  if (transcript === null || typeof transcript !== 'object') {
    throw new TypeError('transcript must be an object');
  }
  const { segments, tokens } = transcript;
  const { privateSegmentIds = [] } = options;
  if (!Array.isArray(segments)) {
    throw new TypeError('transcript.segments must be an array');
  }
  if (!Array.isArray(tokens)) {
    throw new TypeError('transcript.tokens must be an array');
  }
  if (!Array.isArray(privateSegmentIds)) {
    throw new TypeError('privateSegmentIds must be an array');
  }

  const finalPrivate = new Set(privateSegmentIds);
  const ledger = new BigramLedger();
  for (const segment of segments) {
    ledger.defineSegment({
      id: segment.id,
      session: segment.session,
      private: segment.private === true || finalPrivate.has(segment.id),
    });
  }
  ledger.append(tokens);
  return ledger;
}
