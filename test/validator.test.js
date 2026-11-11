import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import {
  buildWritePlan,
  PlanValidationError,
  ENTRY_TYPE,
  VIOLATION,
  MemoryWriteStub,
  DEFAULT_PATH_RULES,
  comparisonKeyForPath,
  checkEntryPath,
  isInsideRoot,
} from '../src/index.js';

const file = (entryPath, contents = null) => ({ type: ENTRY_TYPE.FILE, entryPath, contents });
const dir = (entryPath) => ({ type: ENTRY_TYPE.DIRECTORY, entryPath });

function expectRejected(fn, codes) {
  assert.throws(fn, (err) => {
    assert.ok(err instanceof PlanValidationError);
    const got = err.violations.map((v) => v.code);
    for (const code of codes) assert.ok(got.includes(code), `expected violation ${code}, got ${got}`);
    return true;
  });
}

describe('declared path rules', () => {
  it('declares forward slash, NFC and explicit case policy', () => {
    assert.equal(DEFAULT_PATH_RULES.separator, '/');
    assert.equal(DEFAULT_PATH_RULES.unicodeNormalization, 'NFC');
    assert.equal(DEFAULT_PATH_RULES.caseComparison, 'sensitive');
  });

  it('refuses structural escape attempts before any target is computed', () => {
    const cases = [
      ['../escape', 'PATH_PARENT_COMPONENT'],
      ['a/../../b', 'PATH_PARENT_COMPONENT'],
      ['..', 'PATH_PARENT_COMPONENT'],
      ['/etc/passwd', 'PATH_ABSOLUTE'],
      ['C:/windows', 'PATH_ABSOLUTE'],
      ['//server/share', 'PATH_ABSOLUTE'],
      ['a\\b', 'PATH_CONTAINS_BACKSLASH'],
      ['a//b', 'PATH_EMPTY_COMPONENT'],
      ['a/b/', 'PATH_EMPTY_COMPONENT'],
      ['a/./b', 'PATH_DOT_COMPONENT'],
      ['a\0b', 'PATH_CONTAINS_NUL'],
      ['', 'PATH_EMPTY_OR_NON_STRING'],
    ];
    for (const [path, reason] of cases) {
      assert.equal(checkEntryPath(path).ok, false, path);
      assert.equal(checkEntryPath(path).reason, reason, path);
    }
  });

  it('treats NFC/NFD spellings as the same comparison key', () => {
    const nfc = ['café'.normalize('NFC'), 'file.txt'];
    const nfd = ['café'.normalize('NFD'), 'file.txt'];
    assert.equal(
      comparisonKeyForPath(nfc),
      comparisonKeyForPath(nfd),
    );
  });

  it('folds case only under the insensitive rule', () => {
    const a = ['README.md'];
    const b = ['readme.md'];
    assert.notEqual(
      comparisonKeyForPath(a, DEFAULT_PATH_RULES),
      comparisonKeyForPath(b, DEFAULT_PATH_RULES),
    );
    assert.equal(
      comparisonKeyForPath(a, { ...DEFAULT_PATH_RULES, caseComparison: 'insensitive' }),
      comparisonKeyForPath(b, { ...DEFAULT_PATH_RULES, caseComparison: 'insensitive' }),
    );
  });

  it('isInsideRoot is an explicit containment gate', () => {
    assert.equal(isInsideRoot(['a', 'b']), true);
    assert.equal(isInsideRoot([]), false);
    assert.equal(isInsideRoot(['..']), false);
    assert.equal(isInsideRoot(['a/b']), false);
  });
});

describe('buildWritePlan — accepted plans', () => {
  it('plans a normal nested directory tree with deterministic ordering', () => {
    const entries = [
      file('plugins/p/locale/en/messages.json', {}),
      dir('plugins/p/locale'),
      file('plugins/p/index.js', '// code'),
      dir('plugins/p'),
      file('plugins/p/locale/zh/messages.json', {}),
    ];

    const plan = buildWritePlan(entries, {}, { rootId: 'isolated-xyz' });

    assert.equal(plan.rootId, 'isolated-xyz');
    // Implicit parent directories are materialized, dirs before files,
    // each group ascending by target path.
    assert.deepEqual(
      plan.operations.map((op) => [op.action, op.type, op.targetPath]),
      [
        ['mkdir', 'directory', 'plugins'],
        ['mkdir', 'directory', 'plugins/p'],
        ['mkdir', 'directory', 'plugins/p/locale'],
        ['mkdir', 'directory', 'plugins/p/locale/en'],
        ['mkdir', 'directory', 'plugins/p/locale/zh'],
        ['writeFile', 'file', 'plugins/p/index.js'],
        ['writeFile', 'file', 'plugins/p/locale/en/messages.json'],
        ['writeFile', 'file', 'plugins/p/locale/zh/messages.json'],
      ],
    );
  });

  it('recomputes a stable plan for identical inputs (deterministic)', () => {
    const entries = [
      file('z/last.txt'),
      dir('a'),
      file('a/middle.txt'),
      dir('a/nested'),
      file('a/nested/deep.bin', 0),
    ];
    const first = buildWritePlan(entries, {});
    const second = buildWritePlan(entries, {});
    assert.deepEqual(first, second);

    // Shuffling input order must not change the plan.
    const shuffled = [...entries].reverse();
    assert.deepEqual(buildWritePlan(shuffled, {}), first);
  });

  it('merges when the target already has the same-named directory', () => {
    const target = {
      plugins: { type: 'directory' },
      'plugins/p': { type: 'directory' },
    };
    const plan = buildWritePlan(
      [dir('plugins/p'), dir('plugins/p/new'), file('plugins/p/new/run.js', 1)],
      target,
    );
    assert.deepEqual(
      plan.operations.map((op) => [op.action, op.targetPath]),
      [
        ['noop', 'plugins'],
        ['noop', 'plugins/p'],
        ['mkdir', 'plugins/p/new'],
        ['writeFile', 'plugins/p/new/run.js'],
      ],
    );
  });

  it('turns an existing same-path file into a noop instead of overwriting', () => {
    const target = { 'keep.txt': { type: 'file' } };
    const plan = buildWritePlan([file('keep.txt', 'new bytes')], target);
    assert.equal(plan.operations[0].action, 'noop');
  });
});

describe('buildWritePlan — normalization and case collisions', () => {
  it('rejects entries that collapse to the same path after NFC normalization', () => {
    const nfcName = 'résumé.txt';
    const nfdName = 'résumé.txt'.normalize('NFD');
    assert.notEqual(nfcName, nfdName);

    expectRejected(
      () => buildWritePlan([file(nfcName), file(nfdName)], {}),
      [VIOLATION.DUPLICATE_ENTRY],
    );
  });

  it('rejects case-variant duplicates only under insensitive comparison', () => {
    const entries = [file('Readme.md'), file('README.md')];

    // Sensitive default: distinct files, both planned.
    const sensitive = buildWritePlan(entries, {});
    assert.equal(sensitive.operations.filter((op) => op.action === 'writeFile').length, 2);

    expectRejected(
      () =>
        buildWritePlan(entries, {}, {
          rules: { ...DEFAULT_PATH_RULES, caseComparison: 'insensitive' },
        }),
      [VIOLATION.DUPLICATE_ENTRY],
    );
  });

  it('rejects an entry whose NFC path collides with an NFD-named target tree node', () => {
    // Existing target node is a directory; the NFC-spelled entry is a file,
    // so equal comparison keys must surface as a file/directory conflict.
    const target = { ['café'.normalize('NFD')]: { type: 'directory' } };
    expectRejected(() => buildWritePlan([file('café'.normalize('NFC'))], target), [
      VIOLATION.TARGET_TYPE_CONFLICT,
    ]);

    // Same type across normalization boundaries is compatible (noop).
    const sameType = buildWritePlan(
      [file('café'.normalize('NFC'), 'x')],
      { ['café'.normalize('NFD')]: { type: 'file' } },
    );
    assert.equal(sameType.operations[0].action, 'noop');
  });
});

describe('buildWritePlan — type gate and conflicts', () => {
  it('rejects link entries and any non-file/non-directory type', () => {
    const entries = [
      { type: 'symlink', entryPath: 'link', linkTarget: '/etc/passwd' },
      { type: 'hardlink', entryPath: 'hard', linkTarget: 'orig' },
      { type: 'block-device', entryPath: 'dev/sda' },
      { type: 'file', entryPath: 'ok.txt' },
    ];
    expectRejected(() => buildWritePlan(entries, {}), [VIOLATION.UNSUPPORTED_ENTRY_TYPE]);

    let thrown;
    try {
      buildWritePlan(entries, {});
    } catch (err) {
      thrown = err;
    }
    assert.equal(thrown.violations.filter((v) => v.code === VIOLATION.UNSUPPORTED_ENTRY_TYPE).length, 3);
  });

  it('rejects file/directory conflicts with the existing target', () => {
    expectRejected(
      () => buildWritePlan([dir('x')], { x: { type: 'file' } }),
      [VIOLATION.TARGET_TYPE_CONFLICT],
    );
    expectRejected(
      () => buildWritePlan([file('x')], { x: { type: 'directory' } }),
      [VIOLATION.TARGET_TYPE_CONFLICT],
    );
  });

  it('rejects entries nested beneath a file (in batch and in target)', () => {
    expectRejected(
      () => buildWritePlan([file('a/b'), file('a/b/c')], {}),
      [VIOLATION.FILE_ANCESTOR_CONFLICT],
    );
    expectRejected(
      () => buildWritePlan([file('a/b/c')], { a: { type: 'file' } }),
      [VIOLATION.TARGET_ANCESTOR_IS_FILE],
    );
  });

  it('rejects traversal entries even when mixed with valid ones', () => {
    expectRejected(
      () =>
        buildWritePlan(
          [file('good.txt'), file('../evil.txt'), file('/abs/evil'), dir('sub')],
          {},
        ),
      [VIOLATION.PATH_PARENT_COMPONENT, VIOLATION.PATH_ABSOLUTE],
    );
  });
});

describe('all-or-nothing batch semantics with the in-memory writer', () => {
  it('leaves zero partial changes when a mixed valid/invalid batch is rejected', () => {
    const initial = { root: { type: 'directory' } };
    const writer = new MemoryWriteStub(initial);
    const before = writer.snapshot();

    const entries = [
      file('root/good-one.txt', 'a'),
      file('root/../../escape.txt', 'evil'),
      { type: 'symlink', entryPath: 'root/link', linkTarget: '/etc/passwd' },
      file('root/good-two.txt', 'b'),
    ];

    assert.throws(() => buildWritePlan(entries, initial));
    // No plan ever reaches the writer; its tree and call log are untouched.
    assert.deepEqual(writer.snapshot(), before);
    assert.deepEqual(writer.calls, []);
  });

  it('applies a valid plan and a recomputed plan is stable across the write', () => {
    const entries = [
      dir('plugin'),
      file('plugin/main.js', 'export {}'),
      file('plugin/data/names.json', ['x']),
    ];
    const writer = new MemoryWriteStub();

    const plan = buildWritePlan(entries, {});
    const committed = writer.applyPlan(plan);
    // plugin, plugin/data (2 dirs) + main.js, names.json (2 files)
    assert.equal(committed.length, 4);

    const after = writer.snapshot();
    assert.equal(after['plugin'].type, 'directory');
    assert.equal(after['plugin/main.js'].contents, 'export {}');
    assert.deepEqual(after['plugin/data/names.json'].contents, ['x']);

    // Recompute against the now-populated tree: same ordered shape, all
    // actions degraded to noop, and applying it changes nothing.
    const replay = buildWritePlan(entries, after);
    assert.deepEqual(
      replay.operations.map((op) => op.targetPath),
      plan.operations.map((op) => op.targetPath),
    );
    assert.ok(replay.operations.every((op) => op.action === 'noop'));
    const treeBeforeReplay = writer.snapshot();
    const replayCalls = writer.applyPlan(replay);
    // Replay is all no-ops: the virtual tree is byte-for-byte unchanged.
    assert.deepEqual(writer.snapshot(), treeBeforeReplay);
    assert.ok(replayCalls.every((op) => op.action === 'noop'));
  });

  it('rolls back entirely if a plan fails mid-application (no partial writes)', () => {
    const writer = new MemoryWriteStub({ existing: { type: 'file' } });
    const before = writer.snapshot();

    const badPlan = {
      operations: [
        { action: 'mkdir', type: 'directory', targetPath: 'new-dir' },
        { action: 'writeFile', type: 'file', targetPath: 'existing', contents: 'x' },
      ],
    };
    assert.throws(() => writer.applyPlan(badPlan));
    assert.deepEqual(writer.snapshot(), before);
    assert.deepEqual(writer.calls, []);
  });

  it('refuses to apply an operation that escapes the root even if handed a forged plan', () => {
    const writer = new MemoryWriteStub();
    const forged = {
      operations: [{ action: 'writeFile', type: 'file', targetPath: '../escape', contents: 1 }],
    };
    assert.throws(() => writer.applyPlan(forged));
    assert.deepEqual(writer.snapshot(), {});
  });
});
