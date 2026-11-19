import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  createTlsMessageReader,
  MESSAGE_KINDS,
  COMPLETED_BY,
  REJECTION_CODES,
} from '../src/index.mjs';

function reader(options) {
  return createTlsMessageReader(options);
}

describe('length-declared messages', () => {
  it('confirms a message the moment its declared byte count arrives', () => {
    const r = reader();
    const token = r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 5, token: 'a' });

    r.push(Buffer.from('hel'));
    assert.deepEqual(r.takeConfirmed(), [], 'three of five bytes is not complete');

    r.push(Buffer.from('lo'));
    const confirmed = r.takeConfirmed();
    assert.equal(confirmed.length, 1);
    assert.equal(confirmed[0].token, 'a');
    assert.equal(confirmed[0].bytes, 5);
    assert.equal(confirmed[0].data.toString(), 'hello');
    assert.equal(confirmed[0].completedBy, COMPLETED_BY.APPLICATION_DATA);
    assert.equal(token, 'a');
  });

  it('reassembles one message split across many arbitrary chunks', () => {
    const r = reader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 10, token: 'split' });

    for (const piece of ['a', 'bc', '', 'defg', 'h', 'ij']) {
      r.push(Buffer.from(piece));
    }

    const [msg] = r.takeConfirmed();
    assert.equal(msg.data.toString(), 'abcdefghij');
    assert.equal(msg.bytes, 10);
  });

  it('carves several messages, including leftovers, out of one data chunk', () => {
    const r = reader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 3, token: 'one' });
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 4, token: 'two' });
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 2, token: 'three' });

    r.push(Buffer.from('abc'));
    assert.deepEqual(r.takeConfirmed().map((m) => m.token), ['one']);

    r.push(Buffer.from('defgh'));
    assert.deepEqual(r.takeConfirmed().map((m) => m.token), ['two']);
    assert.equal(r.snapshot().open.token, 'three');
    assert.equal(r.snapshot().open.bytesSeen, 1);

    r.push(Buffer.from('i'));
    const rest = r.takeConfirmed();
    assert.deepEqual(rest.map((m) => m.token), ['three']);
    assert.equal(rest[0].data.toString(), 'hi');
  });

  it('confirms a zero-length message without consuming any data', () => {
    const r = reader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 0, token: 'empty' });
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 2, token: 'next' });

    const confirmed = r.takeConfirmed();
    assert.equal(confirmed.length, 1);
    assert.equal(confirmed[0].token, 'empty');
    assert.equal(confirmed[0].bytes, 0);
    assert.equal(confirmed[0].data.length, 0);

    r.push(Buffer.from('ok'));
    assert.equal(r.takeConfirmed()[0].token, 'next');
  });

  it('does not let close_notify re-label a message data already completed', () => {
    const r = reader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 4, token: 'done' });

    r.push(Buffer.from('full'));
    r.peerCloseNotify();

    const confirmed = r.takeConfirmed();
    assert.equal(confirmed.length, 1);
    assert.equal(confirmed[0].token, 'done');
    assert.equal(confirmed[0].completedBy, COMPLETED_BY.APPLICATION_DATA);
    assert.deepEqual(r.snapshot().completedByCloseNotify, []);
  });
});

describe('until-close messages', () => {
  it('buffers every remaining byte and confirms only on close_notify', () => {
    const r = reader();
    r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'rest' });

    r.push(Buffer.from('all '));
    r.push(Buffer.from('the way'));
    assert.deepEqual(r.takeConfirmed(), [], 'no completion before the peer close_notify');
    assert.equal(r.snapshot().open.bytesSeen, 'all the way'.length);

    r.peerCloseNotify();
    const confirmed = r.takeConfirmed();
    assert.equal(confirmed.length, 1);
    assert.equal(confirmed[0].token, 'rest');
    assert.equal(confirmed[0].data.toString(), 'all the way');
    assert.equal(confirmed[0].completedBy, COMPLETED_BY.CLOSE_NOTIFY);
  });

  it('confirms an empty until-close message when close_notify arrives with no bytes', () => {
    const r = reader();
    r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'empty-tail' });

    r.peerCloseNotify();
    const [msg] = r.takeConfirmed();
    assert.equal(msg.token, 'empty-tail');
    assert.equal(msg.bytes, 0);
    assert.equal(msg.completedBy, COMPLETED_BY.CLOSE_NOTIFY);
  });

  it('starts an until-close message after a length message in the same chunk', () => {
    const r = reader();
    r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 3, token: 'head' });
    r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'body' });

    r.push(Buffer.from('abcpayload'));
    const early = r.takeConfirmed();
    assert.deepEqual(early.map((m) => m.token), ['head']);

    r.peerCloseNotify();
    const [body] = r.takeConfirmed();
    assert.equal(body.token, 'body');
    assert.equal(body.data.toString(), 'payload');
  });

  it('refuses to declare anything after an until-close message', () => {
    const r = reader();
    r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'tail' });

    assert.throws(
      () => r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 1, token: 'nope' }),
      (err) => err.code === REJECTION_CODES.UNTIL_CLOSE_TERMINAL,
    );
    assert.deepEqual(
      r.snapshot().skippedDescriptors.map((d) => d.token),
      ['nope'],
    );
  });
});
