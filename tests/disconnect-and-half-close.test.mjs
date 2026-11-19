import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  createTlsMessageReader,
  MESSAGE_KINDS,
  COMPLETED_BY,
  TRUNCATION_REASONS,
  REJECTION_CODES,
} from '../src/index.mjs';

describe('abrupt disconnect', () => {
  it('keeps confirmed messages and truncates the in-flight one', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 3, token: 'kept' });
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 8, token: 'cut' });

    r.push(Buffer.from('abc'));
    r.push(Buffer.from('XY'));
    r.disconnect();

    const confirmed = r.takeConfirmed();
    assert.equal(confirmed.length, 1, 'only the completed message survives');
    assert.equal(confirmed[0].token, 'kept');
    assert.equal(confirmed[0].data.toString(), 'abc');

    const snap = r.snapshot();
    assert.equal(snap.truncated.length, 1);
    assert.equal(snap.truncated[0].token, 'cut');
    assert.equal(snap.truncated[0].bytesSeen, 2);
    assert.equal(snap.truncated[0].expectedLength, 8);
    assert.equal(snap.truncated[0].reason, TRUNCATION_REASONS.TRANSPORT_DISCONNECT);
    assert.equal(snap.transportDisconnected, true);
  });

  it('never confirms the partially received message', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 4, token: 'partial' });

    r.push(Buffer.from('abc'));
    r.disconnect();

    assert.deepEqual(r.takeConfirmed(), []);
    assert.equal(r.snapshot().truncated[0].token, 'partial');
  });

  it('truncates an in-flight until-close message instead of completing it', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'tail' });

    r.push(Buffer.from('never finished'));
    r.disconnect(new Error('ECONNRESET'));

    assert.deepEqual(r.takeConfirmed(), [], 'close_notify never arrived, so no confirmation');
    const [trunc] = r.snapshot().truncated;
    assert.equal(trunc.token, 'tail');
    assert.equal(trunc.kind, MESSAGE_KINDS.UNTIL_CLOSE);
    assert.equal(trunc.bytesSeen, 'never finished'.length);
    assert.equal(trunc.reason, TRUNCATION_REASONS.TRANSPORT_ERROR);
    assert.equal(r.snapshot().transportError.message, 'ECONNRESET');
  });

  it('records nothing truncated when the disconnect lands on a frame boundary', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 2, token: 'whole' });

    r.push(Buffer.from('ok'));
    r.disconnect();

    const snap = r.snapshot();
    assert.equal(snap.confirmed.length, 1);
    assert.deepEqual(snap.truncated, []);
  });

  it('rejects data and declarations once the transport is gone', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 2, token: 'a' });
    r.push(Buffer.from('o'));
    r.disconnect();

    assert.throws(
      () => r.push(Buffer.from('k')),
      (err) => err.code === REJECTION_CODES.ALREADY_TRUNCATED,
    );
    assert.throws(
      () => r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 1, token: 'b' }),
      (err) => err.code === REJECTION_CODES.ALREADY_TRUNCATED,
    );
    assert.equal(r.snapshot().truncated[0].token, 'a');
  });

  it('rejects close_notify that arrives after the disconnect', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'tail' });
    r.disconnect();

    assert.throws(
      () => r.peerCloseNotify(),
      (err) => err.code === REJECTION_CODES.ALREADY_TRUNCATED,
    );
    assert.deepEqual(r.takeConfirmed(), []);
  });

  it('keeps the first disconnect reason when disconnect fires twice', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 4, token: 'a' });
    r.push(Buffer.from('ab'));
    r.disconnect();
    r.disconnect(new Error('late error'));

    const snap = r.snapshot();
    assert.equal(snap.truncated.length, 1);
    assert.equal(snap.truncated[0].reason, TRUNCATION_REASONS.TRANSPORT_DISCONNECT);
    assert.equal(snap.transportError, null);
  });
});

describe('half-close directions', () => {
  it('keeps reading after the local side stops writing', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 2, token: 'a' });
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 2, token: 'b' });

    r.localStopWriting();
    assert.equal(r.canRead(), true);
    assert.equal(r.canWrite(), false);

    r.push(Buffer.from('hi'));
    assert.deepEqual(r.takeConfirmed().map((m) => m.token), ['a']);

    r.push(Buffer.from('yo'));
    assert.deepEqual(r.takeConfirmed().map((m) => m.token), ['b']);
    assert.equal(r.snapshot().localWriteStopped, true);
    assert.equal(r.snapshot().peerClosed, false);
  });

  it('does not let a local half-close truncate an open message', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'tail' });
    r.push(Buffer.from('partial'));
    r.localStopWriting();

    assert.deepEqual(r.takeConfirmed(), []);
    assert.deepEqual(r.snapshot().truncated, []);
    assert.equal(r.snapshot().open.bytesSeen, 7);

    r.push(Buffer.from(' more'));
    r.peerCloseNotify();
    assert.equal(r.takeConfirmed()[0].data.toString(), 'partial more');
  });

  it('allows the business to finish its response after the peer half-closed', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'request' });
    r.push(Buffer.from('please'));

    r.peerCloseNotify();

    const snap = r.snapshot();
    assert.equal(snap.confirmed[0].token, 'request');
    assert.equal(snap.peerClosed, true);
    assert.equal(snap.localWriteStopped, false);
    assert.equal(r.canWrite(), true, 'peer half-close must not forbid our reply');
    assert.equal(r.canRead(), false, 'peer will send no more data');

    // Finishing our own side afterwards must not disturb what already confirmed.
    r.localStopWriting();
    assert.equal(r.snapshot().confirmed.length, 1);
    assert.deepEqual(r.snapshot().truncated, []);
  });

  it('stops accepting data after the peer half-closed', () => {
    const r = createTlsMessageReader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 2, token: 'a' });
    r.push(Buffer.from('hi'));
    r.peerCloseNotify();

    assert.throws(
      () => r.push(Buffer.from('yo')),
      (err) => err.code === REJECTION_CODES.PEER_CLOSED,
    );
    assert.throws(
      () => r.peerCloseNotify(),
      (err) => err.code === REJECTION_CODES.PEER_CLOSED,
    );
    assert.throws(
      () => r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 1, token: 'late' }),
      (err) => err.code === REJECTION_CODES.PEER_CLOSED,
    );
  });
});
