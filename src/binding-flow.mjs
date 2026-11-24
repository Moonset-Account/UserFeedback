import { randomUUID } from 'node:crypto';

const ID_MAX_LENGTH = 200;
const ISSUER_MAX_LENGTH = 2048;
const SUBJECT_MAX_LENGTH = 512;
const REASON_MAX_LENGTH = 500;

export const STATUS = Object.freeze({
  PENDING_IDENTITY: 'pending_identity',
  AWAITING_CONFIRMATION: 'awaiting_confirmation',
  BOUND: 'bound',
  CONFLICT: 'conflict',
  CANCELLED: 'cancelled',
});

export const RETURN_OUTCOME = Object.freeze({
  ACCEPTED: 'accepted',
  DUPLICATE: 'duplicate',
  IGNORED: 'ignored',
  DEFERRED: 'deferred',
});

export const CONFIRM_OUTCOME = Object.freeze({
  BOUND: 'bound',
  ALREADY_BOUND: 'already_bound',
  CONFLICT: 'conflict',
});

export const CANCEL_OUTCOME = Object.freeze({
  CANCELLED: 'cancelled',
  ALREADY_CANCELLED: 'already_cancelled',
});

const ALL_STATUSES = new Set(Object.values(STATUS));

export class BindingFlowError extends Error {
  constructor(code, message, details) {
    super(message);
    this.name = 'BindingFlowError';
    this.code = code;
    this.details = details === undefined ? undefined : Object.freeze({ ...details });
  }
}

function requireNonEmptyString(value, field, maxLength) {
  if (typeof value !== 'string') {
    throw new BindingFlowError('INVALID_ARGUMENT', `${field} must be a string.`, { field });
  }
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    throw new BindingFlowError('INVALID_ARGUMENT', `${field} must not be empty.`, { field });
  }
  if (trimmed.length > maxLength) {
    throw new BindingFlowError(
      'INVALID_ARGUMENT',
      `${field} must not be longer than ${maxLength} characters.`,
      { field, maxLength },
    );
  }
  return trimmed;
}

function optionalString(value, field, maxLength) {
  if (value === undefined || value === null || value === '') {
    return null;
  }
  return requireNonEmptyString(value, field, maxLength);
}

function identityKey(issuer, subject) {
  // JSON encoding keeps the two components unambiguous even if they contain
  // whitespace or separator-like characters.
  return JSON.stringify([issuer, subject]);
}

function bindingView(binding) {
  if (!binding) return null;
  return Object.freeze({
    bindingId: binding.bindingId,
    issuer: binding.issuer,
    subject: binding.subject,
    localProfileId: binding.localProfileId,
    registrationId: binding.registrationId,
    createdAt: binding.createdAt,
  });
}

function conflictView(conflict) {
  if (!conflict) return null;
  const { occupiedBy } = conflict;
  return Object.freeze({
    code: conflict.code,
    message: conflict.message,
    issuer: conflict.issuer,
    subject: conflict.subject,
    attemptedLocalProfileId: conflict.attemptedLocalProfileId,
    occupiedBy: Object.freeze({
      bindingId: occupiedBy.bindingId,
      localProfileId: occupiedBy.localProfileId,
      registrationId: occupiedBy.registrationId,
      boundAt: occupiedBy.boundAt,
    }),
    detectedAt: conflict.detectedAt,
  });
}

function identityView(identity) {
  if (!identity) return null;
  return Object.freeze({
    issuer: identity.issuer,
    subject: identity.subject,
    eventId: identity.eventId,
    receivedAt: identity.receivedAt,
  });
}

function registrationView(registration) {
  return Object.freeze({
    registrationId: registration.registrationId,
    tabId: registration.tabId,
    localProfileId: registration.localProfileId,
    issuer: registration.issuer,
    status: registration.status,
    identity: identityView(registration.identity),
    binding: bindingView(registration.binding),
    conflict: conflictView(registration.conflict),
    startedAt: registration.startedAt,
    awaitingConfirmationAt: registration.awaitingConfirmationAt,
    confirmedAt: registration.confirmedAt,
    cancelledAt: registration.cancelledAt,
    cancelledByTabId: registration.cancelledByTabId,
    cancelReason: registration.cancelReason,
  });
}

function assertObject(input) {
  if (input === null || typeof input !== 'object' || Array.isArray(input)) {
    throw new BindingFlowError('INVALID_ARGUMENT', 'A parameter object is required.');
  }
}

/**
 * Create an in-memory enterprise-identity binding flow.
 *
 * All methods are synchronous, so identity-claim checking and binding writes
 * form one atomic critical section inside this process.
 */
export function createBindingFlow(options = {}) {
  if (options === null || typeof options !== 'object') {
    throw new TypeError('options must be an object.');
  }

  const now = options.now ?? (() => Date.now());
  const generateBindingId = options.generateBindingId ?? (() => `bind_${randomUUID()}`);
  const generateRegistrationId = options.generateRegistrationId ?? (() => `reg_${randomUUID()}`);
  const maxDeferredReturns = options.maxDeferredReturns ?? 1000;

  if (typeof now !== 'function') {
    throw new TypeError('options.now must be a function.');
  }
  if (typeof generateBindingId !== 'function') {
    throw new TypeError('options.generateBindingId must be a function.');
  }
  if (typeof generateRegistrationId !== 'function') {
    throw new TypeError('options.generateRegistrationId must be a function.');
  }
  if (!Number.isInteger(maxDeferredReturns) || maxDeferredReturns < 1) {
    throw new TypeError('options.maxDeferredReturns must be a positive integer.');
  }

  /** @type {Map<string, object>} registrationId -> registration record */
  const registrations = new Map();
  /** @type {Map<string, object>} identity key -> binding record */
  const bindingByIdentity = new Map();
  /** @type {Set<string>} all issued binding ids */
  const issuedBindingIds = new Set();
  /** @type {Map<string, object>} registrationId -> deferred identity return */
  const deferredReturns = new Map();

  function newRegistrationId() {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const candidate = generateRegistrationId();
      if (typeof candidate !== 'string' || candidate.trim().length === 0) {
        throw new BindingFlowError(
          'REGISTRATION_ID_GENERATION_FAILED',
          'The registration id generator returned an invalid id.',
        );
      }
      if (!registrations.has(candidate) && !deferredReturns.has(candidate)) {
        return candidate;
      }
    }
    throw new BindingFlowError(
      'REGISTRATION_ID_GENERATION_FAILED',
      'The registration id generator repeatedly returned an id that is already in use.',
    );
  }

  function acceptIdentity(record, assertion) {
    if (assertion.eventId !== null) {
      record.events.set(assertion.eventId, identityKey(assertion.issuer, assertion.subject));
    }
    record.identity = Object.freeze({
      issuer: assertion.issuer,
      subject: assertion.subject,
      eventId: assertion.eventId,
      receivedAt: assertion.receivedAt,
    });
    record.status = STATUS.AWAITING_CONFIRMATION;
    record.awaitingConfirmationAt = now();
  }

  function getRecord(registrationId) {
    const record = registrations.get(registrationId);
    if (!record) {
      throw new BindingFlowError(
        'UNKNOWN_REGISTRATION',
        'No binding registration exists for this registration id.',
        { registrationId },
      );
    }
    return record;
  }

  function returnResult(outcome, record, extra = {}) {
    const view = record ? registrationView(record) : null;
    return Object.freeze({
      outcome,
      reason: extra.reason ?? null,
      registration: view,
      identity: view ? view.identity : (extra.identity ?? null),
    });
  }

  /**
   * Start a binding registration. Records the initiating tab, local profile,
   * enterprise identity source and registration id before the browser is sent
   * to the identity provider.
   */
  function startBinding(input) {
    assertObject(input);
    const tabId = requireNonEmptyString(input.tabId, 'tabId', ID_MAX_LENGTH);
    const localProfileId = requireNonEmptyString(input.localProfileId, 'localProfileId', ID_MAX_LENGTH);
    const issuer = requireNonEmptyString(input.issuer, 'issuer', ISSUER_MAX_LENGTH);
    const registrationId = input.registrationId === undefined || input.registrationId === null
      ? newRegistrationId()
      : requireNonEmptyString(input.registrationId, 'registrationId', ID_MAX_LENGTH);

    const existing = registrations.get(registrationId);
    if (existing) {
      if (
        existing.tabId === tabId
        && existing.localProfileId === localProfileId
        && existing.issuer === issuer
      ) {
        return Object.freeze({
          created: false,
          deferredReturnApplied: false,
          registration: registrationView(existing),
        });
      }
      throw new BindingFlowError(
        'REGISTRATION_ID_CONFLICT',
        'This registration id was already used with different registration details.',
        { registrationId },
      );
    }

    const record = {
      registrationId,
      tabId,
      localProfileId,
      issuer,
      status: STATUS.PENDING_IDENTITY,
      identity: null,
      binding: null,
      conflict: null,
      startedAt: now(),
      awaitingConfirmationAt: null,
      confirmedAt: null,
      cancelledAt: null,
      cancelledByTabId: null,
      cancelReason: null,
      events: new Map(),
    };
    registrations.set(registrationId, record);

    let deferredReturnApplied = false;
    const deferred = deferredReturns.get(registrationId);
    if (deferred) {
      deferredReturns.delete(registrationId);
      // A return can be delivered by a race before this registration becomes
      // visible. It is only usable if it came from the identity source that
      // this registration selected.
      if (deferred.issuer === issuer) {
        acceptIdentity(record, deferred);
        deferredReturnApplied = true;
      }
    }

    return Object.freeze({
      created: true,
      deferredReturnApplied,
      registration: registrationView(record),
    });
  }

  /**
   * Receive an identity-service return whose assertion has already been
   * verified by the caller. Safe against out-of-order delivery between
   * registrations and against redelivery.
   */
  function receiveIdentityReturn(input) {
    assertObject(input);
    const registrationId = requireNonEmptyString(input.registrationId, 'registrationId', ID_MAX_LENGTH);
    const issuer = requireNonEmptyString(input.issuer, 'issuer', ISSUER_MAX_LENGTH);
    const subject = requireNonEmptyString(input.subject, 'subject', SUBJECT_MAX_LENGTH);
    const eventId = optionalString(input.eventId, 'eventId', ID_MAX_LENGTH);
    const assertion = Object.freeze({ issuer, subject, eventId, receivedAt: now() });

    const record = registrations.get(registrationId);
    if (!record) {
      const pending = deferredReturns.get(registrationId);
      if (!pending) {
        if (deferredReturns.size >= maxDeferredReturns) {
          throw new BindingFlowError(
            'DEFERRED_RETURN_CAPACITY',
            'Too many identity returns are waiting for their registration to start.',
            { maxDeferredReturns },
          );
        }
        deferredReturns.set(registrationId, assertion);
        return returnResult(RETURN_OUTCOME.DEFERRED, null, { identity: identityView(assertion) });
      }
      const sameEvent = eventId !== null && pending.eventId === eventId;
      const sameIdentity = pending.issuer === issuer && pending.subject === subject;
      if (sameEvent && sameIdentity) {
        return returnResult(RETURN_OUTCOME.DUPLICATE, null, {
          identity: identityView(pending),
          reason: 'RETURN_ALREADY_DEFERRED',
        });
      }
      if (sameIdentity) {
        // A new event for the same identity is still the same pending return.
        return returnResult(RETURN_OUTCOME.DUPLICATE, null, {
          identity: identityView(pending),
          reason: 'ASSERTION_ALREADY_DEFERRED',
        });
      }
      throw new BindingFlowError(
        'IDENTITY_RETURN_CONFLICT',
        'A different identity return is already waiting for this registration id.',
        { registrationId },
      );
    }

    if (issuer !== record.issuer) {
      throw new BindingFlowError(
        'ISSUER_MISMATCH',
        'The identity return does not come from the enterprise identity source selected at start.',
        { registrationId },
      );
    }

    // Terminal states are never changed by a return, not even a redelivery.
    if (record.status === STATUS.CANCELLED) {
      return returnResult(RETURN_OUTCOME.IGNORED, record, { reason: 'REGISTRATION_CANCELLED' });
    }
    if (record.status === STATUS.CONFLICT) {
      return returnResult(RETURN_OUTCOME.IGNORED, record, { reason: 'REGISTRATION_CONFLICT' });
    }

    if (eventId !== null && record.events.has(eventId)) {
      const seenKey = record.events.get(eventId);
      if (seenKey !== identityKey(issuer, subject)) {
        // An event id is an idempotency key; it cannot be recycled for a
        // different identity.
        throw new BindingFlowError(
          'IDENTITY_RETURN_CONFLICT',
          'This event id was already used for a different identity.',
          { registrationId, eventId },
        );
      }
      return returnResult(RETURN_OUTCOME.DUPLICATE, record, { reason: 'EVENT_ALREADY_PROCESSED' });
    }

    if (record.status === STATUS.BOUND) {
      if (record.binding.issuer === issuer && record.binding.subject === subject) {
        return returnResult(RETURN_OUTCOME.DUPLICATE, record, { reason: 'REGISTRATION_BOUND' });
      }
      return returnResult(RETURN_OUTCOME.IGNORED, record, { reason: 'IDENTITY_MISMATCH' });
    }

    if (record.status === STATUS.AWAITING_CONFIRMATION) {
      if (record.identity.issuer === issuer && record.identity.subject === subject) {
        if (eventId !== null) {
          record.events.set(eventId, identityKey(issuer, subject));
        }
        return returnResult(RETURN_OUTCOME.DUPLICATE, record, { reason: 'ASSERTION_ALREADY_RECEIVED' });
      }
      throw new BindingFlowError(
        'IDENTITY_RETURN_CONFLICT',
        'A different identity subject was already received for this registration.',
        { registrationId },
      );
    }

    // PENDING_IDENTITY: this is the first usable return.
    acceptIdentity(record, assertion);
    return returnResult(RETURN_OUTCOME.ACCEPTED, record);
  }

  /**
   * Confirm the binding from the tab that initiated the registration.
   *
   * The (issuer, subject) claim is checked here, at confirmation time, so two
   * tabs racing on the same enterprise identity are serialised by
   * confirmation order: the first confirmation creates the binding, later
   * ones either reuse it (same profile) or are recorded as a conflict
   * (different profile).
   */
  function confirmBinding(input) {
    assertObject(input);
    const registrationId = requireNonEmptyString(input.registrationId, 'registrationId', ID_MAX_LENGTH);
    const tabId = requireNonEmptyString(input.tabId, 'tabId', ID_MAX_LENGTH);
    const record = getRecord(registrationId);

    if (record.tabId !== tabId) {
      throw new BindingFlowError(
        'TAB_MISMATCH',
        'Only the tab that started this registration may confirm it.',
        { registrationId },
      );
    }

    if (record.status === STATUS.CANCELLED) {
      throw new BindingFlowError(
        'REGISTRATION_CANCELLED',
        'This registration was cancelled and can no longer be confirmed.',
        {
          registrationId,
          cancelledAt: record.cancelledAt,
          cancelReason: record.cancelReason,
        },
      );
    }
    if (record.status === STATUS.PENDING_IDENTITY) {
      throw new BindingFlowError(
        'IDENTITY_NOT_READY',
        'No verified identity return has been received for this registration yet.',
        { registrationId, status: record.status },
      );
    }

    if (record.status === STATUS.BOUND) {
      return Object.freeze({
        outcome: CONFIRM_OUTCOME.ALREADY_BOUND,
        binding: bindingView(record.binding),
        conflict: null,
        registration: registrationView(record),
      });
    }

    if (record.status === STATUS.CONFLICT) {
      return Object.freeze({
        outcome: CONFIRM_OUTCOME.CONFLICT,
        binding: null,
        conflict: conflictView(record.conflict),
        registration: registrationView(record),
      });
    }

    // STATUS.AWAITING_CONFIRMATION
    const key = identityKey(record.identity.issuer, record.identity.subject);
    const existing = bindingByIdentity.get(key);

    if (existing) {
      if (existing.localProfileId === record.localProfileId) {
        record.binding = existing;
        record.status = STATUS.BOUND;
        record.confirmedAt = now();
        record.conflict = null;
        return Object.freeze({
          outcome: CONFIRM_OUTCOME.ALREADY_BOUND,
          binding: bindingView(existing),
          conflict: null,
          registration: registrationView(record),
        });
      }

      record.status = STATUS.CONFLICT;
      record.conflict = Object.freeze({
        code: 'IDENTITY_OCCUPIED',
        message: 'This enterprise identity is already linked to a different local profile.',
        issuer: record.identity.issuer,
        subject: record.identity.subject,
        attemptedLocalProfileId: record.localProfileId,
        occupiedBy: Object.freeze({
          bindingId: existing.bindingId,
          localProfileId: existing.localProfileId,
          registrationId: existing.registrationId,
          boundAt: existing.createdAt,
        }),
        detectedAt: now(),
      });
      // The losing registration deliberately keeps its own local profile.
      return Object.freeze({
        outcome: CONFIRM_OUTCOME.CONFLICT,
        binding: null,
        conflict: conflictView(record.conflict),
        registration: registrationView(record),
      });
    }

    const bindingId = generateBindingId();
    if (typeof bindingId !== 'string' || bindingId.trim().length === 0) {
      throw new BindingFlowError(
        'BINDING_ID_GENERATION_FAILED',
        'The binding id generator returned an invalid id.',
      );
    }
    if (issuedBindingIds.has(bindingId)) {
      throw new BindingFlowError(
        'BINDING_ID_COLLISION',
        'The binding id generator returned an id that is already in use.',
        { bindingId },
      );
    }

    const binding = Object.freeze({
      bindingId,
      issuer: record.identity.issuer,
      subject: record.identity.subject,
      localProfileId: record.localProfileId,
      registrationId: record.registrationId,
      createdAt: now(),
    });
    issuedBindingIds.add(bindingId);
    bindingByIdentity.set(key, binding);
    record.binding = binding;
    record.status = STATUS.BOUND;
    record.confirmedAt = binding.createdAt;
    record.conflict = null;

    return Object.freeze({
      outcome: CONFIRM_OUTCOME.BOUND,
      binding: bindingView(binding),
      conflict: null,
      registration: registrationView(record),
    });
  }

  /**
   * Cancel a registration. Cancellation is terminal and remains explicit in
   * every later view; late identity returns cannot revive the registration.
   */
  function cancelBinding(input) {
    assertObject(input);
    const registrationId = requireNonEmptyString(input.registrationId, 'registrationId', ID_MAX_LENGTH);
    const tabId = requireNonEmptyString(input.tabId, 'tabId', ID_MAX_LENGTH);
    const reason = input.reason === undefined || input.reason === null
      ? 'USER_CANCELLED'
      : requireNonEmptyString(input.reason, 'reason', REASON_MAX_LENGTH);
    const record = getRecord(registrationId);

    if (record.tabId !== tabId) {
      throw new BindingFlowError(
        'TAB_MISMATCH',
        'Only the tab that started this registration may cancel it.',
        { registrationId },
      );
    }

    if (record.status === STATUS.CANCELLED) {
      return Object.freeze({
        outcome: CANCEL_OUTCOME.ALREADY_CANCELLED,
        registration: registrationView(record),
      });
    }
    if (record.status === STATUS.BOUND) {
      throw new BindingFlowError(
        'ALREADY_BOUND',
        'A successful binding cannot be cancelled through this flow.',
        { registrationId, bindingId: record.binding.bindingId },
      );
    }

    record.status = STATUS.CANCELLED;
    record.cancelledAt = now();
    record.cancelledByTabId = tabId;
    record.cancelReason = reason;

    return Object.freeze({
      outcome: CANCEL_OUTCOME.CANCELLED,
      registration: registrationView(record),
    });
  }

  function getRegistration(registrationIdInput) {
    const registrationId = requireNonEmptyString(registrationIdInput, 'registrationId', ID_MAX_LENGTH);
    const record = registrations.get(registrationId);
    return record ? registrationView(record) : undefined;
  }

  function listRegistrations(filter = {}) {
    assertObject(filter);
    const tabId = filter.tabId === undefined
      ? null
      : requireNonEmptyString(filter.tabId, 'tabId', ID_MAX_LENGTH);
    let status = null;
    if (filter.status !== undefined) {
      status = requireNonEmptyString(filter.status, 'status', ID_MAX_LENGTH);
      if (!ALL_STATUSES.has(status)) {
        throw new BindingFlowError('INVALID_ARGUMENT', 'Unknown registration status filter.', { status });
      }
    }

    const result = [];
    for (const record of registrations.values()) {
      if (tabId !== null && record.tabId !== tabId) continue;
      if (status !== null && record.status !== status) continue;
      result.push(record);
    }
    result.sort((left, right) => {
      if (left.startedAt !== right.startedAt) return left.startedAt - right.startedAt;
      return left.registrationId < right.registrationId ? -1 : left.registrationId > right.registrationId ? 1 : 0;
    });
    return Object.freeze(result.map(registrationView));
  }

  function getBindingForIdentity(input) {
    assertObject(input);
    const issuer = requireNonEmptyString(input.issuer, 'issuer', ISSUER_MAX_LENGTH);
    const subject = requireNonEmptyString(input.subject, 'subject', SUBJECT_MAX_LENGTH);
    const binding = bindingByIdentity.get(identityKey(issuer, subject));
    return binding ? bindingView(binding) : undefined;
  }

  function listBindings(filter = {}) {
    assertObject(filter);
    const issuer = filter.issuer === undefined
      ? null
      : requireNonEmptyString(filter.issuer, 'issuer', ISSUER_MAX_LENGTH);
    const subject = filter.subject === undefined
      ? null
      : requireNonEmptyString(filter.subject, 'subject', SUBJECT_MAX_LENGTH);
    const localProfileId = filter.localProfileId === undefined
      ? null
      : requireNonEmptyString(filter.localProfileId, 'localProfileId', ID_MAX_LENGTH);

    const result = [];
    for (const binding of bindingByIdentity.values()) {
      if (issuer !== null && binding.issuer !== issuer) continue;
      if (subject !== null && binding.subject !== subject) continue;
      if (localProfileId !== null && binding.localProfileId !== localProfileId) continue;
      result.push(binding);
    }
    result.sort((left, right) => {
      if (left.createdAt !== right.createdAt) return left.createdAt - right.createdAt;
      return left.bindingId < right.bindingId ? -1 : left.bindingId > right.bindingId ? 1 : 0;
    });
    return Object.freeze(result.map(bindingView));
  }

  return Object.freeze({
    startBinding,
    receiveIdentityReturn,
    confirmBinding,
    cancelBinding,
    getRegistration,
    listRegistrations,
    getBindingForIdentity,
    listBindings,
  });
}
