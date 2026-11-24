import test from 'node:test';
import assert from 'node:assert/strict';

import defaultApi, { review } from '../src/index.mjs';
import compatApi, { reviewPackage } from '../src/index.js';
import rootApi from '../index.js';

test('exposes callable default and compatibility entry points', () => {
  assert.equal(typeof defaultApi, 'function');
  assert.equal(typeof review, 'function');
  assert.equal(review, reviewPackage);

  const result = defaultApi({ variants: [] });
  assert.equal(result.ok, false);
  assert.equal(result.issues[0].code, 'NO_VARIANTS');

  assert.equal(typeof compatApi.reviewVariant, 'function');
  assert.equal(typeof rootApi.reviewPackage, 'function');
});
