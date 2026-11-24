import test from 'node:test';
import assert from 'node:assert/strict';

import { evaluateCondition, createVariantContext, compareVersions } from '../src/index.mjs';

test('browser and variant conditions are case-insensitive', () => {
  const ctx = createVariantContext({ id: 'Chrome', browser: 'Chrome' });
  assert.equal(evaluateCondition({ browser: 'chrome' }, ctx), true);
  assert.equal(evaluateCondition({ browsers: ['firefox', 'edge'] }, ctx), false);
  assert.equal(evaluateCondition({ excludeBrowsers: ['firefox'] }, ctx), true);
});

test('all, any, and not compose deterministically', () => {
  const ctx = { browser: 'chrome', flags: { ai: true } };
  assert.equal(evaluateCondition({ all: [{ browser: 'chrome' }, { flag: 'ai' }] }, ctx), true);
  assert.equal(evaluateCondition({ all: [{ browser: 'firefox' }, { flag: 'ai' }] }, ctx), false);
  assert.equal(evaluateCondition({ any: [{ browser: 'firefox' }, { flag: 'ai' }] }, ctx), true);
  assert.equal(evaluateCondition({ not: { browser: 'firefox' } }, ctx), true);
});

test('semantic version constraints compare numeric components', () => {
  assert.equal(compareVersions('130.0.1', '130.0.0'), 1);
  assert.equal(evaluateCondition({ minVersion: '120' }, { version: '130.0.1' }), true);
  assert.equal(evaluateCondition({ version: '>=120 <131' }, { version: '130.0.1' }), true);
  assert.equal(evaluateCondition({ version: '>=131' }, { version: '130.0.1' }), false);
});

test('feature flags and enabled features gate resources', () => {
  const ctx = { flags: { beta: true }, enabledFeatures: ['pdf'] };
  assert.equal(evaluateCondition({ flag: 'beta' }, ctx), true);
  assert.equal(evaluateCondition({ flag: { beta: true, hidden: false } }, ctx), true);
  assert.equal(evaluateCondition({ enabledFeature: 'pdf' }, ctx), true);
  assert.equal(evaluateCondition({ enabledFeatures: ['pdf', 'sync'] }, ctx), false);
});

test('unknown condition keys fail instead of silently loading a resource', () => {
  assert.throws(() => evaluateCondition({ browserr: 'chrome' }, { browser: 'chrome' }), /Unknown loading-condition key/);
});
