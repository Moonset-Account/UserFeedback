import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  createTlsMessageReader,
  createTlsEventReader,
  EVENT_TYPES,
  MESSAGE_KINDS,
  COMPLETED_BY,
  REJECTION_CODES,
} from '../src/index.mjs';

describe('interleaved framing boundaries', () => {
  it('splits a declared message exactly at the boundary across two pushes', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 4, token: 'a' });
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 4, token: 'b' });

    // First push ends mid-message; second starts by finishing 'a' and carrying
    // exactly three bytes into 'b'.
    r.push(Buffer.from('abc'));
    assert.deepEqual(r.takeConfirmed(), []);
    r.push(Buffer.from('dBCD'));

    assert.deepEqual(r.takeConfirmed().map((m) => m.token), ['a']);
    assert.equal(r.snapshot().open.token, 'b');
    assert.equal(r.snapshot().open.bytesSeen, 3);

    r.push(Buffer.from('E'));
    assert.deepEqual(r.takeConfirmed().map((m) => m.token), ['b']);
  });

  it('treats an exact-fit push as complete with nothing left over', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 3, token: 'only' });
    r.push(Buffer.from('xyz'));

    const snap = r.snapshot();
    assert.equal(snap.confirmed.length, 1);
    assert.equal(snap.open, null);
    assert.deepEqual(snap.pendingDescriptors, []);

    // A following empty push is a no-op rather than an error.
    r.push(Buffer.alloc(0));
    assert.equal(r.snapshot().confirmed.length, 1);
  });

  it('rejects a declared length message that never started when close_notify arrives', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 2, token: 'first' });
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 9, token: 'never-started' });

    r.push(Buffer.from('ok'));
    assert.throws(
      () => r.peerCloseNotify(),
      (err) => {
        assert.equal(err.code, REJECTION_CODES.INCOMPLETE_AT_CLOSE);
        assert.equal(err.token, 'never-started');
        assert.equal(err.bytesSeen, 0);
        assert.equal(err.expectedLength, 9);
        return true;
      },
    );

    assert.deepEqual(r.takeConfirmed().map((m) => m.token), ['first']);
    const [trunc] = r.snapshot().truncated;
    assert.equal(trunc.token, 'never-started');
    assert.equal(trunc.bytesSeen, 0);
  });

  it('does not confirm a length message from one byte short, even at close_notify', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 5, token: 'short' });
    r.push(Buffer.from('abcd'));

    assert.throws(
      () => r.peerCloseNotify(),
      (err) => {
        assert.equal(err.code, REJECTION_CODES.INCOMPLETE_AT_CLOSE);
        assert.equal(err.bytesSeen, 4);
        assert.equal(err.expectedLength, 5);
        return true;
      },
    );

    assert.deepEqual(r.takeConfirmed(), [], 'an incomplete length message is never confirmed');
    const snap = r.snapshot();
    assert.equal(snap.confirmed.length, 0);
    assert.equal(snap.truncated[0].token, 'short');
    assert.equal(snap.truncated[0].reason, 'close_notify_incomplete');
    assert.equal(snap.peerClosed, true, 'close_notify still ends the inbound direction');
  });

  it('mixes a complete length message and a short one, confirming only the complete one', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 2, token: 'fine' });
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 6, token: 'short' });
    r.push(Buffer.from('okyo'));

    assert.throws(
      () => r.peerCloseNotify(),
      (err) => err.code === REJECTION_CODES.INCOMPLETE_AT_CLOSE,
    );

    const confirmed = r.takeConfirmed();
    assert.deepEqual(confirmed.map((m) => m.token), ['fine']);
    assert.equal(confirmed[0].completedBy, COMPLETED_BY.APPLICATION_DATA);
    assert.equal(r.snapshot().truncated[0].token, 'short');
    assert.equal(r.snapshot().truncated[0].bytesSeen, 2);
  });

  it('copies incoming bytes so a reused caller Buffer cannot corrupt confirmed data', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 4, token: 'copy' });

    const scratch = Buffer.alloc(4);
    scratch.write('wxyz');
    r.push(scratch);
    scratch.fill(0x21); // caller reuses its buffer immediately afterwards

    assert.equal(r.takeConfirmed()[0].data.toString(), 'wxyz');
  });

  it('copies bytes buffered by an until-close message as well', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'tail' });

    const scratch = Buffer.from('keep');
    r.push(scratch);
    scratch.fill(0x3f);
    r.peerCloseNotify();

    assert.equal(r.takeConfirmed()[0].data.toString(), 'keep');
  });

  it('auto-generates tokens that never collide with explicit ones', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 1, token: 'msg-1' });

    const auto = r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 1 });

    assert.notEqual(auto, 'msg-1');
    r.push(Buffer.from('ab'));
    assert.deepEqual(r.takeConfirmed().map((m) => m.token), ['msg-1', auto]);
  });

  it('rejects data that no declared message claims', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 2, token: 'a' });

    assert.throws(
      () => r.push(Buffer.from('abEXTRA')),
      (err) => {
        assert.equal(err.code, REJECTION_CODES.UNEXPECTED_DATA);
        assert.equal(err.bytes, 5);
        return true;
      },
    );
    // The complete message before the stray bytes is still confirmed.
    assert.deepEqual(r.takeConfirmed().map((m) => m.token), ['a']);
  });

  it('rejects data arriving before any declaration', () => {
    const r = createTlsMessageReader();
    assert.throws(
      () => r.push(Buffer.from('orphan')),
      (err) => err.code === REJECTION_CODES.UNEXPECTED_DATA,
    );
  });

  it('rejects duplicate tokens so confirmations stay unambiguous', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 1, token: 'twin' });
    assert.throws(
      () => r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 1, token: 'twin' }),
      (err) => err.code === REJECTION_CODES.DUPLICATE_TOKEN,
    );
  });

  it('honours maxBuffered for an until-close message', () => {
    const r = createTlsMessageReader({ maxBuffered: 8 });
    r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'big' });

    assert.throws(
      () => r.push(Buffer.from('123456789')),
      (err) => err.code === REJECTION_CODES.BUFFER_LIMIT,
    );
    assert.deepEqual(r.takeConfirmed(), []);
    assert.equal(r.snapshot().truncated[0].reason, 'buffer_limit');
  });
});

describe('event adapter', () => {
  it('drives the reader from named TLS adapter events', () => {
    const bus = createTlsEventReader();

    bus.handle({ type: EVENT_TYPES.DECLARE, kind: MESSAGE_KINDS.LENGTH, length: 3, token: 'head' });
    bus.handle({ type: EVENT_TYPES.DECLARE, kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'tail' });

    bus.handle({ type: EVENT_TYPES.DATA, bytes: Buffer.from('abc') });
    assert.deepEqual(bus.takeConfirmed().map((m) => m.token), ['head']);

    bus.handle({ type: EVENT_TYPES.LOCAL_STOP_WRITING });
    bus.handle({ type: EVENT_TYPES.DATA, bytes: Buffer.from('body') });
    bus.handle({ type: EVENT_TYPES.PEER_CLOSE_NOTIFY });

    const [tail] = bus.takeConfirmed();
    assert.equal(tail.token, 'tail');
    assert.equal(tail.data.toString(), 'body');
    assert.equal(tail.completedBy, COMPLETED_BY.CLOSE_NOTIFY);
  });

  it('surfaces an abrupt adapter disconnect as truncation', () => {
    const bus = createTlsEventReader();
    bus.handle({ type: EVENT_TYPES.DECLARE, kind: MESSAGE_KINDS.LENGTH, length: 4, token: 'cut' });
    bus.handle({ type: EVENT_TYPES.DATA, bytes: Buffer.from('ab') });
    bus.handle({ type: EVENT_TYPES.DISCONNECT, error: new Error('socket hang up') });

    assert.deepEqual(bus.takeConfirmed(), []);
    assert.equal(bus.snapshot().truncated[0].token, 'cut');
  });

  it('rejects unknown events rather than dropping them', () => {
    const bus = createTlsEventReader();
    assert.throws(() => bus.handle({ type: 'something-new' }), TypeError);
    assert.throws(() => bus.handle({ type: EVENT_TYPES.DATA }), TypeError);
  });
});
