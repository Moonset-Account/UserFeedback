// Local synthetic fixtures for the shadow executor tests.
// Nothing here talks to a real external system: the "external world" is an
// in-memory queue driver.

// Driver returning per-resource FIFO queued values. Reads pop the next value;
// an exhausted queue yields undefined. Writes are recorded so tests can prove
// the candidate side never reaches the driver.
export class QueueStorage {
  #queues = new Map();
  #writes = [];
  #reads = [];

  constructor(queues) {
    for (const [resource, values] of Object.entries(queues)) {
      this.#queues.set(resource, [...values]);
    }
  }

  async read(resource) {
    const q = this.#queues.get(resource);
    const value = q && q.length > 0 ? q.shift() : undefined;
    this.#reads.push({ resource, value });
    return value === undefined ? undefined : structuredClone(value);
  }

  async write(resource, value) {
    this.#writes.push({ resource, value: structuredClone(value) });
    return { accepted: true, layer: 'storage', resource };
  }

  get writes() {
    return this.#writes.map((w) => structuredClone(w));
  }

  get reads() {
    return this.#reads.map((r) => structuredClone(r));
  }
}

// Resolvable promise gate for forcing precise interleavings.
export function gate() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

// ---- Baseline implementations (the "known good" side) ---------------------

export const baselineTaskA = {
  id: 'task-a',
  async impl(ctx) {
    const a = await ctx.read('R-A');
    const b = await ctx.read('R-B');
    const sum = a + b.value;
    await ctx.write('W-A', sum);
    return { task: 'a', sum };
  },
};

export const baselineTaskB = {
  id: 'task-b',
  async impl(ctx) {
    const c1 = await ctx.read('R-C');
    const c2 = await ctx.read('R-C');
    const delta = c2 - c1;
    await ctx.write('W-B', delta);
    return { task: 'b', delta };
  },
};

// Reads a resource, writes it, then reads it again: baseline sees the storage
// write; the candidate must instead see its own overlay value.
export const baselineTaskC = {
  id: 'task-c',
  async impl(ctx) {
    const d = await ctx.read('R-D');
    await ctx.write('W-C', d * 10);
    const back = await ctx.read('W-C');
    return { task: 'c', d, readBack: back };
  },
};

// ---- Candidate implementations (the trial side) ---------------------------

export const candidateTaskA = {
  id: 'task-a',
  async impl(ctx) {
    const a = await ctx.read('R-A');
    const b = await ctx.read('R-B');
    const sum = a + b.value;
    await ctx.write('W-A', sum);
    return { task: 'a', sum };
  },
};

export const candidateTaskB = {
  id: 'task-b',
  async impl(ctx) {
    const c1 = await ctx.read('R-C');
    const c2 = await ctx.read('R-C');
    const delta = c2 - c1;
    await ctx.write('W-B', delta);
    return { task: 'b', delta };
  },
};

export const candidateTaskC = {
  id: 'task-c',
  async impl(ctx) {
    const d = await ctx.read('R-D');
    await ctx.write('W-C', d * 10);
    const back = await ctx.read('W-C');
    return { task: 'c', d, readBack: back };
  },
};

// Candidate that performs an extra read of a resource the baseline never read.
export const candidateTaskAWithUnknownRead = {
  id: 'task-a',
  async impl(ctx) {
    const a = await ctx.read('R-A');
    const extra = await ctx.read('R-NEW'); // unpaired: never read by baseline
    const b = await ctx.read('R-B');
    const sum = a + b.value + (extra ?? 0);
    await ctx.write('W-A', sum);
    return { task: 'a', sum, extra };
  },
};

// Candidate that reads R-A one more time than the baseline did.
export const candidateTaskAWithExtraOccurrence = {
  id: 'task-a',
  async impl(ctx) {
    const a1 = await ctx.read('R-A');
    const a2 = await ctx.read('R-A'); // unpaired: no 2nd baseline occurrence
    const b = await ctx.read('R-B');
    const sum = a1 + (a2 ?? 0) + b.value;
    await ctx.write('W-A', sum);
    return { task: 'a', sum };
  },
};
