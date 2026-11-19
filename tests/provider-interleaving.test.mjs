import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createRandomBytesProvider } from '../src/random-bytes-provider.mjs';
import { createLifecycleAdapter } from '../src/lifecycle-adapter.mjs';
import { createNumberedRandomSource } from './helpers/numbered-random-source.mjs';
import { byteFor, flush, settle, track } from './helpers/async.mjs';

describe('interleaved cancellation, supersede, and late callbacks', () => {
  it('keeps three generations of requests, one abort, and a late parent success straight', async () => {
    const source = createNumberedRandomSource({ auto: false });
    const lifecycle = createLifecycleAdapter();
    const provider = createRandomBytesProvider({ source, lifecycle });

    // Generation 1: two waiters on one in-flight initialization.
    const controller = new AbortController();
    const gen1Aborted = track(
      provider.getRandomBytes(8, { label: 'g1-aborted', signal: controller.signal }),
    );
    const gen1Survivor = track(provider.getRandomBytes(8, { label: 'g1-survivor' }));
    assert.equal(source.initializationCount, 1);
    assert.equal(provider.status.pendingRequests, 2);

    // The aborted waiter leaves alone; the survivor remains.
    controller.abort('caller gone');
    const abortedOutcome = await settle(gen1Aborted, 'g1 aborted waiter');
    assert.equal(abortedOutcome.error.code, 'ABORT_ERR');
    assert.equal(provider.status.pendingRequests, 1);
    assert.equal(source.initializationCount, 1, 'abort did not restart initialization');

    // A working copy supersedes generation 1 while its init is still running.
    lifecycle.notifyWorkingCopy();
    const survivorOutcome = await settle(gen1Survivor, 'g1 survivor');
    assert.equal(survivorOutcome.ok, false);
    assert.equal(survivorOutcome.error.code, 'ERR_RANDOM_BYTES_GENERATION_SUPERSEDED');
    assert.equal(provider.generation, 2);
    assert.equal(provider.status.state, 'idle');

    // Generation 2 issues two concurrent requests -> exactly one init.
    const gen2a = track(provider.getRandomBytes(8, { label: 'g2-a' }));
    const gen2b = track(provider.getRandomBytes(8, { label: 'g2-b' }));
    assert.equal(source.initializationCount, 2);
    assert.equal(source.initializations[1].generation, 2);

    // Generation 1's initialization finally succeeds — too late.
    source.resolveInitialization(0);
    await flush();
    assert.equal(provider.status.state, 'initializing');
    assert.equal(provider.status.pendingRequests, 2, 'gen1 late success woke nobody in gen2');
    assert.equal(source.readsForGeneration(1).length, 0, 'no reads attributed to gen1');
    assert.equal(provider.generationStatus(1).ignoredLateSuccesses, 1);

    // Generation 2's own initialization succeeds, serving both waiters with
    // bytes attributable to that one successful initialization.
    source.resolveInitialization(1);
    const [ra, rb] = await Promise.all([
      settle(gen2a, 'g2-a'),
      settle(gen2b, 'g2-b'),
    ]);
    assert.ok(ra.ok && rb.ok);
    const successfulId = provider.status.initializationId;
    assert.equal(successfulId, 1, 'gen2 init is the first provider-wide success');
    assert.deepEqual(ra.value, new Uint8Array(8).fill(byteFor(successfulId)));
    assert.deepEqual(rb.value, new Uint8Array(8).fill(byteFor(successfulId)));

    const gen2Reads = source.readsForGeneration(2);
    assert.equal(gen2Reads.length, 2);
    assert.deepEqual(
      gen2Reads.map((read) => read.label).sort(),
      ['g2-a', 'g2-b'],
    );
    for (const read of gen2Reads) {
      assert.equal(read.initializationId, successfulId);
      assert.equal(read.attempt, 1);
    }

    // A third generation proves readiness is not inherited from gen2.
    lifecycle.notifyWorkingCopy();
    assert.equal(provider.status.state, 'idle');
    const gen3Pending = track(provider.getRandomBytes(8, { label: 'g3' }));
    assert.equal(source.initializationCount, 3);
    source.resolveInitialization(2);
    const gen3 = await settle(gen3Pending, 'g3');
    assert.ok(gen3.ok);
    assert.equal(provider.status.initializationId, 2);
    assert.deepEqual(gen3.value, new Uint8Array(8).fill(byteFor(2)));

    // Nothing from generation 1 or 2 received bytes.
    assert.equal(source.readsForGeneration(1).length, 0);
    assert.equal(source.readsForGeneration(3).length, 1);
  });
});
