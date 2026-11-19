/**
 * Minimal lifecycle adapter used by the process supervisor.
 *
 * The only notification this layer cares about is "a working copy was
 * produced". After such a notification a forked child must not inherit the
 * parent's readiness, so the provider opens a fresh generation and
 * re-initializes lazily on the next request, even when the parent generation
 * had already finished initializing.
 */

export function createLifecycleAdapter() {
  const listeners = new Set();
  let sequence = 0;

  function onWorkingCopy(listener) {
    if (typeof listener !== 'function') {
      throw new TypeError('lifecycle.onWorkingCopy requires a function listener');
    }
    listeners.add(listener);
    let subscribed = true;
    return function unsubscribe() {
      if (!subscribed) return;
      subscribed = false;
      listeners.delete(listener);
    };
  }

  function notifyWorkingCopy(details = {}) {
    sequence += 1;
    const event = Object.freeze({
      ...details,
      type: 'working-copy',
      sequence,
    });
    for (const listener of [...listeners]) {
      listener(event);
    }
    return event;
  }

  return {
    onWorkingCopy,
    notifyWorkingCopy,
    get listenerCount() {
      return listeners.size;
    },
  };
}
