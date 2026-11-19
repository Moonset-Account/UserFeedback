import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createRandomBytesProvider } from '../src/random-bytes-provider.mjs';
import { RandomBytesInitializationError } from '../src/errors.mjs';
import { createNumberedRandomSource } from './helpers/numbered-random-source.mjs';
import { byteFor, flush, settle, track } from './helpers/async.mjs';

describe('cancellation is per waiter', () => {
  it('rejects only the aborted waiter and keeps siblings and initialization alive', async () => {
    const source = createNumberedRandomSource({ auto: false });
    const provider = createRandomBytesProvider({ source });

    const controller = new AbortController();
    const victim = track(
      provider.getRandomBytes(8, { label: 'victim', signal: controller.signal }),
    );
    const survivor = track(provider.getRandomBytes(8, { label: 'survivor' }));

    assert.equal(provider.status.pendingRequests, 2);

    controller.abort(new Error('caller went away'));

    const victimOutcome = await victim;
    assert.equal(victimOutcome.ok, false);
    assert.equal(victimOutcome.error.name, 'AbortError');
    assert.equal(victimOutcome.error.code, 'ABORT_ERR');

    // The other waiter is untouched and still pending.
    assert.equal(provider.status.pendingRequests, 1);
    assert.equal(provider.status.state, 'initializing');

    source.resolveInitialization(0);
    const survivorOutcome = await survivor;
    assert.ok(survivorOutcome.ok, 'the non-cancelled request still receives bytes');

    // Only the survivor produced a read.
    assert.equal(source.readCount, 1);
    assert.equal(source.reads[0].label, 'survivor');
    assert.equal(source.reads[0].initializationId, 1);
  });

  it('does not start an initialization for a request aborted before dispatch', async () => {
    const source = createNumberedRandomSource({ auto: false });
    const provider = createRandomBytesProvider({ source });

    const controller = new AbortController();
    controller.abort();
    const outcome = await track(
      provider.getRandomBytes(8, { signal: controller.signal }),
    );

    assert.equal(outcome.ok, false);
    assert.equal(outcome.error.code, 'ABORT_ERR');
    assert.equal(source.initializationCount, 0);
    assert.equal(provider.status.state, 'idle');
  });

  it('the last waiter aborting does not poison the generation for later requests', async () => {
    const source = createNumberedRandomSource({ auto: false });
    const provider = createRandomBytesProvider({ source });

    const controller = new AbortController();
    const first = track(provider.getRandomBytes(8, { signal: controller.signal }));
    controller.abort();
    await first;
    assert.equal(provider.status.pendingRequests, 0);

    const later = track(provider.getRandomBytes(8));
    assert.equal(provider.status.pendingRequests, 1);
    source.resolveInitialization(0);
    const outcome = await later;
    assert.ok(outcome.ok, 'a later request still rides the still-running initialization');
  });
});

describe('initialization failure fans out and is retryable', () => {
  it('fails every same-generation waiter with the same error, then retries', async () => {
    const source = createNumberedRandomSource({ auto: false });
    const provider = createRandomBytesProvider({ source });

    const a = track(provider.getRandomBytes(8, { label: 'a' }));
    const b = track(provider.getRandomBytes(8, { label: 'b' }));

    const bootError = new Error('entropy device unavailable');
    source.rejectInitialization(0, bootError);

    const [ra, rb] = await Promise.all([
      settle(a, 'waiter a'),
      settle(b, 'waiter b'),
    ]);
    assert.equal(ra.ok, false);
    assert.equal(rb.ok, false);
    assert.ok(ra.error instanceof RandomBytesInitializationError);
    assert.equal(ra.error.code, 'ERR_RANDOM_BYTES_INIT');
    // Same generation, same failure: the very same error instance.
    assert.equal(ra.error, rb.error);
    assert.equal(ra.error.cause, bootError);

    assert.equal(provider.status.state, 'failed');
    assert.equal(source.readCount, 0, 'no bytes were produced from a failed initialization');

    // The next request retries with a fresh initialization attempt.
    const retry = track(provider.getRandomBytes(8, { label: 'retry' }));
    assert.equal(source.initializationCount, 2);
    assert.equal(source.initializations[1].attempt, 2);
    assert.equal(source.initializations[1].generation, provider.generation);

    source.resolveInitialization(1);
    const retryOutcome = await retry;
    assert.ok(retryOutcome.ok);
    // The retry is the first *successful* initialization provider-wide.
    assert.deepEqual(retryOutcome.value, new Uint8Array(8).fill(byteFor(1)));
    assert.equal(provider.status.initializationId, 1);
  });

  it('a synchronous throw from initialize is reported as a generation failure', async () => {
    const provider = createRandomBytesProvider({
      source: {
        initialize() {
          throw new Error('boom during activation');
        },
        randomBytes: () => new Uint8Array(1),
      },
    });

    const outcome = await track(provider.getRandomBytes(4));
    assert.equal(outcome.ok, false);
    assert.equal(outcome.error.code, 'ERR_RANDOM_BYTES_INIT');
    assert.equal(provider.status.state, 'failed');
  });

  it('tolerates a hostile thenable that resolves more than once', async () => {
    // A thenable that keeps its resolver can fire again after the provider has
    // already treated the initialization as complete. The double settle must
    // not re-serve requests, bump the initialization id, or change state.
    let lateResolve;
    let calls = 0;
    const provider = createRandomBytesProvider({
      source: {
        initialize() {
          return {
            then(resolve) {
              calls += 1;
              lateResolve = resolve;
            },
          };
        },
        randomBytes(size, context) {
          return new Uint8Array(size).fill(context.initializationId);
        },
      },
    });

    const first = track(provider.getRandomBytes(4));
    // Promise assimilation of the thenable happens on a microtask.
    await flush();
    assert.equal(calls, 1, 'the provider assimilates the thenable exactly once');
    lateResolve();
    const firstOutcome = await first;
    assert.ok(firstOutcome.ok);
    assert.equal(provider.status.initializationId, 1);
    assert.deepEqual([...firstOutcome.value], [1, 1, 1, 1]);

    // A duplicate late resolution must be a no-op.
    lateResolve();
    await flush();
    assert.equal(provider.status.state, 'ready');
    assert.equal(provider.status.initializationId, 1);

    // The generation still serves subsequent requests normally.
    const second = await provider.getRandomBytes(4);
    assert.deepEqual([...second], [1, 1, 1, 1]);
  });

  it('a single read failure rejects only that request and keeps the generation ready', async () => {
    const source = createNumberedRandomSource();
    const provider = createRandomBytesProvider({ source });

    await provider.getRandomBytes(4);
    assert.equal(provider.status.state, 'ready');

    source.failReadWhen((context) => (context.requestId === 2 ? new Error('read hiccup') : null));
    const [bad, good] = await Promise.all([
      track(provider.getRandomBytes(4)),
      track(provider.getRandomBytes(4)),
    ]);

    assert.equal(bad.ok, false);
    assert.equal(bad.error.code, 'ERR_RANDOM_BYTES_READ');
    assert.ok(good.ok);
    assert.equal(provider.status.state, 'ready');
    assert.equal(provider.status.initializationId, 1, 'no re-initialization after a read error');
  });
});

describe('closing the provider', () => {
  it('rejects pending and future requests and unsubscribes from the lifecycle', async () => {
    const source = createNumberedRandomSource({ auto: false });
    const { createLifecycleAdapter } = await import('../src/lifecycle-adapter.mjs');
    const lifecycle = createLifecycleAdapter();
    const provider = createRandomBytesProvider({ source, lifecycle });

    assert.equal(lifecycle.listenerCount, 1);
    const pending = track(provider.getRandomBytes(8));
    provider.close();
    assert.equal(lifecycle.listenerCount, 0);

    const pendingOutcome = await pending;
    assert.equal(pendingOutcome.ok, false);
    assert.equal(pendingOutcome.error.code, 'ERR_RANDOM_BYTES_CLOSED');

    const afterClose = await track(provider.getRandomBytes(8));
    assert.equal(afterClose.error.code, 'ERR_RANDOM_BYTES_CLOSED');

    // A late lifecycle notification after close must not resurrect anything.
    lifecycle.notifyWorkingCopy();
    assert.equal(provider.closed, true);
  });
});
