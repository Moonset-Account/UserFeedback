/**
 * End-to-end tests for the command line entry point. They spawn the real CLI so
 * the documented reproduction steps in the README are exercised as written.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const cli = join(root, 'bin', 'admission.mjs');

function run(args) {
  const result = spawnSync(process.execPath, [cli, ...args], {
    cwd: root,
    encoding: 'utf8',
  });
  return result;
}

test('analyze prints an admissible verdict and exits 0', () => {
  const result = run([
    'analyze',
    'examples/admissible.spec.json',
    'examples/admissible.initial.json',
  ]);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /verdict: admissible/);
  assert.match(result.stdout, /accepted \{"notified":"yes","owner":"alice","status":"approved"\}/);
});

test('analyze reports nontermination and includes replayable evidence', () => {
  const result = run([
    'analyze',
    'examples/nonterminating.spec.json',
    'examples/nonterminating.initial.json',
    '--json',
  ]);
  assert.equal(result.status, 0, result.stderr);
  const parsed = JSON.parse(result.stdout);
  assert.equal(parsed.verdict, 'nontermination');
  assert.equal(parsed.nonterminationWitnesses.length, 1);

  const directory = mkdtempSync(join(tmpdir(), 'admission-'));
  const evidencePath = join(directory, 'evidence.json');
  writeFileSync(evidencePath, JSON.stringify(parsed.nonterminationWitnesses[0]));
  const verify = run([
    'verify',
    'examples/nonterminating.spec.json',
    evidencePath,
  ]);
  assert.equal(verify.status, 0, verify.stderr);
  const replay = JSON.parse(verify.stdout);
  assert.equal(replay.ok, true);
  assert.equal(replay.reproduced.closes, true);
});

test('verify exits 1 for tampered evidence', () => {
  const evidence = {
    version: 1,
    kind: 'nontermination',
    programSignature: 'not-the-real-signature',
    programHookIds: ['toB', 'toA'],
    initialObject: { x: 'a' },
    path: [],
    prefix: [],
    cycle: ['toB'],
    expected: {},
  };
  const directory = mkdtempSync(join(tmpdir(), 'admission-'));
  const evidencePath = join(directory, 'tampered.json');
  writeFileSync(evidencePath, JSON.stringify(evidence));
  const result = run([
    'verify',
    'examples/nonterminating.spec.json',
    evidencePath,
  ]);
  assert.equal(result.status, 1);
  const replay = JSON.parse(result.stdout);
  assert.equal(replay.ok, false);
  assert.ok(replay.failures.some((item) => item.code === 'PROGRAM_MISMATCH'));
});

test('round command executes a caller-supplied order', () => {
  const directory = mkdtempSync(join(tmpdir(), 'admission-'));
  const orderPath = join(directory, 'order.json');
  writeFileSync(orderPath, JSON.stringify(['notify', 'assign', 'approve']));
  const result = run([
    'round',
    'examples/admissible.spec.json',
    'examples/admissible.initial.json',
    orderPath,
  ]);
  assert.equal(result.status, 0, result.stderr);
  const parsed = JSON.parse(result.stdout);
  assert.equal(parsed.changed, true);
  // notify and assign ran before approve matched, so only status changed.
  assert.deepEqual(parsed.object, {
    notified: 'no',
    owner: 'none',
    status: 'approved',
  });
});

test('invalid spec exits 1 with a stable error code', () => {
  const directory = mkdtempSync(join(tmpdir(), 'admission-'));
  const specPath = join(directory, 'bad.json');
  writeFileSync(
    specPath,
    JSON.stringify({
      domain: { x: ['a'] },
      hooks: [
        {
          id: 'h',
          when: { kind: 'eq', field: 'x', value: 'a' },
          action: { kind: 'set', field: 'x', value: 'b' },
        },
      ],
      accept: { kind: 'eq', field: 'x', value: 'a' },
    }),
  );
  const result = run(['analyze', specPath, 'examples/nonterminating.initial.json']);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /VALUE_OUT_OF_DOMAIN/);
});

test('README reproduction commands point at files that exist', () => {
  const readme = readFileSync(join(root, 'README.md'), 'utf8');
  for (const example of [
    'examples/admissible.spec.json',
    'examples/nonterminating.spec.json',
    'examples/divergent.spec.json',
  ]) {
    assert.ok(readme.includes(example), `README should reference ${example}`);
  }
});
