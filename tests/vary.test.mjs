import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { buildVariantKey, normalizeHeaders, parseVary, varyEquals } from '../src/vary.js';

describe('vary helpers', () => {
  it('parses Vary headers into sorted lowercase field names', () => {
    assert.deepEqual(parseVary('Accept-Language, X-Foo'), ['accept-language', 'x-foo']);
    assert.deepEqual(parseVary(['X-B, x-a']), ['x-a', 'x-b']);
    assert.deepEqual(parseVary('*'), ['*']);
    assert.deepEqual(parseVary(undefined), []);
  });

  it('normalizes plain objects, Maps, arrays and Headers-like inputs', () => {
    assert.deepEqual(normalizeHeaders({ 'Accept-Language': 'en' }), [['accept-language', 'en']]);
    assert.deepEqual(normalizeHeaders(new Map([['X', 'y']])), [['x', 'y']]);
    assert.deepEqual(normalizeHeaders([['A', 'b']]), [['a', 'b']]);
    assert.deepEqual(normalizeHeaders(undefined), []);
  });

  it('builds stable variant keys from the selected request headers only', () => {
    const vary = parseVary('Accept-Language');
    const keyA = buildVariantKey(vary, { 'accept-language': 'en', 'x-other': '1' });
    const keyB = buildVariantKey(vary, { 'accept-language': 'en', 'x-other': '2' });
    const keyC = buildVariantKey(vary, { 'accept-language': 'fr' });
    assert.equal(keyA, keyB, 'unrelated headers must not change the variant');
    assert.notEqual(keyA, keyC);
  });

  it('distinguishes a missing header from an empty one', () => {
    const vary = parseVary('X-Tenant');
    const missing = buildVariantKey(vary, {});
    const empty = buildVariantKey(vary, { 'x-tenant': '' });
    assert.notEqual(missing, empty);
  });

  it('compares Vary field lists', () => {
    assert.equal(varyEquals(['a', 'b'], ['a', 'b']), true);
    assert.equal(varyEquals(['a'], ['a', 'b']), false);
    assert.equal(varyEquals(['b', 'a'], ['a', 'b']), false);
  });
});
