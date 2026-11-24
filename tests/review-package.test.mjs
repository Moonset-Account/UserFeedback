import test from 'node:test';
import assert from 'node:assert/strict';

import { reviewPackage } from '../src/index.mjs';
import { syntheticPackage, cloneSyntheticPackage } from '../examples/synthetic-package.mjs';

function codes(variant) {
  return variant.issues.map((issue) => issue.code);
}

test('expands the real loaded feature combination for each browser variant', () => {
  const report = reviewPackage(syntheticPackage);
  assert.deepEqual(report.variantIds, ['chrome', 'firefox']);

  const chrome = report.getVariant('chrome');
  const firefox = report.getVariant('firefox');

  assert.deepEqual(chrome.loadedResources.map((r) => r.name).sort(), [
    'features/ai.js',
    'features/diagnostics.js',
    'features/export.js',
    'shared/core.js',
  ]);
  assert.deepEqual(chrome.skippedResources.map((r) => r.name), ['features/legacy-sync.js']);

  assert.deepEqual(firefox.loadedResources.map((r) => r.name).sort(), [
    'features/ai.js',
    'features/legacy-sync.js',
    'shared/core.js',
  ]);
  assert.deepEqual(firefox.skippedResources.map((r) => r.name), ['features/export.js']);

  assert.ok(chrome.features.find((f) => f.id === 'export-pdf')?.implemented);
  assert.equal(firefox.features.find((f) => f.id === 'export-pdf')?.implemented, false);
  assert.equal(chrome.features.find((f) => f.id === 'legacy-sync')?.implemented, false);
  assert.ok(firefox.features.find((f) => f.id === 'legacy-sync')?.implemented);
});

test('reports extra install-time permissions for the variant that requests them', () => {
  const report = reviewPackage(syntheticPackage);
  const chrome = report.getVariant('chrome');
  const firefox = report.getVariant('firefox');

  assert.equal(chrome.extraInstallPermissions.length, 1);
  assert.equal(chrome.extraInstallPermissions[0].permission, 'management');
  assert.deepEqual(chrome.extraInstallPermissions[0].resources, ['features/diagnostics.js']);
  assert.ok(codes(chrome).includes('EXTRA_INSTALL_PERMISSION'));

  assert.equal(firefox.extraInstallPermissions.length, 0);
  assert.equal(report.summary.extraInstallPermissions, 1);
});

test('compares site scopes and reports only unapproved variant requests', () => {
  const report = reviewPackage(syntheticPackage);
  const chrome = report.getVariant('chrome');
  const badSites = chrome.unapprovedSites.map((r) => r.pattern);
  assert.deepEqual(badSites, ['https://telemetry.example.com/v1/*']);

  const docs = chrome.sites.install.find((r) => r.pattern === 'https://docs.example.com/workspace/*');
  assert.equal(docs.scopeApproved, true);

  const ai = chrome.sites.enable.find((r) => r.feature === 'ai-summary');
  assert.equal(ai.scopeApproved, true);

  const exportSite = chrome.sites.optional.find((r) => r.feature === 'export-pdf');
  assert.equal(exportSite.scopeApproved, true);
});

test('optional requests receive approved feature attribution and scope decisions', () => {
  const chrome = reviewPackage(syntheticPackage).getVariant('chrome');

  const downloads = chrome.optionalRequestReviews.find((r) => r.permission === 'downloads');
  assert.equal(downloads.attributionStatus, 'approved-feature');
  assert.equal(downloads.scopeApproved, true);
  assert.equal(downloads.approved, true);

  const clipboard = chrome.optionalRequestReviews.find((r) => r.permission === 'clipboardWrite');
  assert.equal(clipboard.attributionStatus, 'approved-feature');
  assert.equal(clipboard.featureApproved, true);
  assert.equal(clipboard.scopeApproved, false);
  assert.ok(codes(chrome).includes('UNAPPROVED_OPTIONAL_PERMISSION'));

  const geolocation = chrome.optionalRequestReviews.find((r) => r.permission === 'geolocation');
  assert.equal(geolocation.attributionStatus, 'missing-feature');
  assert.equal(geolocation.scopeApproved, false);
  assert.ok(codes(chrome).includes('OPTIONAL_REQUEST_WITHOUT_FEATURE'));
});

test('lists residual requests from optional features without a loaded implementation', () => {
  const report = reviewPackage(syntheticPackage);
  const chrome = report.getVariant('chrome');
  const firefox = report.getVariant('firefox');

  const chromeResidualKeys = chrome.residualOptionalRequests
    .map((r) => `${r.feature}:${r.kind}:${r.identity}`)
    .sort();
  assert.deepEqual(chromeResidualKeys, [
    'cloud-vault:permission:cloudVault.read',
    'cloud-vault:site:https://vault.example.com/*',
    'legacy-sync:permission:identity',
    'legacy-sync:site:https://sync.example.com/legacy/*',
  ]);
  assert.deepEqual(chrome.residualOptionalFeatures.map((f) => f.id), ['legacy-sync', 'cloud-vault']);

  // The same shared file name is loaded in Firefox, so its requests are not residual there.
  assert.equal(firefox.residualOptionalRequests.some((r) => r.feature === 'legacy-sync'), false);
  assert.ok(firefox.residualOptionalRequests.some((r) => r.feature === 'export-pdf'));
  assert.ok(codes(firefox).every((code) => code === 'RESIDUAL_OPTIONAL_REQUEST'));
  assert.equal(firefox.ok, true);
});

test('package verdict is false when one variant has blocking review errors', () => {
  const report = reviewPackage(syntheticPackage);
  assert.equal(report.ok, false);
  assert.equal(report.releaseApproved, false);
  assert.ok(report.summary.errors > 0);
  assert.ok(report.getVariant('firefox').ok);
});

test('strict mode turns residual warnings into release blockers', () => {
  const standard = reviewPackage(syntheticPackage);
  const strict = reviewPackage(syntheticPackage, { strictResidual: true, strict: true });
  assert.equal(standard.getVariant('firefox').releaseApproved, true);
  assert.equal(strict.getVariant('firefox').releaseApproved, false);
  assert.ok(strict.getVariant('firefox').issues.every((i) => i.severity === 'error'));
});

test('does not let approvals or loaded resources from one variant leak into another', () => {
  const pkg = cloneSyntheticPackage();
  // Give Firefox the exact Chrome-only extra permission approval by accident in
  // the shared package approval fallback; variant approvals must still win.
  pkg.approval = { installPermissions: ['management'] };
  const report = reviewPackage(pkg);
  assert.equal(report.getVariant('chrome').extraInstallPermissions[0].permission, 'management');
  assert.equal(report.getVariant('firefox').extraInstallPermissions.length, 0);
});
