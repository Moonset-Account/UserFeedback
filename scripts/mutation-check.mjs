#!/usr/bin/env node
// Mutation check: apply one deliberate defect at a time to a copy of src/ and
// confirm the acceptance suite fails. Run from anywhere; all output stays in
// the OS temp directory.
import { cp, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const ROOT = resolve(import.meta.dirname, '..');
const SRC = join(ROOT, 'src', 'random-bytes-provider.mjs');

const mutations = [
  {
    name: 'late callback wakes the new generation (attempt keyed without generation identity)',
    find: `  function isCurrentAttempt(generationRecord, attempt) {
    return (
      !closed &&
      generationRecord.active &&
      current === generationRecord &&
      generationRecord.state === 'initializing' &&
      generationRecord.attempt === attempt
    );
  }`,
    replace: `  function isCurrentAttempt(generationRecord, attempt) {
    return (
      !closed &&
      current.state === 'initializing' &&
      current.attempt === attempt
    );
  }`,
  },
  {
    name: 'abort cancels the whole generation instead of one waiter',
    find: `    const abortHandler = () => {
      if (waiter.settled) return;
      generationRecord.waiters.delete(waiter.id);
      settleWaiter(waiter, 'reject', new RandomBytesRequestAbortedError(signal.reason));
    };`,
    replace: `    const abortHandler = () => {
      for (const other of [...generationRecord.waiters.values()]) {
        generationRecord.waiters.delete(other.id);
        settleWaiter(other, 'reject', new RandomBytesRequestAbortedError(signal.reason));
      }
    };`,
  },
  {
    name: 'initialization failure only rejects the first waiter',
    find: `    for (const waiter of pending) {
      // Same error instance for every waiter of the generation.
      settleWaiter(waiter, 'reject', error);
    }`,
    replace: `    settleWaiter(pending[0], 'reject', error);`,
  },
  {
    name: 'working copy does not open a new generation',
    find: `    unsubscribe = lifecycle.onWorkingCopy(() => {
      beginGeneration({ reason: 'working-copy' });
    });`,
    replace: `    unsubscribe = lifecycle.onWorkingCopy(() => {});`,
  },
  {
    name: 'each request initializes instead of sharing one initialization',
    find: `    if (generationRecord.state === 'idle' || generationRecord.state === 'failed') {`,
    replace: `    if (true) {`,
  },
  {
    name: 'failed initialization is never retried',
    find: `    if (generationRecord.state === 'idle' || generationRecord.state === 'failed') {`,
    replace: `    if (generationRecord.state === 'idle') {`,
  },
];

let allCaught = true;

for (const [index, mutation] of mutations.entries()) {
  const dir = await mkdtemp(join(tmpdir(), 'rbp-mutation-'));
  try {
    await cp(join(ROOT, 'src'), join(dir, 'src'), { recursive: true });
    await cp(join(ROOT, 'tests'), join(dir, 'tests'), { recursive: true });
    await writeFile(join(dir, 'package.json'), '{"type":"module"}\n');

    const target = join(dir, 'src', 'random-bytes-provider.mjs');
    const original = await readFile(target, 'utf8');
    if (!original.includes(mutation.find)) {
      throw new Error(`mutation ${index + 1} anchor not found: ${mutation.name}`);
    }
    await writeFile(target, original.replace(mutation.find, mutation.replace));

    const result = spawnSync(
      process.execPath,
      ['--test', '--test-timeout=5000', 'tests/*.test.mjs'],
      { cwd: dir, encoding: 'utf8', timeout: 60_000 },
    );
    const failed = /ℹ fail (\d+)/.exec(result.stdout)?.[1] ?? '?';
    const caught = result.status !== 0;
    allCaught &&= caught;
    console.log(
      `${caught ? 'CAUGHT' : 'MISSED'}  fail=${failed}  ${mutation.name}`,
    );
    if (!caught) {
      console.log(result.stdout.split('\n').slice(-20).join('\n'));
    }
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

if (!allCaught) {
  console.error('\nAt least one mutation slipped through the acceptance suite.');
  process.exit(1);
}
console.log('\nAll mutations were caught by the acceptance suite.');
