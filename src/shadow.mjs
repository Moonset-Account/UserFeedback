// Shadow executor for differential verification of two implementations.
//
// Flow:
//   1. runBaseline(tasks) runs the baseline implementation against the real
//      storage driver and, per task marker, records every external read
//      (resource, call order within the task, returned value) and every write.
//   2. runCandidate(tasks) replays the recorded reads to the candidate
//      implementation. Tasks may finish in any order (swapped, interleaved,
//      concurrent): replay pairing is keyed by (task marker, resource,
//      per-task read occurrence), never by global call order.
//   3. Candidate writes go to a per-task overlay only; later reads in the same
//      task see the overlay first. The baseline storage is never mutated.
//   4. An external read that cannot be paired with a baseline observation
//      falls back to the (read-only) fallback driver, the observed difference
//      is retained, and the dependent task result is marked unsubstantiated.

export class UnknownTaskError extends Error {
  constructor(taskId, known) {
    super(
      `candidate task "${taskId}" has no baseline trace; known tasks: ${known.join(', ') || '(none)'}`,
    );
    this.name = 'UnknownTaskError';
    this.taskId = taskId;
  }
}

const clone = (v) => (v === undefined ? undefined : structuredClone(v));

function serializeError(err) {
  if (err === undefined) return undefined;
  if (!(err instanceof Error)) return { name: 'NonError', message: String(err) };
  return { name: err.name || 'Error', message: err.message };
}

class BaselineContext {
  #driver;
  #taskId;
  #trace;

  constructor(driver, taskId, trace) {
    this.#driver = driver;
    this.#taskId = taskId;
    this.#trace = trace;
  }

  async read(resource, options = {}) {
    const value = await this.#driver.read(resource, options);
    const perResourceOccurrence =
      this.#trace.reads.filter((r) => r.resource === resource).length + 1;
    this.#trace.reads.push({
      kind: 'read',
      taskId: this.#taskId,
      resource,
      order: this.#trace.reads.length + 1, // task-global call order
      occurrence: perResourceOccurrence, // n-th read of THIS resource in the task
      value: clone(value),
      source: 'storage',
    });
    return clone(value);
  }

  async write(resource, value, options = {}) {
    const ack = await this.#driver.write(resource, value, options);
    this.#trace.writes.push({
      kind: 'write',
      taskId: this.#taskId,
      resource,
      order: this.#trace.writes.length + 1,
      value: clone(value),
      ack: clone(ack),
    });
    return clone(ack);
  }
}

class CandidateContext {
  #taskId;
  #baselineReads;
  #fallback;
  #state;

  constructor({ taskId, baselineReads, fallbackDriver, state }) {
    this.#taskId = taskId;
    this.#baselineReads = baselineReads;
    this.#fallback = fallbackDriver;
    this.#state = state;
  }

  async read(resource, options = {}) {
    const task = this.#state.tasks.get(this.#taskId);

    // 1) The candidate's own overlay wins: after a write it must read back
    //    its own update. Overlay reads do not consume a baseline replay slot.
    if (task.overlay.has(resource)) {
      const overlayValue = task.overlay.get(resource);
      task.reads.push({
        kind: 'read',
        taskId: this.#taskId,
        resource,
        order: task.reads.length + 1,
        occurrence: task.reads.filter((r) => r.resource === resource).length + 1,
        value: clone(overlayValue),
        source: 'overlay',
      });
      return clone(overlayValue);
    }

    // 2) Pair with the n-th baseline observation of the same resource inside
    //    the SAME task. Pairing ignores when the task runs, so tasks may swap
    //    order or interleave without changing what a task observes.
    const occurrence =
      task.reads.filter((r) => r.source !== 'overlay' && r.resource === resource).length + 1;
    const paired = this.#baselineReads.find(
      (r) => r.taskId === this.#taskId && r.resource === resource && r.occurrence === occurrence,
    );

    if (paired) {
      task.reads.push({
        kind: 'read',
        taskId: this.#taskId,
        resource,
        order: task.reads.length + 1,
        occurrence,
        value: clone(paired.value),
        source: 'replay',
        pairedWith: { order: paired.order, occurrence: paired.occurrence },
      });
      return clone(paired.value);
    }

    // 3) Unpaired read: keep the observed difference instead of fabricating a
    //    value, mark the task (and therefore its result) unsubstantiated.
    task.unsubstantiated = true;
    let observed;
    let fallbackError;
    try {
      observed = await this.#fallback.read(resource, options);
    } catch (err) {
      fallbackError = err;
      observed = undefined;
    }
    const event = {
      kind: 'unpaired-read',
      taskId: this.#taskId,
      resource,
      order: task.reads.length + 1,
      occurrence,
      reason: this.#baselineReads.some(
        (r) => r.taskId === this.#taskId && r.resource === resource,
      )
        ? 'read-occurrence-without-baseline'
        : 'resource-never-read-by-baseline',
      observedValue: clone(observed),
      fallbackError: serializeError(fallbackError),
    };
    task.unpairedReads.push(event);
    task.reads.push({
      kind: 'read',
      taskId: this.#taskId,
      resource,
      order: event.order,
      occurrence,
      value: clone(observed),
      source: 'fallback',
      paired: false,
    });
    if (fallbackError) throw fallbackError;
    return clone(observed);
  }

  async write(resource, value) {
    const task = this.#state.tasks.get(this.#taskId);
    task.overlay.set(resource, clone(value));
    task.writes.push({
      kind: 'write',
      taskId: this.#taskId,
      resource,
      order: task.writes.length + 1,
      value: clone(value),
      layer: 'overlay',
    });
    // The candidate receives a normal-looking ack; nothing reaches storage.
    return { accepted: true, layer: 'overlay', resource };
  }
}

export class ShadowExecutor {
  #driver;
  #fallback;
  #state;

  /**
   * @param {object} driver baseline storage driver ({read, write})
   * @param {object} [options]
   * @param {object} [options.fallbackDriver] read-only driver used when a
   *        candidate read cannot be paired. Defaults to the baseline driver,
   *        which is safe because the candidate path never calls write on it.
   */
  constructor(driver, options = {}) {
    if (!driver || typeof driver.read !== 'function' || typeof driver.write !== 'function') {
      throw new TypeError('driver must implement async read() and write()');
    }
    this.#driver = driver;
    this.#fallback = options.fallbackDriver ?? driver;
    this.#state = {
      baseline: { tasks: new Map(), order: [] },
      candidate: { tasks: new Map(), order: [] },
    };
  }

  /**
   * Run baseline tasks against the real driver and record their external
   * interactions. tasks: [{ id, impl(ctx) }]
   */
  async runBaseline(tasks) {
    for (const task of tasks) {
      if (!task || typeof task.id !== 'string' || typeof task.impl !== 'function') {
        throw new TypeError('each baseline task needs a string id and an impl(ctx) function');
      }
      const trace = { id: task.id, reads: [], writes: [] };
      this.#state.baseline.tasks.set(task.id, trace);
      this.#state.baseline.order.push(task.id);
      const ctx = new BaselineContext(this.#driver, task.id, trace);
      try {
        trace.result = await task.impl(ctx);
        trace.status = 'fulfilled';
      } catch (err) {
        trace.status = 'rejected';
        trace.error = err;
      }
    }
    return this.report();
  }

  /**
   * Run candidate tasks against recorded baseline reads plus an isolated
   * overlay. `tasks` may be in any order; with { concurrent: true } they run
   * together and may interleave arbitrarily.
   */
  async runCandidate(tasks, options = {}) {
    const known = [...this.#state.baseline.tasks.keys()];
    for (const task of tasks) {
      if (!task || typeof task.id !== 'string' || typeof task.impl !== 'function') {
        throw new TypeError('each candidate task needs a string id and an impl(ctx) function');
      }
      if (!this.#state.baseline.tasks.has(task.id)) {
        throw new UnknownTaskError(task.id, known);
      }
      if (this.#state.candidate.tasks.has(task.id)) {
        throw new Error(`candidate task "${task.id}" has already been run`);
      }
      this.#state.candidate.tasks.set(task.id, {
        id: task.id,
        overlay: new Map(),
        reads: [],
        writes: [],
        unpairedReads: [],
        unsubstantiated: false,
        status: 'pending',
      });
    }

    const runOne = async (task) => {
      const slot = this.#state.candidate.tasks.get(task.id);
      this.#state.candidate.order.push(task.id);
      const ctx = new CandidateContext({
        taskId: task.id,
        baselineReads: this.#baselineReads(),
        fallbackDriver: this.#fallback,
        state: this.#state.candidate,
      });
      try {
        slot.result = await task.impl(ctx);
        slot.status = 'fulfilled';
      } catch (err) {
        slot.status = 'rejected';
        slot.error = err;
      }
    };

    if (options.concurrent) {
      await Promise.all(tasks.map(runOne));
    } else {
      for (const task of tasks) await runOne(task);
    }
    return this.report();
  }

  #baselineReads() {
    const all = [];
    for (const trace of this.#state.baseline.tasks.values()) all.push(...trace.reads);
    return all;
  }

  /** Structured comparison report covering all observed constraints. */
  report() {
    const tasks = {};
    for (const [id, base] of this.#state.baseline.tasks) {
      const cand = this.#state.candidate.tasks.get(id);
      tasks[id] = this.#compareTask(base, cand);
    }
    return {
      baselineTaskOrder: [...this.#state.baseline.order],
      candidateTaskOrder: [...this.#state.candidate.order],
      tasks,
      summary: {
        baselineTasks: this.#state.baseline.order.length,
        candidateTasks: this.#state.candidate.order.length,
        allSubstantiated:
          this.#state.candidate.order.length > 0 &&
          this.#state.candidate.order.every(
            (id) => this.#state.candidate.tasks.get(id)?.unsubstantiated === false,
          ),
        anyUnpairedRead: this.#state.candidate.order.some(
          (id) => this.#state.candidate.tasks.get(id)?.unpairedReads.length > 0,
        ),
      },
    };
  }

  #compareTask(base, cand) {
    if (!cand) {
      return {
        taskId: base.id,
        candidateRan: false,
        unsubstantiated: false,
        baseline: this.#traceView(base),
      };
    }

    // Replay integrity: every paired read must have returned exactly the
    // baseline value (the executor guarantees this; the check makes it
    // testable and catches future regressions).
    const replayChecks = cand.reads
      .filter((r) => r.source === 'replay')
      .map((r) => {
        const paired = base.reads.find(
          (b) => b.resource === r.resource && b.occurrence === r.occurrence,
        );
        return {
          resource: r.resource,
          occurrence: r.occurrence,
          equal: paired !== undefined && deepEqual(r.value, paired.value),
        };
      });

    const writeDiffs = this.#writeDiffs(base, cand);

    let resultsEqual = null;
    if (base.status === 'fulfilled' && cand.status === 'fulfilled') {
      resultsEqual = deepEqual(base.result, cand.result);
    } else {
      resultsEqual =
        base.status === cand.status &&
        (base.status === 'rejected'
          ? base.error?.name === cand.error?.name && base.error?.message === cand.error?.message
          : true);
    }

    return {
      taskId: base.id,
      candidateRan: true,
      unsubstantiated: cand.unsubstantiated,
      missingBasis: cand.unpairedReads.map((e) => ({
        resource: e.resource,
        occurrence: e.occurrence,
        reason: e.reason,
        observedValue: e.observedValue,
        fallbackError: e.fallbackError,
      })),
      baseline: this.#traceView(base),
      candidate: {
        status: cand.status,
        result: cand.status === 'fulfilled' ? clone(cand.result) : undefined,
        error: cand.status === 'rejected' ? serializeError(cand.error) : undefined,
        reads: cand.reads.map((r) => ({
          resource: r.resource,
          order: r.order,
          occurrence: r.occurrence,
          source: r.source,
          value: clone(r.value),
        })),
        writes: cand.writes.map((w) => ({
          resource: w.resource,
          order: w.order,
          value: clone(w.value),
          layer: w.layer,
        })),
        overlaySnapshot: Object.fromEntries(
          [...cand.overlay.entries()].map(([k, v]) => [k, clone(v)]),
        ),
      },
      replayChecks,
      writeDiffs,
      resultsEqual,
    };
  }

  #traceView(trace) {
    return {
      status: trace.status,
      result: trace.status === 'fulfilled' ? clone(trace.result) : undefined,
      error: trace.status === 'rejected' ? serializeError(trace.error) : undefined,
      reads: trace.reads.map((r) => ({
        resource: r.resource,
        order: r.order,
        occurrence: r.occurrence,
        value: clone(r.value),
      })),
      writes: trace.writes.map((w) => ({
        resource: w.resource,
        order: w.order,
        value: clone(w.value),
      })),
    };
  }

  #writeDiffs(base, cand) {
    const diffs = [];
    const resources = new Set([
      ...base.writes.map((w) => w.resource),
      ...cand.writes.map((w) => w.resource),
    ]);
    for (const resource of resources) {
      const b = base.writes.filter((w) => w.resource === resource);
      const c = cand.writes.filter((w) => w.resource === resource);
      if (b.length === 0) {
        diffs.push({
          resource,
          kind: 'candidate-only-write',
          candidateValues: c.map((w) => clone(w.value)),
        });
      } else if (c.length === 0) {
        diffs.push({
          resource,
          kind: 'baseline-only-write',
          baselineValues: b.map((w) => clone(w.value)),
        });
      } else {
        const sameSequence =
          b.length === c.length && b.every((w, i) => deepEqual(w.value, c[i].value));
        diffs.push({
          resource,
          kind: 'write-compare',
          sameSequence,
          baselineValues: b.map((w) => clone(w.value)),
          candidateValues: c.map((w) => clone(w.value)),
        });
      }
    }
    return diffs;
  }
}

/** Small structural-deep-equality used by the report (no external deps). */
export function deepEqual(a, b) {
  if (Object.is(a, b)) return true;
  if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object') return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    if (a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => Object.hasOwn(b, k) && deepEqual(a[k], b[k]));
}
