import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadManifest } from '../src/manifest.js';

const validBase = () => ({
  builds: [{ id: 'linux', budgetMinutes: 30, requiredFixes: ['FIX-A'] }],
  cases: [
    { id: 'C1', durationMinutes: 5, builds: ['linux'], fixes: ['FIX-A'], mandatory: true, dependencies: [] },
  ],
});

test('合法清单通过校验并完成归一化（去重、排序）', () => {
  const m = loadManifest(
    JSON.stringify({
      builds: [{ id: 'linux', requiredFixes: ['FIX-B', 'FIX-A'] }],
      defaults: { budgetMinutes: 40 },
      cases: [
        {
          id: 'C1',
          durationMinutes: 5,
          builds: ['linux'],
          fixes: ['FIX-B', 'FIX-A', 'FIX-A'],
          mandatory: true,
        },
      ],
    }),
  );
  assert.equal(m.builds[0].budgetMinutes, 40);
  assert.deepEqual(m.builds[0].requiredFixes, ['FIX-A', 'FIX-B']);
  assert.deepEqual(m.cases[0].fixes, ['FIX-A', 'FIX-B']);
});

test('非法 JSON 报 INVALID_JSON', () => {
  assert.throws(() => loadManifest('{not json'), (err) => err.code === 'INVALID_JSON');
});

test('缺少必填字段、负耗时等 schema 问题被收集为 VALIDATION_FAILED', () => {
  const bad = {
    builds: [{ id: 'linux', budgetMinutes: -1, requiredFixes: [] }],
    cases: [
      { id: '', durationMinutes: 1, builds: ['linux'], fixes: [] },
      { id: 'C2', durationMinutes: -3, builds: ['linux'], fixes: [] },
      { id: 'C3', durationMinutes: 2, builds: [], fixes: [] },
    ],
  };
  assert.throws(
    () => loadManifest(JSON.stringify(bad)),
    (err) => {
      assert.equal(err.code, 'VALIDATION_FAILED');
      assert.ok(err.details.length >= 3);
      return true;
    },
  );
});

test('重号用例仅在构建范围不相交时合法（“只在某构建有效的重复用例”）', () => {
  const ok = validBase();
  ok.builds.push({ id: 'arm', budgetMinutes: 30, requiredFixes: ['FIX-A'] });
  ok.cases.push({ id: 'C1', durationMinutes: 9, builds: ['arm'], fixes: ['FIX-A'] });
  const m = loadManifest(JSON.stringify(ok));
  assert.equal(m.cases.length, 2);
  assert.deepEqual(
    m.caseIndex.get('C1').map((c) => c.builds),
    [['linux'], ['arm']],
  );
});

test('重号用例在同一构建上同时有效时报 DUPLICATE_CASE_SCOPE_OVERLAP', () => {
  const bad = validBase();
  bad.cases.push({ id: 'C1', durationMinutes: 2, builds: ['linux'], fixes: ['FIX-A'] });
  assert.throws(
    () => loadManifest(JSON.stringify(bad)),
    (err) => {
      assert.equal(err.code, 'VALIDATION_FAILED');
      assert.ok(err.details.some((d) => d.code === 'DUPLICATE_CASE_SCOPE_OVERLAP'));
      return true;
    },
  );
});

test('依赖不存在报 MISSING_DEPENDENCY；依赖不适用于同构建报 DEPENDENCY_NOT_APPLICABLE', () => {
  const missing = validBase();
  missing.cases[0].dependencies = ['GHOST'];
  assert.throws(
    () => loadManifest(JSON.stringify(missing)),
    (err) => err.details.some((d) => d.code === 'MISSING_DEPENDENCY'),
  );

  const notApplicable = validBase();
  notApplicable.builds.push({ id: 'arm', budgetMinutes: 30, requiredFixes: [] });
  notApplicable.cases[0].dependencies = ['C2'];
  notApplicable.cases.push({ id: 'C2', durationMinutes: 1, builds: ['arm'], fixes: [] });
  assert.throws(
    () => loadManifest(JSON.stringify(notApplicable)),
    (err) => err.details.some((d) => d.code === 'DEPENDENCY_NOT_APPLICABLE'),
  );

  // 重号条目分别适用不同构建时，依赖可以正确解析到对应条目
  const resolved = validBase();
  resolved.builds.push({ id: 'arm', budgetMinutes: 30, requiredFixes: ['FIX-A'] });
  resolved.cases[0].dependencies = ['C2'];
  resolved.cases.push({ id: 'C2', durationMinutes: 1, builds: ['linux'], fixes: [] });
  resolved.cases.push({ id: 'C2', durationMinutes: 2, builds: ['arm'], fixes: ['FIX-A'] });
  assert.doesNotThrow(() => loadManifest(JSON.stringify(resolved)));
});

test('引用未声明构建报 UNKNOWN_BUILD_REF；构建 id 重复报 DUPLICATE_BUILD', () => {
  const badRef = validBase();
  badRef.cases[0].builds = ['linux', 'solaris'];
  assert.throws(
    () => loadManifest(JSON.stringify(badRef)),
    (err) => err.details.some((d) => d.code === 'UNKNOWN_BUILD_REF'),
  );

  const dupBuild = validBase();
  dupBuild.builds.push({ id: 'linux', budgetMinutes: 1, requiredFixes: [] });
  assert.throws(
    () => loadManifest(JSON.stringify(dupBuild)),
    (err) => err.details.some((d) => d.code === 'DUPLICATE_BUILD'),
  );
});
