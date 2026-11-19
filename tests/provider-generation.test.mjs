import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createRandomBytesProvider } from '../src/random-bytes-provider.mjs';
import { createLifecycleAdapter } from '../src/lifecycle-adapter.mjs';
import { createNumberedRandomSource } from './helpers/numbered-random-source.mjs';
import { byteFor, flush, settle, track } from './helpers/async.mjs';

describe('shared initialization per process generation', () => {
  it('serves concurrent same-generation requests from a single initialization', async () => {
    const source = createNumberedRandomSource({ auto: false });
    const provider = createRandomBytesProvider({ source });

    const a = track(provider.getRandomBytes(8, { label: 'a' }));
    const b = track(provider.getRandomBytes(8, { label: 'b' }));
    const c = track(provider.getRandomBytes(32, { label: 'c' }));

    // One initialization for the generation, not one per request.
    assert.equal(source.initializationCount, 1);
    assert.equal(provider.status.state, 'initializing');
    assert.equal(provider.status.pendingRequests, 3);

    source.resolveInitialization(0);
    const [ra, rb, rc] = await Promise.all([a, b, c]);

    assert.ok(ra.ok && rb.ok && rc.ok, 'every caller receives bytes');
    assert.equal(provider.status.state, 'ready');
    assert.equal(provider.status.initializationId, 1);
    assert.equal(source.readCount, 3);

    // Every waiter was attributed to the one successful initialization, and the
    // payload is the marker byte of that initialization.
    for (const read of source.reads) {
      assert.equal(read.initializationId, 1);
      assert.equal(read.generation, provider.generation);
    }
    assert.deepEqual(ra.value, new Uint8Array(8).fill(byteFor(1)));
    assert.deepEqual(rc.value, new Uint8Array(32).fill(byteFor(1)));

    // A ready generation does not re-initialize.
    const d = track(provider.getRandomBytes(4));
    assert.equal((await d).ok, true);
    assert.equal(source.initializationCount, 1);
  });

  it('returns an independent copy so credential material cannot be mutated by the source', async () => {
    const buffer = new Uint8Array(4).fill(7);
    const provider = createRandomBytesProvider({
      source: { randomBytes: () => buffer },
    });

    const bytes = await provider.getRandomBytes(4);
    assert.notEqual(bytes, buffer);
    buffer.fill(0);
    assert.deepEqual([...bytes], [7, 7, 7, 7]);
  });
});

describe('lifecycle notification forces re-initialization per generation', () => {
  it('re-initializes for a working copy even when the parent was ready', async () => {
    const source = createNumberedRandomSource({ auto: false });
    const lifecycle = createLifecycleAdapter();
    const provider = createRandomBytesProvider({ source, lifecycle });

    // Parent generation initializes and becomes ready.
    const parent = track(provider.getRandomBytes(8));
    source.resolveInitialization(0);
    const parentOutcome = await parent;
    assert.ok(parentOutcome.ok);
    assert.equal(provider.status.state, 'ready');
    assert.equal(provider.generation, 1);
    const parentGeneration = provider.generation;

    // The supervisor produces a working copy.
    lifecycle.notifyWorkingCopy();

    assert.equal(provider.generation, parentGeneration + 1);
    assert.equal(provider.status.state, 'idle', 'fresh generation is not inheriting readiness');

    // The child's first request triggers its own initialization.
    const child = track(provider.getRandomBytes(8));
    assert.equal(source.initializationCount, 2, 'a second initialization ran for the child');
    assert.equal(source.initializations[1].generation, provider.generation);

    source.resolveInitialization(1);
    const childOutcome = await child;
    assert.ok(childOutcome.ok);

    // Bytes are attributable to the child initialization, not the parent's.
    assert.equal(provider.status.initializationId, 2);
    assert.deepEqual(childOutcome.value, new Uint8Array(8).fill(byteFor(2)));
    const childReads = source.readsForGeneration(provider.generation);
    assert.equal(childReads.length, 1);
    assert.equal(childReads[0].initializationId, 2);
    assert.deepEqual(childReads[0].bytes, new Uint8Array(8).fill(byteFor(2)));
  });

  it('does not re-initialize when nothing had been initialized yet', async () => {
    const source = createNumberedRandomSource();
    const lifecycle = createLifecycleAdapter();
    const provider = createRandomBytesProvider({ source, lifecycle });

    lifecycle.notifyWorkingCopy();
    assert.equal(provider.generation, 2);
    await provider.getRandomBytes(4);
    assert.equal(source.initializationCount, 1);
    assert.equal(source.initializations[0].generation, 2);
  });
});

describe('late callbacks from an old generation', () => {
  it('ignores a late success from the parent and still initializes the child', async () => {
    const source = createNumberedRandomSource({ auto: false });
    const lifecycle = createLifecycleAdapter();
    const provider = createRandomBytesProvider({ source, lifecycle });

    // Parent initialization is in flight when the working copy is produced.
    const parentWaiter = track(provider.getRandomBytes(8));
    assert.equal(source.initializationCount, 1);

    lifecycle.notifyWorkingCopy();
    assert.equal(provider.generation, 2);
    // Parent waiter cannot be satisfied from the parent's half-done init.
    const parentOutcome = await settle(parentWaiter, 'parent waiter');
    assert.equal(parentOutcome.ok, false);

    // Child request starts the child's own initialization.
    const childWaiter = track(provider.getRandomBytes(8));
    assert.equal(source.initializationCount, 2);
    assert.equal(provider.status.state, 'initializing');

    // The *parent's* initialization now succeeds — late.
    source.resolveInitialization(0);
    await flush();

    assert.equal(
      provider.status.state,
      'initializing',
      'late parent success must not mark the child ready',
    );
    assert.equal(provider.status.pendingRequests, 1);
    assert.equal(provider.generationStatus(1).ignoredLateSuccesses, 1);

    // Child's initialization succeeds and wakes only the child. It is the
    // first *successful* initialization provider-wide (the parent's was
    // ignored), so its id is 1.
    source.resolveInitialization(1);
    const childOutcome = await settle(childWaiter, 'child waiter');
    assert.ok(childOutcome.ok);
    assert.deepEqual(childOutcome.value, new Uint8Array(8).fill(byteFor(1)));
    assert.equal(provider.status.initializationId, 1);
    assert.equal(source.readsForGeneration(2).length, 1);
    assert.equal(source.readsForGeneration(2)[0].attempt, 1);
  });

  it('ignores a late failure from the parent generation without failing the child', async () => {
    const source = createNumberedRandomSource({ auto: false });
    const lifecycle = createLifecycleAdapter();
    const provider = createRandomBytesProvider({ source, lifecycle });

    const parentWaiter = track(provider.getRandomBytes(8));
    lifecycle.notifyWorkingCopy();
    await settle(parentWaiter, 'parent waiter');

    const childWaiter = track(provider.getRandomBytes(8));
    source.rejectInitialization(0, new Error('parent boot failed late'));
    await flush();

    assert.equal(provider.status.state, 'initializing');
    assert.equal(provider.generationStatus(1).ignoredLateFailures, 1);

    source.resolveInitialization(1);
    const childOutcome = await settle(childWaiter, 'child waiter');
    assert.ok(childOutcome.ok);
  });
});
