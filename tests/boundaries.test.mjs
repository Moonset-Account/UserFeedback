import test from 'node:test';
import assert from 'node:assert/strict';

import { reviewPackage, reviewVariant, reviewManifest } from '../src/index.mjs';

test('same file name in two variants is expanded independently', () => {
  const pkg = {
    variants: [
      {
        id: 'chrome',
        resources: [{
          name: 'same.js',
          condition: { browser: 'chrome' },
          optionalFeatures: ['shared-experiment'],
          optionalPermissions: [{ id: 'experimental', feature: 'shared-experiment' }],
        }],
        approval: { features: [{ id: 'shared-experiment', optional: true }] },
      },
      {
        id: 'firefox',
        resources: [{
          name: 'same.js',
          condition: { browser: 'chrome' },
          optionalFeatures: ['shared-experiment'],
          optionalPermissions: [{ id: 'experimental', feature: 'shared-experiment' }],
        }],
        approval: { features: [{ id: 'shared-experiment', optional: true }] },
      },
    ],
  };

  const report = reviewPackage(pkg);
  const chrome = report.getVariant('chrome');
  const firefox = report.getVariant('firefox');

  assert.equal(chrome.counts.loadedResources, 1);
  assert.equal(chrome.residualOptionalRequests.length, 0);
  assert.equal(chrome.optionalRequestReviews[0].scopeApproved, true);

  assert.equal(firefox.counts.loadedResources, 0);
  assert.equal(firefox.counts.skippedResources, 1);
  assert.equal(firefox.residualOptionalRequests.length, 1);
  assert.equal(firefox.residualOptionalRequests[0].permission, 'experimental');
});

test('install permissions inside skipped resources are not active extras', () => {
  const result = reviewVariant({
    id: 'chrome',
    resources: [
      { name: 'loaded.js', condition: true, permissions: ['storage'] },
      { name: 'skipped.js', condition: { browser: 'firefox' }, permissions: ['management'] },
    ],
    approval: { installPermissions: ['storage'] },
  });

  assert.equal(result.ok, true);
  assert.deepEqual(result.extraInstallPermissions, []);
  assert.deepEqual(result.permissions.install.map((r) => r.permission), ['storage']);
});

test('a residual request still physically present at install time is both extra and residual', () => {
  const result = reviewVariant({
    id: 'chrome',
    resources: [{
      name: 'ghost.js',
      condition: false,
      optionalFeatures: ['ghost'],
    }],
    permissions: [{ id: 'ghost.install', timing: 'install', feature: 'ghost' }],
    approval: { features: [], installPermissions: [] },
  });

  assert.equal(result.extraInstallPermissions.length, 1);
  assert.equal(result.extraInstallPermissions[0].permission, 'ghost.install');
  assert.equal(result.residualOptionalRequests.length, 1);
  assert.equal(result.residualOptionalRequests[0].active, true);
  assert.ok(result.issues.some((i) => i.code === 'EXTRA_INSTALL_PERMISSION'));
  assert.ok(result.issues.some((i) => i.code === 'RESIDUAL_OPTIONAL_REQUEST'));
});

test('invalid loading conditions fail closed and are reported', () => {
  const result = reviewVariant({
    id: 'chrome',
    resources: [{ name: 'mystery.js', condition: { unknownGate: true }, permissions: ['storage'] }],
    approval: { installPermissions: ['storage'] },
  });

  assert.equal(result.ok, false);
  assert.equal(result.loadedResources.length, 0);
  assert.ok(result.issues.some((i) => i.code === 'INVALID_LOAD_CONDITION'));
});

test('invalid site scopes are blocking review errors', () => {
  const result = reviewVariant({
    id: 'chrome',
    resources: [{ name: 'content.js', sites: ['https://'] }],
    approval: { sites: ['https://example.com/*'] },
  });

  assert.equal(result.ok, false);
  assert.ok(result.issues.some((i) => i.code === 'INVALID_SITE_SCOPE'));
});

test('bare approved feature attribution approves its optional request bundle', () => {
  const result = reviewVariant({
    id: 'firefox',
    resources: [{
      name: 'reader.js',
      optionalFeatures: ['reader'],
      optionalPermissions: [{ id: 'clipboardRead', feature: 'reader' }],
    }],
    approval: ['reader'],
  });

  const request = result.optionalRequestReviews[0];
  assert.equal(request.attributionStatus, 'approved-feature');
  assert.equal(request.scopeApproved, true);
  assert.equal(request.approved, true);
  assert.equal(result.ok, true);
});

test('explicit empty feature permission scope denies undeclared grants', () => {
  const result = reviewVariant({
    id: 'chrome',
    resources: [{
      name: 'reader.js',
      optionalFeatures: ['reader'],
      optionalPermissions: [{ id: 'clipboardRead', feature: 'reader' }],
    }],
    approval: { features: [{ id: 'reader', permissions: [] }] },
  });

  assert.equal(result.ok, false);
  assert.equal(result.optionalRequestReviews[0].scopeApproved, false);
});

test('accepts variant maps, approval maps, and package-level shared catalog', () => {
  const report = reviewPackage({
    resources: {
      'a.js': { features: ['core'], permissions: ['storage'] },
    },
    variants: {
      chrome: { browser: 'chrome' },
      firefox: { browser: 'firefox' },
    },
    approvals: {
      chrome: { features: ['core'], permissions: ['storage'] },
      firefox: { features: ['core'], permissions: ['storage'] },
    },
  });

  assert.deepEqual(report.variantIds, ['chrome', 'firefox']);
  assert.equal(report.getVariant('chrome').loadedResources.length, 1);
  assert.equal(report.getVariant('firefox').loadedResources.length, 1);
  assert.equal(report.ok, true);
});

test('reviewManifest reviews a single manifest and reviewPackage rejects no variants', () => {
  const single = reviewManifest({
    id: 'edge',
    resources: [{ name: 'x.js', permissions: ['storage'] }],
    approval: { permissions: ['storage'] },
  });
  assert.equal(single.variantId, 'edge');
  assert.equal(single.ok, true);

  const empty = reviewPackage({ name: 'empty' });
  assert.equal(empty.ok, false);
  assert.ok(empty.issues.some((i) => i.code === 'NO_VARIANTS'));
});

test('accepts string conditions and top-level browser shortcut fields', () => {
  const result = reviewPackage({
    variants: [
      {
        id: 'chrome',
        resources: [
          { name: 'string.js', condition: 'chrome', permissions: ['a'] },
          { name: 'shortcut.js', browsers: ['firefox'], permissions: ['b'] },
        ],
        approval: { permissions: ['a', 'b'] },
      },
    ],
  }).getVariant('chrome');

  assert.deepEqual(result.loadedResources.map((r) => r.name), ['string.js']);
  assert.deepEqual(result.skippedResources.map((r) => r.name), ['shortcut.js']);
  assert.equal(result.ok, true);
});

test('finds approved feature attribution from feature-scoped approval grants', () => {
  const result = reviewVariant({
    id: 'chrome',
    resources: [{
      name: 'optional.js',
      optionalPermissions: ['downloads'],
    }],
    approval: {
      features: [
        { id: 'pdf-export', optional: true, optionalPermissions: ['downloads'] },
      ],
    },
  });

  const request = result.optionalRequestReviews[0];
  assert.equal(request.declaredFeature, null);
  assert.equal(request.resolvedFeature, 'pdf-export');
  assert.equal(request.attributionSource, 'approval-scope');
  assert.equal(request.attributionStatus, 'approved-feature');
  assert.equal(request.approved, true);
});

test('ambiguous approval ownership is reported rather than silently choosing a feature', () => {
  const result = reviewVariant({
    id: 'chrome',
    resources: [{ name: 'optional.js', optionalPermissions: ['storage'] }],
    approval: {
      features: [
        { id: 'feature-a', optional: true, optionalPermissions: ['storage'] },
        { id: 'feature-b', optional: true, optionalPermissions: ['storage'] },
      ],
    },
  });

  const request = result.optionalRequestReviews[0];
  assert.equal(request.attributionStatus, 'ambiguous-feature');
  assert.deepEqual(request.featureCandidates.sort(), ['feature-a', 'feature-b']);
  assert.ok(result.issues.some((i) => i.code === 'OPTIONAL_FEATURE_ATTRIBUTION_AMBIGUOUS'));
});

test('malformed variant entries are reported instead of throwing', () => {
  const report = reviewPackage({ variants: [null, { id: 'chrome', approval: {}, resources: [] }] });
  assert.equal(report.ok, false);
  assert.ok(report.issues.some((i) => i.code === 'INVALID_VARIANT'));
});

test('duplicate variant ids are package-level errors', () => {
  const report = reviewPackage({ variants: [{ id: 'chrome' }, { id: 'chrome' }] });
  assert.ok(report.packageIssues.some((i) => i.code === 'DUPLICATE_VARIANT_ID'));
  assert.equal(report.ok, false);
});

test('Chinese timing aliases normalize to install/enable/optional buckets', () => {
  const result = reviewVariant({
    id: 'chrome',
    resources: [{
      name: 'timing.js',
      permissions: [
        { id: 'p.install', timing: '安装时' },
        { id: 'p.enable', timing: '启用后', feature: 'f' },
        { id: 'p.optional', timing: '可选申请', feature: 'f' },
      ],
      features: ['f'],
    }],
    approval: {
      features: ['f'],
      installPermissions: ['p.install'],
      enablePermissions: ['p.enable'],
      optionalPermissions: ['p.optional'],
    },
  });

  assert.deepEqual(result.permissions.install.map((r) => r.permission), ['p.install']);
  assert.deepEqual(result.permissions.enable.map((r) => r.permission), ['p.enable']);
  assert.deepEqual(result.permissions.optional.map((r) => r.permission), ['p.optional']);
  assert.equal(result.ok, true);
});
