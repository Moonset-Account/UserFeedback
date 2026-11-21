/**
 * Alert notification coordinator for start/recover alert events.
 *
 * A suppression graph describes which upstream root-cause nodes suppress which
 * downstream symptom nodes.  The graph must be acyclic.  Symptoms debounce
 * their first notification for an aggregation window, and they stay held while
 * any transitive upstream node still has an active fault.
 *
 * The coordinator is deliberately deterministic: `replay()` consumes a
 * complete synthetic timeline, processes all events that share a timestamp
 * before evaluating aggregation deadlines for that timestamp, and emits
 * notifications with a monotonically increasing sequence number.
 *
 * Event shape:
 *   { kind: 'start' | 'recover', faultId: string, nodeId: string, at: number }
 *
 * Notification shape:
 *   { seq, kind: 'start', faultId, nodeId, at, trigger }
 *   { seq, kind: 'recover', faultId, nodeId, at, trigger: 'recovery',
 *     pairedWithSeq, heldWhileSuppressed }
 *
 * `at` values are caller-defined milliseconds.  The coordinator never reads a
 * wall clock.
 */

export const START = 'start';
export const RECOVER = 'recover';

const EMPTY_SET = Object.freeze(new Set());

const START_TRIGGERS = Object.freeze({
  ROOT_CAUSE: 'root-cause',
  AGGREGATION_ELAPSED: 'aggregation-elapsed',
  SUPPRESSION_CLEARED: 'suppression-cleared',
});

function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0;
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Create a coordinator.
 *
 * @param {object} options
 * @param {Array<{cause: string, symptom: string}>} [options.suppressions]
 *        Directed edges.  `cause` is the upstream suppressor of `symptom`.
 * @param {number} options.aggregationMs
 *        Debounce window applied to symptom nodes (nodes that have at least
 *        one transitive ancestor).  Root-cause nodes notify immediately.
 * @param {(notification: object) => void} [options.onNotification]
 *        Optional synchronous sink, called in emission order.
 */
export function createNotificationCoordinator(options = {}) {
  return new NotificationCoordinator(options);
}

export class NotificationCoordinator {
  constructor(options = {}) {
    if (!isRecord(options)) {
      throw new TypeError('options must be an object');
    }

    const { suppressions = [], aggregationMs, onNotification } = options;

    if (typeof aggregationMs !== 'number' || !Number.isFinite(aggregationMs) || aggregationMs < 0) {
      throw new TypeError('aggregationMs must be a finite, non-negative number');
    }
    if (!Array.isArray(suppressions)) {
      throw new TypeError('suppressions must be an array');
    }
    if (onNotification !== undefined && typeof onNotification !== 'function') {
      throw new TypeError('onNotification must be a function when provided');
    }

    const parents = new Map();
    const nodes = new Set();

    suppressions.forEach((edge, index) => {
      if (!isRecord(edge)) {
        throw new TypeError(`suppressions[${index}] must be an object`);
      }
      const { cause, symptom } = edge;
      if (!isNonEmptyString(cause) || !isNonEmptyString(symptom)) {
        throw new TypeError(
          `suppressions[${index}] must have non-empty string cause and symptom`,
        );
      }
      if (cause === symptom) {
        throw new Error(`Suppression graph contains a cycle: ${cause} -> ${symptom}`);
      }
      nodes.add(cause);
      nodes.add(symptom);
      if (!parents.has(symptom)) parents.set(symptom, new Set());
      parents.get(symptom).add(cause);
    });

    const depths = new Map();
    const ancestors = new Map();

    assertAcyclic(nodes, parents);
    for (const node of nodes) {
      collectAncestors(node, parents, ancestors);
      computeDepth(node, parents, depths);
    }

    this.aggregationMs = aggregationMs;
    this._parents = parents;
    this._ancestors = ancestors;
    this._depths = depths;
    this._onNotification = onNotification ?? null;

    /** @type {Map<string, object>} faultId -> incident */
    this._incidents = new Map();
    /** @type {Map<string, number>} nodeId -> number of active faults */
    this._activeByNode = new Map();

    this._notifications = [];
    this._seq = 0;
    this._incidentSeq = 0;
  }

  /** Immutable-in-practice snapshot of emitted notifications, in order. */
  get notifications() {
    return this._notifications.map((notification) => ({ ...notification }));
  }

  /**
   * Replay a complete timeline.  Events are stably ordered by `at`; events
   * with the same `at` keep their input order.  For every timestamp:
   *
   *   1. deadlines strictly before the timestamp are evaluated;
   *   2. all events at the timestamp are handled in input order;
   *   3. deadlines at the timestamp are evaluated.
   *
   * Afterwards, remaining deadlines are evaluated in chronological order.
   *
   * @param {Array<object>} events
   * @returns {Array<object>} notifications
   */
  replay(events) {
    if (!Array.isArray(events)) {
      throw new TypeError('events must be an array');
    }

    const timeline = events
      .map((event, index) => normalizeEvent(event, index))
      .sort((a, b) => a.at - b.at || a.index - b.index);

    let cursor = 0;
    while (cursor < timeline.length) {
      const at = timeline[cursor].at;
      this._flushExpiries(at, false);

      let end = cursor;
      while (end < timeline.length && timeline[end].at === at) end += 1;

      for (let index = cursor; index < end; index += 1) {
        this._processEvent(timeline[index]);
      }

      this._flushExpiries(at, true);
      cursor = end;
    }

    this._flushExpiries(Number.POSITIVE_INFINITY, true);
    return this.notifications;
  }

  _processEvent(event) {
    if (event.kind === START) {
      this._handleStart(event);
    } else {
      this._handleRecover(event);
    }
  }

  _handleStart(event) {
    const existing = this._incidents.get(event.faultId);
    if (existing !== undefined) {
      if (existing.nodeId !== event.nodeId) {
        throw new Error(
          `Fault "${event.faultId}" is already bound to node "${existing.nodeId}"; ` +
            `cannot rebind it to "${event.nodeId}"`,
        );
      }
      // A re-delivered start is a no-op.  It must not reset the aggregation
      // deadline, reopen a silent incident, or add a notification.
      return;
    }

    const incident = {
      faultId: event.faultId,
      nodeId: event.nodeId,
      // Stable per-fault number: assigned once, on the very first start, so a
      // re-delivered start or recovery cannot change it.
      faultSeq: (this._incidentSeq += 1),
      state: 'pending', // pending | notified | silent | closed
      active: true,
      firstSeenAt: event.at,
      deadline: null,
      expired: false,
      startSeq: null,
      recoveryHeldAt: null,
      recoveredAt: null,
    };

    this._incidents.set(event.faultId, incident);
    this._activeByNode.set(
      event.nodeId,
      (this._activeByNode.get(event.nodeId) ?? 0) + 1,
    );

    if (this._ancestorSet(event.nodeId).size > 0) {
      // Symptoms always serve the aggregation window, even when no ancestor
      // is currently active.
      incident.deadline = event.at + this.aggregationMs;
    } else {
      this._emitStart(incident, event.at, START_TRIGGERS.ROOT_CAUSE);
    }
  }

  _handleRecover(event) {
    const incident = this._incidents.get(event.faultId);
    if (incident === undefined) {
      // A recovery for an unknown fault is stale input.  It can never create a
      // notification.
      return;
    }
    if (incident.nodeId !== event.nodeId) {
      throw new Error(
        `Fault "${event.faultId}" belongs to node "${incident.nodeId}", ` +
          `but the recovery names node "${event.nodeId}"`,
      );
    }
    if (!incident.active) {
      // Duplicate recovery (or a start re-sent after recovery): no-op.
      return;
    }

    incident.active = false;
    incident.recoveredAt = event.at;
    this._decrementActive(incident.nodeId);

    if (incident.state === 'pending') {
      // The symptom recovered before its first notification.  It stays silent
      // forever, even if an upstream ancestor later recovers.
      incident.state = 'silent';
    } else if (incident.state === 'notified') {
      if (this._isCovered(incident.nodeId)) {
        // Keep the single paired recovery until the last suppressing ancestor
        // recovers.
        incident.recoveryHeldAt = event.at;
      } else {
        this._emitRecovery(incident, event.at);
      }
    }

    // This fault is no longer active, so it may stop suppressing downstream
    // incidents even if its own recovery notification is still held.
    this._releaseDescendants(event.nodeId, event.at);
  }

  _releaseDescendants(ancestorNode, at) {
    const affected = [];

    for (const incident of this._incidents.values()) {
      if (incident.nodeId === ancestorNode) continue;
      if (!this._ancestorSet(incident.nodeId).has(ancestorNode)) continue;

      if (
        incident.state === 'pending' &&
        incident.active &&
        incident.deadline !== null &&
        at >= incident.deadline
      ) {
        affected.push({ incident, action: 'start' });
      } else if (
        incident.state === 'notified' &&
        !incident.active &&
        incident.recoveryHeldAt !== null
      ) {
        affected.push({ incident, action: 'recover' });
      }
    }

    // Upstream nodes first, then oldest incident first.  This keeps a single
    // release sweep causally readable for the receiver.
    affected.sort((left, right) => {
      const depthDelta =
        this._depth(left.incident.nodeId) - this._depth(right.incident.nodeId);
      return depthDelta !== 0 ? depthDelta : left.incident.faultSeq - right.incident.faultSeq;
    });

    for (const { incident, action } of affected) {
      if (this._isCovered(incident.nodeId)) continue;

      if (action === 'start') {
        incident.expired = true;
        this._emitStart(
          incident,
          at,
          START_TRIGGERS.SUPPRESSION_CLEARED,
        );
      } else {
        this._emitRecovery(incident, at, { heldWhileSuppressed: true });
      }
    }
  }

  _flushExpiries(limit, inclusive) {
    // Each pass expires one incident, so a covered incident cannot starve the
    // rest of the deadline queue.
    for (;;) {
      let next = null;

      for (const incident of this._incidents.values()) {
        if (incident.state !== 'pending' || !incident.active || incident.expired) {
          continue;
        }
        if (incident.deadline === null) continue;

        const due = inclusive
          ? incident.deadline <= limit
          : incident.deadline < limit;
        if (!due) continue;

        if (
          next === null ||
          incident.deadline < next.deadline ||
          (incident.deadline === next.deadline && incident.faultSeq < next.faultSeq)
        ) {
          next = incident;
        }
      }

      if (next === null) return;

      next.expired = true;
      if (!this._isCovered(next.nodeId)) {
        this._emitStart(
          next,
          next.deadline,
          START_TRIGGERS.AGGREGATION_ELAPSED,
        );
      }
    }
  }

  _emitStart(incident, at, trigger) {
    incident.state = 'notified';
    incident.startSeq = this._seq + 1;
    this._emit(START, incident, at, { trigger });
  }

  _emitRecovery(incident, at, { heldWhileSuppressed = false } = {}) {
    incident.state = 'closed';
    incident.recoveryHeldAt = null;
    this._emit(RECOVER, incident, at, {
      trigger: 'recovery',
      pairedWithSeq: incident.startSeq,
      heldWhileSuppressed,
    });
  }

  _emit(kind, incident, at, fields) {
    this._seq += 1;
    const notification = Object.freeze({
      seq: this._seq,
      kind,
      faultId: incident.faultId,
      faultSeq: incident.faultSeq,
      nodeId: incident.nodeId,
      at,
      ...fields,
    });

    this._notifications.push(notification);
    if (this._onNotification !== null) {
      this._onNotification(notification);
    }
    return notification;
  }

  _decrementActive(nodeId) {
    const remaining = (this._activeByNode.get(nodeId) ?? 0) - 1;
    if (remaining <= 0) {
      this._activeByNode.delete(nodeId);
    } else {
      this._activeByNode.set(nodeId, remaining);
    }
  }

  _isCovered(nodeId) {
    for (const ancestor of this._ancestorSet(nodeId)) {
      if ((this._activeByNode.get(ancestor) ?? 0) > 0) return true;
    }
    return false;
  }

  _ancestorSet(nodeId) {
    return this._ancestors.get(nodeId) ?? EMPTY_SET;
  }

  _depth(nodeId) {
    return this._depths.get(nodeId) ?? 0;
  }
}

function normalizeEvent(event, index) {
  if (!isRecord(event)) {
    throw new TypeError(`events[${index}] must be an object`);
  }

  const { kind, faultId, nodeId, at } = event;

  if (kind !== START && kind !== RECOVER) {
    throw new TypeError(
      `events[${index}].kind must be "${START}" or "${RECOVER}"`,
    );
  }
  if (!isNonEmptyString(faultId)) {
    throw new TypeError(`events[${index}].faultId must be a non-empty string`);
  }
  if (!isNonEmptyString(nodeId)) {
    throw new TypeError(`events[${index}].nodeId must be a non-empty string`);
  }
  if (typeof at !== 'number' || !Number.isFinite(at)) {
    throw new TypeError(`events[${index}].at must be a finite number`);
  }

  return { kind, faultId, nodeId, at, index };
}

function assertAcyclic(nodes, parents) {
  const color = new Map(); // 1 = visiting, 2 = done
  const stack = [];

  const visit = (node) => {
    const state = color.get(node);
    if (state === 2) return;
    if (state === 1) {
      const cycleStart = stack.indexOf(node);
      const path = [...stack.slice(cycleStart), node].join(' -> ');
      throw new Error(`Suppression graph contains a cycle: ${path}`);
    }

    color.set(node, 1);
    stack.push(node);
    for (const parent of parents.get(node) ?? []) {
      visit(parent);
    }
    stack.pop();
    color.set(node, 2);
  };

  for (const node of nodes) visit(node);
}

function collectAncestors(node, parents, memo) {
  const cached = memo.get(node);
  if (cached !== undefined) return cached;

  const result = new Set();
  for (const parent of parents.get(node) ?? []) {
    result.add(parent);
    for (const ancestor of collectAncestors(parent, parents, memo)) {
      result.add(ancestor);
    }
  }

  memo.set(node, result);
  return result;
}

function computeDepth(node, parents, memo) {
  const cached = memo.get(node);
  if (cached !== undefined) return cached;

  let depth = 0;
  for (const parent of parents.get(node) ?? []) {
    depth = Math.max(depth, computeDepth(parent, parents, memo) + 1);
  }

  memo.set(node, depth);
  return depth;
}
