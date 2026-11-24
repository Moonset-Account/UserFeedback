import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  CONFIRM_OUTCOME,
  RETURN_OUTCOME,
  STATUS,
  createBindingFlow,
} from '../src/binding-flow.mjs';

function makeFlow() {
  let clock = 1_000;
  let bindingSeq = 0;
  const flow = createBindingFlow({
    now: () => {
      clock += 1;
      return clock;
    },
    generateBindingId: () => `bind-${++bindingSeq}`,
  });
  return flow;
}

const IDP_A = 'https://idp.example.com/tenant-a';
const IDP_B = 'https://idp.example.com/tenant-b';

test('a return that arrives before the registration starts is held and then applied', () => {
  const flow = makeFlow();

  const early = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
    eventId: 'evt-1',
  });
  assert.equal(early.outcome, RETURN_OUTCOME.DEFERRED);

  const started = flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  assert.equal(started.created, true);
  assert.equal(started.deferredReturnApplied, true);
  assert.equal(started.registration.status, STATUS.AWAITING_CONFIRMATION);
  assert.equal(started.registration.identity.subject, 'subject-1');

  const confirmed = flow.confirmBinding({ registrationId: 'reg-1', tabId: 'tab-a' });
  assert.equal(confirmed.outcome, CONFIRM_OUTCOME.BOUND);
});

test('an early return from a different identity source is not attached to the registration', () => {
  const flow = makeFlow();

  flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_B,
    subject: 'subject-1',
  });

  const started = flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });

  assert.equal(started.created, true);
  assert.equal(started.deferredReturnApplied, false);
  assert.equal(started.registration.status, STATUS.PENDING_IDENTITY);
  assert.equal(started.registration.identity, null);

  // The correct source can still deliver the real assertion.
  const accepted = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
  });
  assert.equal(accepted.outcome, RETURN_OUTCOME.ACCEPTED);
  assert.equal(accepted.registration.status, STATUS.AWAITING_CONFIRMATION);
});

test('a deferral-then-start return is not applied again when redelivered', () => {
  const flow = makeFlow();
  flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
    eventId: 'evt-1',
  });
  const started = flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  assert.equal(started.deferredReturnApplied, true);

  const redelivered = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
    eventId: 'evt-1',
  });
  assert.equal(redelivered.outcome, RETURN_OUTCOME.DUPLICATE);
  assert.equal(redelivered.reason, 'EVENT_ALREADY_PROCESSED');
  assert.equal(flow.getRegistration('reg-1').awaitingConfirmationAt, started.registration.awaitingConfirmationAt);
});

test('a deferral slot is released when its registration starts even if the source differs', () => {
  const flow = makeFlow();
  flow.receiveIdentityReturn({ registrationId: 'reg-1', issuer: IDP_B, subject: 's' });
  assert.equal(
    flow.receiveIdentityReturn({ registrationId: 'reg-1', issuer: IDP_B, subject: 's' }).outcome,
    RETURN_OUTCOME.DUPLICATE,
  );

  // Starts with A, discards the B deferral.
  flow.startBinding({ registrationId: 'reg-1', tabId: 'tab-a', localProfileId: 'p', issuer: IDP_A });
  // The slot is free; a brand new deferral for another registration is accepted.
  const other = flow.receiveIdentityReturn({ registrationId: 'reg-2', issuer: IDP_A, subject: 's' });
  assert.equal(other.outcome, RETURN_OUTCOME.DEFERRED);
});

test('redelivered returns are deduplicated by event id and by identity', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });

  const first = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
    eventId: 'evt-1',
  });
  assert.equal(first.outcome, RETURN_OUTCOME.ACCEPTED);

  const sameEvent = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
    eventId: 'evt-1',
  });
  assert.equal(sameEvent.outcome, RETURN_OUTCOME.DUPLICATE);
  assert.equal(sameEvent.reason, 'EVENT_ALREADY_PROCESSED');

  const newEventSameIdentity = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
    eventId: 'evt-2',
  });
  assert.equal(newEventSameIdentity.outcome, RETURN_OUTCOME.DUPLICATE);
  assert.equal(newEventSameIdentity.reason, 'ASSERTION_ALREADY_RECEIVED');

  const registration = flow.getRegistration('reg-1');
  assert.equal(registration.status, STATUS.AWAITING_CONFIRMATION);
  assert.equal(registration.identity.eventId, 'evt-1');
});

test('a verified return for a different subject in the same registration is rejected', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
    eventId: 'evt-1',
  });

  assert.throws(
    () => flow.receiveIdentityReturn({
      registrationId: 'reg-1',
      issuer: IDP_A,
      subject: 'subject-2',
      eventId: 'evt-2',
    }),
    (error) => error.code === 'IDENTITY_RETURN_CONFLICT',
  );

  const registration = flow.getRegistration('reg-1');
  assert.equal(registration.identity.subject, 'subject-1');
  assert.equal(registration.status, STATUS.AWAITING_CONFIRMATION);
});

test('conflicting deferred returns for one registration are rejected', () => {
  const flow = makeFlow();
  flow.receiveIdentityReturn({ registrationId: 'reg-1', issuer: IDP_A, subject: 'subject-1' });

  const duplicate = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
  });
  assert.equal(duplicate.outcome, RETURN_OUTCOME.DUPLICATE);

  assert.throws(
    () => flow.receiveIdentityReturn({
      registrationId: 'reg-1',
      issuer: IDP_A,
      subject: 'subject-2',
    }),
    (error) => error.code === 'IDENTITY_RETURN_CONFLICT',
  );
});

test('returns arriving after a binding are duplicates when they match and ignored otherwise', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
    eventId: 'evt-1',
  });
  flow.confirmBinding({ registrationId: 'reg-1', tabId: 'tab-a' });

  const redelivered = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
    eventId: 'evt-1',
  });
  assert.equal(redelivered.outcome, RETURN_OUTCOME.DUPLICATE);
  assert.equal(redelivered.reason, 'EVENT_ALREADY_PROCESSED');

  const differentIdentity = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-9',
  });
  assert.equal(differentIdentity.outcome, RETURN_OUTCOME.IGNORED);
  assert.equal(differentIdentity.reason, 'IDENTITY_MISMATCH');
  assert.equal(flow.getRegistration('reg-1').binding.subject, 'subject-1');
});

test('out-of-order returns across two tabs land on their own registrations', () => {
  const flow = makeFlow();

  flow.startBinding({
    registrationId: 'reg-a',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.startBinding({
    registrationId: 'reg-b',
    tabId: 'tab-b',
    localProfileId: 'profile-personal',
    issuer: IDP_B,
  });

  // tab-b's return is handled first even though tab-a started first
  const forB = flow.receiveIdentityReturn({
    registrationId: 'reg-b',
    issuer: IDP_B,
    subject: 'subject-b',
    eventId: 'evt-b',
  });
  const forA = flow.receiveIdentityReturn({
    registrationId: 'reg-a',
    issuer: IDP_A,
    subject: 'subject-a',
    eventId: 'evt-a',
  });

  assert.equal(forB.registration.tabId, 'tab-b');
  assert.equal(forB.registration.localProfileId, 'profile-personal');
  assert.equal(forA.registration.tabId, 'tab-a');
  assert.equal(forA.registration.localProfileId, 'profile-work');

  const confirmedB = flow.confirmBinding({ registrationId: 'reg-b', tabId: 'tab-b' });
  const confirmedA = flow.confirmBinding({ registrationId: 'reg-a', tabId: 'tab-a' });
  assert.equal(confirmedB.binding.localProfileId, 'profile-personal');
  assert.equal(confirmedA.binding.localProfileId, 'profile-work');
  assert.equal(flow.listBindings().length, 2);
});

test('two tabs confirming the same enterprise identity: first confirmation wins', () => {
  const flow = makeFlow();

  flow.startBinding({
    registrationId: 'reg-a',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.startBinding({
    registrationId: 'reg-b',
    tabId: 'tab-b',
    localProfileId: 'profile-personal',
    issuer: IDP_A,
  });

  // Both tabs are waiting on the same identity; returns are interleaved.
  flow.receiveIdentityReturn({
    registrationId: 'reg-b',
    issuer: IDP_A,
    subject: 'shared-subject',
    eventId: 'evt-b',
  });
  flow.receiveIdentityReturn({
    registrationId: 'reg-a',
    issuer: IDP_A,
    subject: 'shared-subject',
    eventId: 'evt-a',
  });

  const winner = flow.confirmBinding({ registrationId: 'reg-a', tabId: 'tab-a' });
  assert.equal(winner.outcome, CONFIRM_OUTCOME.BOUND);
  assert.equal(winner.binding.localProfileId, 'profile-work');

  const loser = flow.confirmBinding({ registrationId: 'reg-b', tabId: 'tab-b' });
  assert.equal(loser.outcome, CONFIRM_OUTCOME.CONFLICT);
  assert.equal(loser.binding, null);
  assert.equal(loser.conflict.code, 'IDENTITY_OCCUPIED');
  assert.equal(loser.conflict.attemptedLocalProfileId, 'profile-personal');
  assert.equal(loser.conflict.occupiedBy.localProfileId, 'profile-work');
  assert.equal(loser.conflict.occupiedBy.bindingId, winner.binding.bindingId);
  assert.equal(loser.conflict.occupiedBy.registrationId, 'reg-a');

  // Only one binding exists, and it belongs to the winning profile.
  const bindings = flow.listBindings({ issuer: IDP_A, subject: 'shared-subject' });
  assert.equal(bindings.length, 1);
  assert.equal(bindings[0].localProfileId, 'profile-work');

  // The losing tab kept its own local profile, does not adopt the winner's.
  const loserRegistration = flow.getRegistration('reg-b');
  assert.equal(loserRegistration.status, STATUS.CONFLICT);
  assert.equal(loserRegistration.localProfileId, 'profile-personal');
  assert.equal(loserRegistration.tabId, 'tab-b');
  assert.equal(flow.listBindings({ localProfileId: 'profile-personal' }).length, 0);
});

test('the losing tab can retry confirmation and keeps receiving the same conflict', () => {
  const flow = makeFlow();
  for (const [reg, tab, profile] of [
    ['reg-a', 'tab-a', 'profile-work'],
    ['reg-b', 'tab-b', 'profile-personal'],
  ]) {
    flow.startBinding({ registrationId: reg, tabId: tab, localProfileId: profile, issuer: IDP_A });
    flow.receiveIdentityReturn({ registrationId: reg, issuer: IDP_A, subject: 'shared' });
  }

  flow.confirmBinding({ registrationId: 'reg-a', tabId: 'tab-a' });
  const firstLoss = flow.confirmBinding({ registrationId: 'reg-b', tabId: 'tab-b' });
  const secondLoss = flow.confirmBinding({ registrationId: 'reg-b', tabId: 'tab-b' });

  assert.equal(secondLoss.outcome, CONFIRM_OUTCOME.CONFLICT);
  assert.deepEqual(secondLoss.conflict, firstLoss.conflict);
  assert.equal(flow.listBindings().length, 1);
});

test('repeated confirmation of the winning registration returns the original binding', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-a',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.receiveIdentityReturn({ registrationId: 'reg-a', issuer: IDP_A, subject: 'subject-1' });

  const first = flow.confirmBinding({ registrationId: 'reg-a', tabId: 'tab-a' });
  const second = flow.confirmBinding({ registrationId: 'reg-a', tabId: 'tab-a' });
  const third = flow.confirmBinding({ registrationId: 'reg-a', tabId: 'tab-a' });

  assert.equal(first.outcome, CONFIRM_OUTCOME.BOUND);
  for (const repeat of [second, third]) {
    assert.equal(repeat.outcome, CONFIRM_OUTCOME.ALREADY_BOUND);
    assert.equal(repeat.binding.bindingId, first.binding.bindingId);
    assert.equal(repeat.binding.createdAt, first.binding.createdAt);
    assert.equal(repeat.registration.binding.bindingId, first.binding.bindingId);
  }
  assert.equal(flow.listBindings().length, 1);
});

test('a repeated success in a second tab for the same profile reuses the existing binding', () => {
  const flow = makeFlow();
  for (const [reg, tab] of [['reg-a', 'tab-a'], ['reg-b', 'tab-b']]) {
    flow.startBinding({
      registrationId: reg,
      tabId: tab,
      localProfileId: 'profile-work',
      issuer: IDP_A,
    });
    flow.receiveIdentityReturn({ registrationId: reg, issuer: IDP_A, subject: 'shared' });
  }

  const first = flow.confirmBinding({ registrationId: 'reg-a', tabId: 'tab-a' });
  const second = flow.confirmBinding({ registrationId: 'reg-b', tabId: 'tab-b' });

  assert.equal(second.outcome, CONFIRM_OUTCOME.ALREADY_BOUND);
  assert.equal(second.binding.bindingId, first.binding.bindingId);
  assert.equal(second.registration.status, STATUS.BOUND);
  assert.equal(flow.listBindings().length, 1);
});

test('the winning tab may be the one that confirms second', () => {
  const flow = makeFlow();
  for (const [reg, tab, profile] of [
    ['reg-a', 'tab-a', 'profile-work'],
    ['reg-b', 'tab-b', 'profile-personal'],
  ]) {
    flow.startBinding({ registrationId: reg, tabId: tab, localProfileId: profile, issuer: IDP_A });
    flow.receiveIdentityReturn({ registrationId: reg, issuer: IDP_A, subject: 'shared' });
  }

  const later = flow.confirmBinding({ registrationId: 'reg-b', tabId: 'tab-b' });
  const earlier = flow.confirmBinding({ registrationId: 'reg-a', tabId: 'tab-a' });

  assert.equal(later.outcome, CONFIRM_OUTCOME.BOUND);
  assert.equal(later.binding.localProfileId, 'profile-personal');
  assert.equal(earlier.outcome, CONFIRM_OUTCOME.CONFLICT);
  assert.equal(earlier.conflict.occupiedBy.localProfileId, 'profile-personal');
  assert.equal(flow.listBindings().length, 1);
});

test('a tab without its own identity return cannot take the shared identity from another tab', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-a',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.startBinding({
    registrationId: 'reg-b',
    tabId: 'tab-b',
    localProfileId: 'profile-personal',
    issuer: IDP_A,
  });
  flow.receiveIdentityReturn({ registrationId: 'reg-a', issuer: IDP_A, subject: 'shared' });
  flow.confirmBinding({ registrationId: 'reg-a', tabId: 'tab-a' });

  assert.throws(
    () => flow.confirmBinding({ registrationId: 'reg-b', tabId: 'tab-b' }),
    (error) => error.code === 'IDENTITY_NOT_READY',
  );
  assert.equal(flow.getRegistration('reg-b').status, STATUS.PENDING_IDENTITY);
});

test('cancellation is explicit and terminal, and late returns cannot revive it', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });

  const cancelled = flow.cancelBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    reason: 'USER_CLOSED_TAB',
  });
  assert.equal(cancelled.registration.status, STATUS.CANCELLED);
  assert.equal(cancelled.registration.cancelReason, 'USER_CLOSED_TAB');
  assert.equal(cancelled.registration.cancelledByTabId, 'tab-a');
  assert.ok(cancelled.registration.cancelledAt !== null);

  const late = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 'subject-1',
    eventId: 'evt-1',
  });
  assert.equal(late.outcome, RETURN_OUTCOME.IGNORED);
  assert.equal(late.reason, 'REGISTRATION_CANCELLED');
  assert.equal(late.registration.status, STATUS.CANCELLED);
  assert.equal(late.registration.identity, null);

  assert.throws(
    () => flow.confirmBinding({ registrationId: 'reg-1', tabId: 'tab-a' }),
    (error) => error.code === 'REGISTRATION_CANCELLED',
  );
  assert.equal(flow.listBindings().length, 0);
});

test('cancelling twice reports the existing cancellation state', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  const first = flow.cancelBinding({ registrationId: 'reg-1', tabId: 'tab-a' });
  const second = flow.cancelBinding({ registrationId: 'reg-1', tabId: 'tab-a' });

  assert.equal(second.outcome, 'already_cancelled');
  assert.equal(second.registration.cancelledAt, first.registration.cancelledAt);
  assert.equal(second.registration.cancelReason, first.registration.cancelReason);
});

test('a cancelled registration id cannot be restarted into a usable flow', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.cancelBinding({ registrationId: 'reg-1', tabId: 'tab-a' });

  const restart = flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  assert.equal(restart.created, false);
  assert.equal(restart.registration.status, STATUS.CANCELLED);

  // A fresh registration id is required for a new attempt.
  const fresh = flow.startBinding({
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  assert.equal(fresh.created, true);
  assert.equal(fresh.registration.status, STATUS.PENDING_IDENTITY);
});

test('cancelling one tab does not affect the other tab in flight', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-a',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.startBinding({
    registrationId: 'reg-b',
    tabId: 'tab-b',
    localProfileId: 'profile-personal',
    issuer: IDP_A,
  });
  flow.receiveIdentityReturn({ registrationId: 'reg-a', issuer: IDP_A, subject: 'shared' });
  flow.receiveIdentityReturn({ registrationId: 'reg-b', issuer: IDP_A, subject: 'shared' });

  flow.cancelBinding({ registrationId: 'reg-a', tabId: 'tab-a' });
  const stillWaiting = flow.getRegistration('reg-b');
  assert.equal(stillWaiting.status, STATUS.AWAITING_CONFIRMATION);

  const confirmed = flow.confirmBinding({ registrationId: 'reg-b', tabId: 'tab-b' });
  assert.equal(confirmed.outcome, CONFIRM_OUTCOME.BOUND);
  assert.equal(confirmed.binding.localProfileId, 'profile-personal');
  assert.equal(flow.getRegistration('reg-a').status, STATUS.CANCELLED);
});

test('a bound registration cannot be cancelled through this flow', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.receiveIdentityReturn({ registrationId: 'reg-1', issuer: IDP_A, subject: 'subject-1' });
  flow.confirmBinding({ registrationId: 'reg-1', tabId: 'tab-a' });

  assert.throws(
    () => flow.cancelBinding({ registrationId: 'reg-1', tabId: 'tab-a' }),
    (error) => error.code === 'ALREADY_BOUND',
  );
  assert.equal(flow.getRegistration('reg-1').status, STATUS.BOUND);
});

test('identity keys keep issuer and subject components separate', () => {
  const flow = makeFlow();
  const pairs = [
    { registrationId: 'reg-1', issuer: 'issuer-a', subject: 'b|c' },
    { registrationId: 'reg-2', issuer: 'issuer-a|b', subject: 'c' },
  ];
  for (const [index, pair] of pairs.entries()) {
    flow.startBinding({
      registrationId: pair.registrationId,
      tabId: `tab-${index}`,
      localProfileId: `profile-${index}`,
      issuer: pair.issuer,
    });
    flow.receiveIdentityReturn({
      registrationId: pair.registrationId,
      issuer: pair.issuer,
      subject: pair.subject,
    });
    flow.confirmBinding({ registrationId: pair.registrationId, tabId: `tab-${index}` });
  }

  assert.equal(flow.listBindings().length, 2);
  assert.equal(flow.getBindingForIdentity({ issuer: 'issuer-a', subject: 'b|c' }).localProfileId, 'profile-0');
  assert.equal(flow.getBindingForIdentity({ issuer: 'issuer-a|b', subject: 'c' }).localProfileId, 'profile-1');
});

test('a second identity source with the same subject string stays a separate binding', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.receiveIdentityReturn({ registrationId: 'reg-1', issuer: IDP_A, subject: 'subject-1' });
  flow.confirmBinding({ registrationId: 'reg-1', tabId: 'tab-a' });

  assert.equal(flow.getBindingForIdentity({ issuer: IDP_B, subject: 'subject-1' }), undefined);
});

test('registration listing filters by tab and status', () => {
  const flow = makeFlow();
  flow.startBinding({ registrationId: 'reg-1', tabId: 'tab-a', localProfileId: 'p1', issuer: IDP_A });
  flow.startBinding({ registrationId: 'reg-2', tabId: 'tab-b', localProfileId: 'p2', issuer: IDP_B });
  flow.cancelBinding({ registrationId: 'reg-2', tabId: 'tab-b' });

  assert.equal(flow.listRegistrations({ tabId: 'tab-a' }).length, 1);
  assert.equal(flow.listRegistrations({ status: STATUS.CANCELLED }).length, 1);
  assert.equal(flow.listRegistrations({ tabId: 'tab-a', status: STATUS.CANCELLED }).length, 0);
  assert.throws(
    () => flow.listRegistrations({ status: 'nope' }),
    (error) => error.code === 'INVALID_ARGUMENT',
  );
});

test('a duplicated binding id from the generator is refused instead of overwriting a binding', () => {
  const flow = createBindingFlow({
    now: () => 1,
    generateBindingId: () => 'always-same',
  });
  // Different identities, so both reach id generation rather than colliding on
  // the (issuer, subject) uniqueness check first.
  const cases = [
    ['reg-1', 'tab-a', 'p1', 'subject-1'],
    ['reg-2', 'tab-b', 'p2', 'subject-2'],
  ];
  for (const [reg, tab, profile, subject] of cases) {
    flow.startBinding({ registrationId: reg, tabId: tab, localProfileId: profile, issuer: IDP_A });
    flow.receiveIdentityReturn({ registrationId: reg, issuer: IDP_A, subject });
  }
  const first = flow.confirmBinding({ registrationId: 'reg-1', tabId: 'tab-a' });
  assert.equal(first.outcome, CONFIRM_OUTCOME.BOUND);
  assert.equal(first.binding.bindingId, 'always-same');

  assert.throws(
    () => flow.confirmBinding({ registrationId: 'reg-2', tabId: 'tab-b' }),
    (error) => error.code === 'BINDING_ID_COLLISION',
  );

  // reg-2 is untouched and no binding was overwritten.
  const untouched = flow.getRegistration('reg-2');
  assert.equal(untouched.status, STATUS.AWAITING_CONFIRMATION);
  assert.equal(untouched.binding, null);
  const bindings = flow.listBindings();
  assert.equal(bindings.length, 1);
  assert.equal(bindings[0].localProfileId, 'p1');
  assert.equal(flow.getBindingForIdentity({ issuer: IDP_A, subject: 'subject-2' }), undefined);
});

test('a conflicted registration is terminal and ignores later returns', () => {
  const flow = makeFlow();
  for (const [reg, tab, profile] of [
    ['reg-a', 'tab-a', 'profile-work'],
    ['reg-b', 'tab-b', 'profile-personal'],
  ]) {
    flow.startBinding({ registrationId: reg, tabId: tab, localProfileId: profile, issuer: IDP_A });
    flow.receiveIdentityReturn({ registrationId: reg, issuer: IDP_A, subject: 'shared' });
  }
  flow.confirmBinding({ registrationId: 'reg-a', tabId: 'tab-a' });
  const lost = flow.confirmBinding({ registrationId: 'reg-b', tabId: 'tab-b' });
  assert.equal(lost.outcome, CONFIRM_OUTCOME.CONFLICT);

  const late = flow.receiveIdentityReturn({
    registrationId: 'reg-b',
    issuer: IDP_A,
    subject: 'shared',
    eventId: 'evt-late',
  });
  assert.equal(late.outcome, RETURN_OUTCOME.IGNORED);
  assert.equal(late.reason, 'REGISTRATION_CONFLICT');
  assert.equal(flow.getRegistration('reg-b').status, STATUS.CONFLICT);
  assert.equal(flow.getRegistration('reg-b').conflict.code, 'IDENTITY_OCCUPIED');
  assert.equal(flow.listBindings().length, 1);
});

test('a deferred return that is consumed by a mismatched-source start is discarded', () => {
  const flow = makeFlow();
  flow.receiveIdentityReturn({ registrationId: 'reg-1', issuer: IDP_B, subject: 's1', eventId: 'evt-1' });

  const started = flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'p',
    issuer: IDP_A,
  });
  assert.equal(started.deferredReturnApplied, false);

  // The old deferred event id must not be considered already processed here.
  const accepted = flow.receiveIdentityReturn({
    registrationId: 'reg-1',
    issuer: IDP_A,
    subject: 's1',
    eventId: 'evt-1',
  });
  assert.equal(accepted.outcome, RETURN_OUTCOME.ACCEPTED);
});

test('a single flow can carry many independent tabs without cross-talk', () => {
  const flow = makeFlow();
  const tabs = Array.from({ length: 8 }, (_, index) => ({
    registrationId: `reg-${index}`,
    tabId: `tab-${index}`,
    localProfileId: `profile-${index}`,
    issuer: index % 2 === 0 ? IDP_A : IDP_B,
    subject: `subject-${index}`,
  }));

  for (const tab of tabs) {
    flow.startBinding(tab);
  }
  // Deliver in reverse order to exercise out-of-order handling.
  for (const tab of [...tabs].reverse()) {
    flow.receiveIdentityReturn(tab);
  }
  for (const tab of tabs) {
    const result = flow.confirmBinding({ registrationId: tab.registrationId, tabId: tab.tabId });
    assert.equal(result.outcome, CONFIRM_OUTCOME.BOUND);
    assert.equal(result.binding.subject, tab.subject);
    assert.equal(result.binding.localProfileId, tab.localProfileId);
  }
  assert.equal(flow.listBindings().length, 8);
});

test('receiving an identity return does not create a binding by itself', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'profile-work',
    issuer: IDP_A,
  });
  flow.receiveIdentityReturn({ registrationId: 'reg-1', issuer: IDP_A, subject: 'subject-1' });

  assert.equal(flow.listBindings().length, 0);
  assert.equal(
    flow.getBindingForIdentity({ issuer: IDP_A, subject: 'subject-1' }),
    undefined,
  );
  const registration = flow.getRegistration('reg-1');
  assert.equal(registration.status, STATUS.AWAITING_CONFIRMATION);
  assert.equal(registration.binding, null);
});

test('pending deferred returns are capped so a flood of unknown registrations is refused', () => {
  const flow = createBindingFlow({
    now: () => 1,
    generateBindingId: (() => {
      let seq = 0;
      return () => `bind-${++seq}`;
    })(),
    maxDeferredReturns: 2,
  });

  assert.equal(
    flow.receiveIdentityReturn({ registrationId: 'r1', issuer: IDP_A, subject: 's' }).outcome,
    RETURN_OUTCOME.DEFERRED,
  );
  assert.equal(
    flow.receiveIdentityReturn({ registrationId: 'r2', issuer: IDP_A, subject: 's' }).outcome,
    RETURN_OUTCOME.DEFERRED,
  );
  assert.throws(
    () => flow.receiveIdentityReturn({ registrationId: 'r3', issuer: IDP_A, subject: 's' }),
    (error) => error.code === 'DEFERRED_RETURN_CAPACITY',
  );

  // Starting a registration consumes its slot, making room again.
  flow.startBinding({ registrationId: 'r1', tabId: 'tab-a', localProfileId: 'p1', issuer: IDP_A });
  assert.equal(
    flow.receiveIdentityReturn({ registrationId: 'r3', issuer: IDP_A, subject: 's' }).outcome,
    RETURN_OUTCOME.DEFERRED,
  );
});
