import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeSiteScope, siteCovers, findCoveringScopes } from '../src/index.mjs';

test('normalizes bare hosts, origins, and match patterns', () => {
  assert.equal(normalizeSiteScope('example.com').canonical, '*://example.com/*');
  assert.equal(normalizeSiteScope('https://example.com').canonical, 'https://example.com/*');
  assert.equal(normalizeSiteScope('https://Example.COM/api').canonical, 'https://example.com/api');
  assert.equal(normalizeSiteScope('<all_urls>').canonical, '<all_urls>');
});

test('approved wildcard subdomain covers apex and one/multi-level subdomains only', () => {
  assert.equal(siteCovers('https://*.example.com/*', 'https://example.com/a'), true);
  assert.equal(siteCovers('https://*.example.com/*', 'https://api.example.com/v1/a'), true);
  assert.equal(siteCovers('https://*.example.com/*', 'https://deep.api.example.com/a'), true);
  assert.equal(siteCovers('https://*.example.com/*', 'https://evil-example.com/a'), false);
  assert.equal(siteCovers('https://*.example.com/*', 'https://example.org/a'), false);
});

test('scheme and port boundaries are enforced', () => {
  assert.equal(siteCovers('*://example.com/*', 'https://example.com/a'), true);
  assert.equal(siteCovers('https://example.com/*', 'http://example.com/a'), false);
  assert.equal(siteCovers('https://example.com/*', 'https://example.com:8443/a'), false);
  assert.equal(siteCovers('https://example.com:8443/*', 'https://example.com:8443/a'), true);
});

test('path prefixes cover nested requested prefixes and literal paths', () => {
  assert.equal(siteCovers('https://example.com/api/*', 'https://example.com/api/v1/users'), true);
  assert.equal(siteCovers('https://example.com/api/*', 'https://example.com/api/v1/*'), true);
  assert.equal(siteCovers('https://example.com/api/*', 'https://example.com/other'), false);
  assert.equal(siteCovers('https://example.com/api/*', 'https://example.com/api'), false);
});

test('findCoveringScopes returns every matching approval', () => {
  const approved = ['https://docs.example.com/*', 'https://*.example.com/docs/*'];
  const covers = findCoveringScopes(approved, 'https://docs.example.com/docs/a');
  assert.ok(covers.length >= 1);
  assert.equal(findCoveringScopes(approved, 'https://evil.test/*').length, 0);
});

test('invalid site scopes are rejected', () => {
  assert.throws(() => normalizeSiteScope(''), /non-empty/);
  assert.throws(() => normalizeSiteScope('https:///missing-host'), /missing host/);
});

test('file match patterns with an empty host can be compared', () => {
  assert.equal(normalizeSiteScope('file:///Users/demo/*.html').hostname, '');
  assert.equal(siteCovers('file:///Users/demo/*', 'file:///Users/demo/report.html'), true);
  assert.equal(siteCovers('file:///Users/demo/*', 'file:///etc/passwd'), false);
});
