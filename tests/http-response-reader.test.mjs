import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import {
  HttpResponseReader,
  findHeader,
} from '../src/http-response-reader.mjs';

// ---------- message builders (synthetic, locally generated) ----------

function statusLine(statusCode, reason = 'OK') {
  return `HTTP/1.1 ${statusCode}${reason ? ` ${reason}` : ''}\r\n`;
}

function headerLines(headers) {
  return headers.map(([name, value]) => `${name}: ${value}\r\n`).join('');
}

function fixedResponse({ statusCode = 200, reason, headers = [], body = Buffer.alloc(0) }) {
  return Buffer.concat([
    Buffer.from(statusLine(statusCode, reason), 'latin1'),
    Buffer.from(headerLines(headers), 'latin1'),
    Buffer.from('\r\n', 'latin1'),
    body,
  ]);
}

function chunkedResponse({
  statusCode = 200,
  reason,
  headers = [],
  chunks = [],
  trailers = [],
}) {
  const parts = [
    Buffer.from(statusLine(statusCode, reason), 'latin1'),
    Buffer.from(headerLines(headers), 'latin1'),
    Buffer.from('\r\n', 'latin1'),
  ];
  for (const data of chunks) {
    parts.push(Buffer.from(`${data.length.toString(16)}\r\n`, 'latin1'));
    parts.push(data);
    parts.push(Buffer.from('\r\n', 'latin1'));
  }
  parts.push(Buffer.from('0\r\n', 'latin1'));
  parts.push(Buffer.from(headerLines(trailers), 'latin1'));
  parts.push(Buffer.from('\r\n', 'latin1'));
  return Buffer.concat(parts);
}

// ---------- feeding strategies ----------

function feedAll(reader, data) {
  reader.push(data);
}

function feedByteByByte(reader, data) {
  for (const byte of data) reader.push(Buffer.from([byte]));
}

// Deterministic pseudo-random splits so any failure is reproducible.
function makeSplitter(seed = 1234) {
  let state = seed >>> 0;
  const next = () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 0x100000000;
  };
  return function feedRandom(reader, data) {
    let offset = 0;
    while (offset < data.length) {
      const span = 1 + Math.floor(next() * 7); // 1..7 bytes per feed
      reader.push(data.subarray(offset, offset + span));
      offset += span;
    }
  };
}

const FEEDERS = {
  whole: feedAll,
  byteByByte: feedByteByByte,
  random: makeSplitter(99),
};

function makeReader(methods, { onResponse, onInterim } = {}) {
  const reader = new HttpResponseReader({ onResponse, onInterim });
  for (const method of methods) reader.registerRequest(method);
  return reader;
}

// ---------- fixed-length body ----------

test('GET with fixed Content-Length body parses under every split strategy', () => {
  const body = Buffer.from('hello, world', 'utf8');
  const message = fixedResponse({
    headers: [
      ['Content-Type', 'text/plain'],
      ['Content-Length', String(body.length)],
    ],
    body,
  });

  for (const [name, feed] of Object.entries(FEEDERS)) {
    const results = [];
    const reader = makeReader(['GET'], { onResponse: (r) => results.push(r) });
    feed(reader, message);
    reader.end();

    assert.equal(results.length, 1, `feeder ${name}`);
    const res = results[0];
    assert.equal(res.statusCode, 200);
    assert.equal(res.method, 'GET');
    assert.equal(res.requestIndex, 0);
    assert.equal(findHeader(res.headers, 'content-type'), 'text/plain');
    assert.deepEqual(Buffer.from(res.body), body, `feeder ${name} body`);
    assert.deepEqual(res.trailers, []);
    assert.equal(reader.unfinished, null);
    assert.equal(reader.completedCount, 1);
  }
});

test('zero-length fixed body completes immediately after headers', () => {
  const message = fixedResponse({
    headers: [['Content-Length', '0']],
  });
  const results = [];
  const reader = makeReader(['GET'], { onResponse: (r) => results.push(r) });
  feedByteByByte(reader, message);
  reader.end();
  assert.equal(results.length, 1);
  assert.deepEqual(Buffer.from(results[0].body), Buffer.alloc(0));
});

// ---------- HEAD / 204 / 304 have no body ----------

test('HEAD completes at end of headers even with positive Content-Length', () => {
  const body = Buffer.from('this is never sent', 'utf8');
  const head = fixedResponse({
    headers: [
      ['Content-Length', String(body.length)],
      ['Content-Type', 'text/plain'],
    ],
  });
  // A real GET response follows on the same connection.
  const getBody = Buffer.from('actual body');
  const get = fixedResponse({
    headers: [
      ['Content-Length', String(getBody.length)],
    ],
    body: getBody,
  });

  const results = [];
  const reader = makeReader(['HEAD', 'GET'], { onResponse: (r) => results.push(r) });
  feedByteByByte(reader, Buffer.concat([head, get]));
  reader.end();

  assert.equal(results.length, 2);
  const [headRes, getRes] = results;
  assert.equal(headRes.method, 'HEAD');
  assert.equal(findHeader(headRes.headers, 'content-length'), String(body.length));
  assert.deepEqual(Buffer.from(headRes.body), Buffer.alloc(0), 'HEAD body stays empty');
  assert.equal(getRes.method, 'GET');
  assert.deepEqual(Buffer.from(getRes.body), getBody, 'GET bytes not stolen by HEAD');
});

test('204 and 304 responses complete at end of headers despite Content-Length', () => {
  const message204 = fixedResponse({
    statusCode: 204,
    reason: 'No Content',
    headers: [['Content-Length', '120']],
  });
  const message304 = fixedResponse({
    statusCode: 304,
    reason: 'Not Modified',
    headers: [['Content-Length', '999']],
  });
  const followBody = Buffer.from('ok');
  const follow = fixedResponse({
    headers: [['Content-Length', String(followBody.length)]],
    body: followBody,
  });

  const results = [];
  const reader = makeReader(['GET', 'GET', 'GET'], {
    onResponse: (r) => results.push(r),
  });
  const feed = makeSplitter(7);
  feed(reader, Buffer.concat([message204, message304, follow]));
  reader.end();

  assert.deepEqual(results.map((r) => r.statusCode), [204, 304, 200]);
  assert.deepEqual(Buffer.from(results[0].body), Buffer.alloc(0));
  assert.deepEqual(Buffer.from(results[1].body), Buffer.alloc(0));
  assert.deepEqual(Buffer.from(results[2].body), followBody);
});

// ---------- interim 100 / 103 ----------

test('100 Continue then final 200: interim does not consume the request', () => {
  const interim = fixedResponse({ statusCode: 100, reason: 'Continue' });
  const body = Buffer.from('payload');
  const final = fixedResponse({
    headers: [['Content-Length', String(body.length)]],
    body,
  });

  const finals = [];
  const interims = [];
  const reader = makeReader(['GET'], {
    onResponse: (r) => finals.push(r),
    onInterim: (r) => interims.push(r),
  });
  feedByteByByte(reader, Buffer.concat([interim, final]));
  reader.end();

  assert.equal(interims.length, 1);
  assert.equal(interims[0].statusCode, 100);
  assert.equal(interims[0].method, 'GET');
  assert.equal(finals.length, 1);
  assert.equal(finals[0].statusCode, 200);
  assert.deepEqual(Buffer.from(finals[0].body), body);
  // The interim is attached to the request's final result as well.
  assert.deepEqual(finals[0].interim.map((r) => r.statusCode), [100]);
  assert.equal(reader.completedCount, 1);
});

test('multiple 103 early hints then 200 chunked, with random splits', () => {
  const hint1 = fixedResponse({
    statusCode: 103,
    reason: 'Early Hints',
    headers: [['Link', '</style.css>; rel=preload; as=style']],
  });
  const hint2 = fixedResponse({
    statusCode: 103,
    reason: 'Early Hints',
    headers: [['Link', '</app.js>; rel=preload; as=script']],
  });
  const final = chunkedResponse({
    headers: [['Transfer-Encoding', 'chunked']],
    chunks: [Buffer.from('one '), Buffer.from('two'), Buffer.from(' three')],
  });

  const finals = [];
  const interims = [];
  const reader = makeReader(['GET', 'GET'], {
    onResponse: (r) => finals.push(r),
    onInterim: (r) => interims.push(r),
  });
  // A second, plain fixed response after the hinted request.
  const secondBody = Buffer.from('second');
  const second = fixedResponse({
    headers: [['Content-Length', String(secondBody.length)]],
    body: secondBody,
  });

  const feed = makeSplitter(2024);
  feed(reader, Buffer.concat([hint1, hint2, final, second]));
  reader.end();

  assert.equal(interims.length, 2);
  assert.deepEqual(interims.map((r) => r.statusCode), [103, 103]);
  assert.equal(finals.length, 2);
  assert.deepEqual(Buffer.from(finals[0].body), Buffer.from('one two three'));
  assert.deepEqual(finals[0].interim.map((r) => r.statusCode), [103, 103]);
  assert.deepEqual(Buffer.from(finals[1].body), secondBody);
});

// ---------- chunked transfer coding ----------

test('chunked body is decoded and trailers returned separately', () => {
  const message = chunkedResponse({
    headers: [['Transfer-Encoding', 'chunked']],
    chunks: [
      Buffer.from('Wikipedia in\r\n'),
      Buffer.from('chunks.'),
      Buffer.from(''), // zero-data chunk entry is skipped by builder when empty
    ].filter((b) => b.length > 0),
    trailers: [
      ['X-Checksum', 'deadbeef'],
      ['X-Trailer-Two', 'value'],
    ],
  });

  for (const [name, feed] of Object.entries(FEEDERS)) {
    const results = [];
    const reader = makeReader(['GET'], { onResponse: (r) => results.push(r) });
    feed(reader, message);
    reader.end();

    assert.equal(results.length, 1, `feeder ${name}`);
    const res = results[0];
    assert.deepEqual(
      Buffer.from(res.body),
      Buffer.from('Wikipedia in\r\nchunks.'),
      `feeder ${name} decoded body`,
    );
    assert.deepEqual(res.trailers, [
      ['X-Checksum', 'deadbeef'],
      ['X-Trailer-Two', 'value'],
    ]);
    // Trailer names must not leak into the main header section.
    assert.equal(findHeader(res.headers, 'x-checksum'), undefined);
    assert.equal(findHeader(res.trailers, 'x-checksum'), 'deadbeef');
  }
});

test('chunk extensions are tolerated and ignored', () => {
  const message = Buffer.from(
    'HTTP/1.1 200 OK\r\n'
      + 'Transfer-Encoding: chunked\r\n\r\n'
      + '4;name=value\r\n'
      + 'Wiki\r\n'
      + '5\r\n'
      + 'pedia\r\n'
      + '0\r\n\r\n',
    'latin1',
  );
  const results = [];
  const reader = makeReader(['GET'], { onResponse: (r) => results.push(r) });
  feedByteByByte(reader, message);
  reader.end();
  assert.deepEqual(Buffer.from(results[0].body), Buffer.from('Wikipedia'));
});

test('empty chunked body yields empty data with no trailers', () => {
  const message = chunkedResponse({
    headers: [['Transfer-Encoding', 'chunked']],
    chunks: [],
  });
  const results = [];
  const reader = makeReader(['GET'], { onResponse: (r) => results.push(r) });
  const feed = makeSplitter(55);
  feed(reader, message);
  reader.end();
  assert.deepEqual(Buffer.from(results[0].body), Buffer.alloc(0));
  assert.deepEqual(results[0].trailers, []);
});

// ---------- pipelining / interleaving ----------

test('pipelined HEAD then GET then GET with mixed framing stays aligned', () => {
  const head = fixedResponse({
    headers: [['Content-Length', '500']],
  });
  const get1 = chunkedResponse({
    headers: [['Transfer-Encoding', 'chunked']],
    chunks: [Buffer.from('abc'), Buffer.from('defgh')],
    trailers: [['X-T', '1']],
  });
  const body2 = Buffer.from('fixed tail');
  const get2 = fixedResponse({
    headers: [['Content-Length', String(body2.length)]],
    body: body2,
  });

  const results = [];
  const reader = makeReader(['HEAD', 'GET', 'GET'], {
    onResponse: (r) => results.push(r),
  });
  const feed = makeSplitter(31337);
  feed(reader, Buffer.concat([head, get1, get2]));
  reader.end();

  assert.deepEqual(results.map((r) => r.method), ['HEAD', 'GET', 'GET']);
  assert.deepEqual(results.map((r) => r.statusCode), [200, 200, 200]);
  assert.deepEqual(Buffer.from(results[0].body), Buffer.alloc(0));
  assert.deepEqual(Buffer.from(results[1].body), Buffer.from('abcdefgh'));
  assert.deepEqual(results[1].trailers, [['X-T', '1']]);
  assert.deepEqual(Buffer.from(results[2].body), body2);
  assert.equal(reader.unfinished, null);
});

test('responses fire in strict request order via onResponse', () => {
  const order = [];
  const body = Buffer.from('x');
  const one = fixedResponse({ headers: [['Content-Length', '1']], body });
  const reader = makeReader(['GET', 'GET', 'GET'], {
    onResponse: (r) => order.push(r.requestIndex),
  });
  reader.push(Buffer.concat([one, one, one]));
  reader.end();
  assert.deepEqual(order, [0, 1, 2]);
});

// ---------- splits inside status line, hex length and line endings ----------

test('status line split across feeds is reassembled', () => {
  const body = Buffer.from('xy');
  const message = fixedResponse({
    headers: [['Content-Length', '2']],
    body,
  });
  const results = [];
  const reader = makeReader(['GET'], { onResponse: (r) => results.push(r) });
  // Split precisely inside "HTTP/1.1 200 OK" and across the CRLF.
  reader.push(message.subarray(0, 9));
  reader.push(message.subarray(9, 12));
  reader.push(message.subarray(12));
  reader.end();
  assert.equal(results[0].statusCode, 200);
  assert.deepEqual(Buffer.from(results[0].body), body);
});

test('hex chunk-size line split across feeds', () => {
  const message = Buffer.from(
    'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
      + 'a\r\n0123456789\r\n0\r\n\r\n',
    'latin1',
  );
  const results = [];
  const reader = makeReader(['GET'], { onResponse: (r) => results.push(r) });
  // Cut right between 'a' and its CRLF.
  const cut = message.indexOf(Buffer.from('a\r\n0123'));
  reader.push(message.subarray(0, cut + 1));
  reader.push(message.subarray(cut + 1));
  reader.end();
  assert.deepEqual(Buffer.from(results[0].body), Buffer.from('0123456789'));
});

test('reason phrase is optional', () => {
  const message = Buffer.from('HTTP/1.1 204\r\nContent-Length: 3\r\n\r\n', 'latin1');
  const results = [];
  const reader = makeReader(['GET'], { onResponse: (r) => results.push(r) });
  reader.push(message);
  reader.end();
  assert.equal(results[0].statusCode, 204);
  assert.equal(results[0].reasonPhrase, '');
});

// ---------- headers edge cases ----------

test('duplicate and mixed-case headers are preserved and looked up case-insensitively', () => {
  const body = Buffer.from('zz');
  const message = fixedResponse({
    headers: [
      ['Set-Cookie', 'a=1'],
      ['set-cookie', 'b=2'],
      ['X-FOO', 'bar'],
      ['Content-Length', '2'],
    ],
    body,
  });
  const results = [];
  const reader = makeReader(['GET'], { onResponse: (r) => results.push(r) });
  reader.push(message);
  reader.end();
  const res = results[0];
  assert.deepEqual(
    res.headers.filter(([name]) => name.toLowerCase() === 'set-cookie'),
    [['Set-Cookie', 'a=1'], ['set-cookie', 'b=2']],
  );
  assert.equal(findHeader(res.headers, 'X-foo'), 'bar');
  assert.equal(findHeader(res.headers, 'CONTENT-LENGTH'), '2');
});

test('optional whitespace around header values is trimmed', () => {
  const message = Buffer.from(
    'HTTP/1.1 200 OK\r\nX-A:\t spaced  \r\nContent-Length: 0\r\n\r\n',
    'latin1',
  );
  const results = [];
  const reader = makeReader(['GET'], { onResponse: (r) => results.push(r) });
  reader.push(message);
  reader.end();
  assert.equal(findHeader(results[0].headers, 'x-a'), 'spaced');
});

// ---------- early connection close ----------

test('early close preserves completed responses and marks the unfinished request', () => {
  const firstBody = Buffer.from('complete');
  const first = fixedResponse({
    headers: [['Content-Length', String(firstBody.length)]],
    body: firstBody,
  });
  const second = fixedResponse({
    headers: [['Content-Length', '100']],
    body: Buffer.from('only a few bytes'),
  });

  const results = [];
  const reader = makeReader(['GET', 'GET'], {
    onResponse: (r) => results.push(r),
  });
  // Cut the second response halfway through its declared body.
  const cut = first.length + second.length - 5;
  assert.throws(
    () => {
      reader.push(Buffer.concat([first, second]).subarray(0, cut));
      reader.end();
    },
    /incomplete response for request #1 \(GET\)/,
  );

  assert.equal(results.length, 1, 'completed response survives the early close');
  assert.deepEqual(Buffer.from(results[0].body), firstBody);
  assert.deepEqual(reader.unfinished, { index: 1, method: 'GET' });
  assert.equal(reader.completedCount, 1);
});

test('early close during header section reports the in-flight HEAD request', () => {
  const partial = Buffer.from('HTTP/1.1 200 OK\r\nContent-Len', 'latin1');
  const results = [];
  const reader = makeReader(['HEAD', 'GET'], {
    onResponse: (r) => results.push(r),
  });
  assert.throws(
    () => {
      feedByteByByte(reader, partial);
      reader.end();
    },
    /incomplete response for request #0 \(HEAD\)/,
  );
  assert.equal(results.length, 0);
  assert.deepEqual(reader.unfinished, { index: 0, method: 'HEAD' });
});

test('early close with no response bytes marks the first registered request', () => {
  const reader = makeReader(['GET', 'GET']);
  assert.throws(() => reader.end(), /incomplete response for request #0 \(GET\)/);
  assert.deepEqual(reader.unfinished, { index: 0, method: 'GET' });
});

test('early close inside an interim response still names the request', () => {
  const partial = Buffer.from('HTTP/1.1 100 Continue\r\nX-Partial:', 'latin1');
  const interims = [];
  const reader = makeReader(['GET'], { onInterim: (r) => interims.push(r) });
  assert.throws(() => {
    reader.push(partial);
    reader.end();
  }, /incomplete response for request #0 \(GET\)/);
  assert.equal(interims.length, 0);
  assert.deepEqual(reader.unfinished, { index: 0, method: 'GET' });
});

test('early close inside chunked body reports unfinished and keeps prior response', () => {
  const firstBody = Buffer.from('done');
  const first = fixedResponse({
    headers: [['Content-Length', String(firstBody.length)]],
    body: firstBody,
  });
  const partialChunked = Buffer.from(
    'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nabc',
    'latin1',
  );
  const results = [];
  const reader = makeReader(['GET', 'GET'], {
    onResponse: (r) => results.push(r),
  });
  assert.throws(() => {
    reader.push(Buffer.concat([first, partialChunked]));
    reader.end();
  }, /incomplete response for request #1 \(GET\)/);
  assert.equal(results.length, 1);
  assert.deepEqual(Buffer.from(results[0].body), firstBody);
  assert.deepEqual(reader.unfinished, { index: 1, method: 'GET' });
});

// ---------- usage / framing errors ----------

test('end() without pending requests on a clean stream succeeds', () => {
  const reader = makeReader([]);
  reader.end();
  assert.equal(reader.unfinished, null);
});

test('double end() and push after end() throw', () => {
  const reader = makeReader([]);
  reader.end();
  assert.throws(() => reader.end(), /end\(\) called more than once/);
  assert.throws(() => reader.push(Buffer.from('x')), /after end\(\)/);
});

test('registering after end is rejected', () => {
  const reader = makeReader([]);
  reader.end();
  assert.throws(() => reader.registerRequest('GET'), /after end\(\)/);
});

test('a response without a registered request throws', () => {
  const message = fixedResponse({
    headers: [['Content-Length', '0']],
  });
  const reader = new HttpResponseReader();
  assert.throws(() => reader.push(message), /unregistered request/);
});

test('malformed status line and bad chunk size are rejected', () => {
  const reader1 = makeReader(['GET']);
  assert.throws(
    () => reader1.push(Buffer.from('BORING/1.0 200 OK\r\n\r\n', 'latin1')),
    /malformed status line/,
  );

  const badChunk = Buffer.from(
    'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n',
    'latin1',
  );
  const reader2 = makeReader(['GET']);
  assert.throws(() => reader2.push(badChunk), /malformed chunk size/);
});
