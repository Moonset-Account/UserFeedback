// Deterministic fuzz-style tests: generate synthetic legal response streams,
// slice them at pseudo-random byte boundaries, and verify the incremental
// reader produces exactly the same decoded result as the reference model.
// No network access; all messages are built locally in this file.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { HttpResponseReader } from '../src/http-response-reader.mjs';

function lcg(seed) {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 0x100000000;
  };
}

function randomBytes(rand, maxLen) {
  const len = Math.floor(rand() * (maxLen + 1));
  const out = Buffer.alloc(len);
  for (let i = 0; i < len; i += 1) {
    // Avoid 0x0d/0x0a in bodies so framing stays unambiguous when debugging;
    // legal payloads anyway — framing bytes live only in the generated lines.
    out[i] = 0x20 + Math.floor(rand() * (0x7e - 0x20));
  }
  return out;
}

function buildScenario(rand) {
  const requestCount = 1 + Math.floor(rand() * 5);
  const methods = [];
  const expected = [];
  const parts = [];

  for (let i = 0; i < requestCount; i += 1) {
    const method = rand() < 0.4 ? 'HEAD' : 'GET';
    methods.push(method);

    // Zero, one or two interim responses (100 / 103).
    const interimCount = Math.floor(rand() * 3);
    const interimCodes = [];
    for (let j = 0; j < interimCount; j += 1) {
      const code = rand() < 0.5 ? 100 : 103;
      interimCodes.push(code);
      parts.push(
        Buffer.from(
          `HTTP/1.1 ${code} ${code === 100 ? 'Continue' : 'Early Hints'}\r\n`
            + `X-Interim: ${j}\r\n\r\n`,
          'latin1',
        ),
      );
    }

    let statusCode;
    const roll = rand();
    if (roll < 0.15) statusCode = 204;
    else if (roll < 0.3) statusCode = 304;
    else statusCode = 200;

    const framing = rand();
    const declaredLength = Math.floor(rand() * 40);
    const bodyPieces =
      statusCode === 200 && method === 'GET'
        ? [randomBytes(rand, 15), randomBytes(rand, 15), randomBytes(rand, 5)]
        : [];
    const expectedBody = Buffer.concat(bodyPieces);

    const head = [`HTTP/1.1 ${statusCode} status\r\n`, 'X-Request: ', String(i), '\r\n'];

    if (statusCode === 200 && method === 'GET' && framing < 0.5) {
      // Fixed-length framing.
      head.push(`Content-Length: ${expectedBody.length}\r\n\r\n`);
      parts.push(Buffer.from(head.join(''), 'latin1'));
      parts.push(expectedBody);
    } else if (statusCode === 200 && method === 'GET') {
      // Chunked framing, possibly with trailers.
      head.push('Transfer-Encoding: chunked\r\n\r\n');
      parts.push(Buffer.from(head.join(''), 'latin1'));
      for (const piece of bodyPieces) {
        if (piece.length === 0) continue;
        parts.push(Buffer.from(`${piece.length.toString(16)}\r\n`, 'latin1'));
        parts.push(piece);
        parts.push(Buffer.from('\r\n', 'latin1'));
      }
      parts.push(Buffer.from('0\r\n', 'latin1'));
      const trailers = rand() < 0.5
        ? [['X-Trailer', 'tval']]
        : [];
      for (const [name, value] of trailers) {
        parts.push(Buffer.from(`${name}: ${value}\r\n`, 'latin1'));
      }
      parts.push(Buffer.from('\r\n', 'latin1'));
      expected.push({
        method,
        statusCode,
        interimCodes,
        body: expectedBody,
        trailers,
      });
      continue;
    } else {
      // HEAD, 204 or 304: carry a misleading positive Content-Length, which
      // must be ignored because no body exists for these responses.
      head.push(`Content-Length: ${Math.max(declaredLength, 1)}\r\n\r\n`);
      parts.push(Buffer.from(head.join(''), 'latin1'));
    }

    expected.push({
      method,
      statusCode,
      interimCodes,
      body: statusCode === 200 && method === 'GET' ? expectedBody : Buffer.alloc(0),
      trailers: [],
    });
  }

  return { methods, expected, stream: Buffer.concat(parts) };
}

function randomSlices(rand, data) {
  const slices = [];
  let offset = 0;
  while (offset < data.length) {
    // Include span 1 often enough to hit every possible cut point over runs.
    const span = 1 + Math.floor(rand() * 9);
    slices.push(data.subarray(offset, Math.min(offset + span, data.length)));
    offset += span;
  }
  return slices;
}

test('fuzz: 60 random streams parsed through random byte boundaries', () => {
  const rand = lcg(0xC0FFEE);
  for (let iteration = 0; iteration < 60; iteration += 1) {
    const { methods, expected, stream } = buildScenario(rand);
    const finals = [];
    const interims = [];
    const reader = new HttpResponseReader({
      onResponse: (r) => finals.push(r),
      onInterim: (r) => interims.push(r),
    });
    for (const method of methods) reader.registerRequest(method);

    for (const slice of randomSlices(rand, stream)) reader.push(slice);
    reader.end();

    assert.equal(finals.length, expected.length, `iteration ${iteration}: final count`);
    let interimSeen = 0;
    for (let i = 0; i < expected.length; i += 1) {
      const res = finals[i];
      const want = expected[i];
      assert.equal(res.requestIndex, i, `iteration ${iteration}: index`);
      assert.equal(res.method, want.method, `iteration ${iteration}: method`);
      assert.equal(res.statusCode, want.statusCode, `iteration ${iteration}: status`);
      assert.deepEqual(
        Buffer.from(res.body),
        want.body,
        `iteration ${iteration}: body of request ${i}`,
      );
      assert.deepEqual(
        res.trailers,
        want.trailers.map(([n, v]) => [n, v]),
        `iteration ${iteration}: trailers`,
      );
      assert.deepEqual(
        res.interim.map((r) => r.statusCode),
        want.interimCodes,
        `iteration ${iteration}: interim linkage`,
      );
      interimSeen += want.interimCodes.length;
    }
    assert.equal(interims.length, interimSeen, `iteration ${iteration}: interim count`);
    assert.equal(reader.unfinished, null);
    assert.equal(reader.completedCount, expected.length);
  }
});

test('fuzz: same stream must decode identically across three feed strategies', () => {
  const rand = lcg(424242);
  for (let iteration = 0; iteration < 25; iteration += 1) {
    const { methods, stream } = buildScenario(rand);
    const runs = [];
    for (const strategy of ['whole', 'slices', 'bytes']) {
      const finals = [];
      const reader = new HttpResponseReader({ onResponse: (r) => finals.push(r) });
      for (const method of methods) reader.registerRequest(method);
      if (strategy === 'whole') {
        reader.push(stream);
      } else if (strategy === 'slices') {
        const localRand = lcg(iteration * 7 + 1);
        for (const slice of randomSlices(localRand, stream)) reader.push(slice);
      } else {
        for (const byte of stream) reader.push(Buffer.from([byte]));
      }
      reader.end();
      runs.push(finals.map((r) => ({
        statusCode: r.statusCode,
        body: Buffer.from(r.body).toString('hex'),
        trailers: r.trailers,
      })));
    }
    assert.deepEqual(runs[0], runs[1], `iteration ${iteration}: whole vs slices`);
    assert.deepEqual(runs[1], runs[2], `iteration ${iteration}: slices vs bytes`);
  }
});
