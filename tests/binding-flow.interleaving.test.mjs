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
  return createBindingFlow({
    now: () => {
      clock += 1;
      return clock;
    },
    generateBindingId: () => `bind-${++bindingSeq}`,
  });
}

test('an event id cannot be recycled for a different identity', () => {
  const flow = makeFlow();
  flow.startBinding({
    registrationId: 'reg-1',
    tabId: 'tab-a',
    localProfileId: 'p1',
    issuer: 'idp',
  });
  flow.receiveIdentityReturn({ registrationId: 'reg-1', issuer: 'idp', subject: 's1', eventId: 'evt-1' });

  assert.throws(
    () => flow.receiveIdentityReturn({ registrationId: 'reg-1', issuer: 'idp', subject: 's2', eventId: 'evt-1' }),
    (error) => error.code === 'IDENTITY_RETURN_CONFLICT',
  );
  assert.equal(flow.getRegistration('reg-1').identity.subject, 's1');
});

// A tiny deterministic PRNG so failures are reproducible from the seed.
function mulberry32(seed) {
  let state = seed >>> 0;
  return function next() {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffle(items, random) {
  const copy = [...items];
  for (let i = copy.length - 1; i > 0; i -= 1) {
    const j = Math.floor(random() * (i + 1));
    [copy[i], copy[j]] = [copy[j], copy[i]];
  }
  return copy;
}

const ISSUERS = ['idp-a', 'idp-b'];

for (const seed of [1, 7, 42, 1234, 90210]) {
  test(`randomised interleavings preserve binding invariants (seed ${seed})`, () => {
    const random = mulberry32(seed);
    const flow = makeFlow();

    const identities = Array.from({ length: 5 }, (_, i) => ({
      issuer: ISSUERS[i % ISSUERS.length],
      subject: `subject-${i}`,
    }));
    // Profiles sometimes intentionally collide so the conflict path is exercised.
    const profiles = Array.from({ length: 9 }, (_, i) => `profile-${i % 6}`);

    const tabs = profiles.map((localProfileId, index) => {
      const identity = identities[Math.floor(random() * identities.length)];
      return {
        registrationId: `reg-${index}`,
        tabId: `tab-${index}`,
        localProfileId,
        issuer: identity.issuer,
        subject: identity.subject,
        eventId: `evt-${index}`,
      };
    });

    // Phase 1: start + return in a random order, interleaving tabs.
    const phaseOne = shuffle(
      tabs.flatMap((tab) => [
        { kind: 'start', tab },
        { kind: 'return', tab },
      ]),
      random,
    );

    for (const step of phaseOne) {
      if (step.kind === 'start') {
        const outcome = flow.startBinding({
          registrationId: step.tab.registrationId,
          tabId: step.tab.tabId,
          localProfileId: step.tab.localProfileId,
          issuer: step.tab.issuer,
        });
        assert.equal(outcome.created, true);
      } else {
        const outcome = flow.receiveIdentityReturn({
          registrationId: step.tab.registrationId,
          issuer: step.tab.issuer,
          subject: step.tab.subject,
          eventId: step.tab.eventId,
        });
        // Every return is unique per registration, so either it was accepted
        // directly or it was deferred and applied on start.
        assert.ok(
          outcome.outcome === RETURN_OUTCOME.ACCEPTED
            || outcome.outcome === RETURN_OUTCOME.DEFERRED,
          `unexpected return outcome ${outcome.outcome}`,
        );
      }
    }

    for (const tab of tabs) {
      assert.equal(flow.getRegistration(tab.registrationId).status, STATUS.AWAITING_CONFIRMATION);
    }

    // Phase 2: confirm in a random order. Exactly one binding per identity, and
    // a binding's profile is always the profile of the first winner.
    const confirmOrder = shuffle(tabs, random);
    const firstConfirmer = new Map();

    for (const tab of confirmOrder) {
      const result = flow.confirmBinding({ registrationId: tab.registrationId, tabId: tab.tabId });
      const identityKey = JSON.stringify([tab.issuer, tab.subject]);

      if (!firstConfirmer.has(identityKey)) {
        assert.equal(result.outcome, CONFIRM_OUTCOME.BOUND);
        assert.equal(result.binding.localProfileId, tab.localProfileId);
        firstConfirmer.set(identityKey, result.binding);
      } else {
        const winner = firstConfirmer.get(identityKey);
        if (winner.localProfileId === tab.localProfileId) {
          assert.equal(result.outcome, CONFIRM_OUTCOME.ALREADY_BOUND);
          assert.equal(result.binding.bindingId, winner.bindingId);
        } else {
          assert.equal(result.outcome, CONFIRM_OUTCOME.CONFLICT);
          assert.equal(result.binding, null);
          assert.equal(result.conflict.occupiedBy.bindingId, winner.bindingId);
          // The loser kept its own profile.
          assert.equal(result.conflict.attemptedLocalProfileId, tab.localProfileId);
          assert.equal(
            flow.getRegistration(tab.registrationId).localProfileId,
            tab.localProfileId,
          );
        }
      }
    }

    // Global invariant: at most one binding per (issuer, subject), and every
    // binding points at a real local profile and a real registration.
    const bindings = flow.listBindings();
    const seenIdentities = new Set();
    for (const binding of bindings) {
      const identityKey = JSON.stringify([binding.issuer, binding.subject]);
      assert.equal(seenIdentities.has(identityKey), false, 'duplicate identity binding');
      seenIdentities.add(identityKey);
      const registration = flow.getRegistration(binding.registrationId);
      assert.equal(registration.localProfileId, binding.localProfileId);
      assert.equal(registration.status, STATUS.BOUND);
    }

    // Every identity that had a winner has exactly one binding.
    assert.equal(bindings.length, firstConfirmer.size);

    // Re-confirming every bound registration yields the original binding.
    for (const tab of tabs) {
      const registration = flow.getRegistration(tab.registrationId);
      if (registration.status !== STATUS.BOUND) continue;
      const repeat = flow.confirmBinding({ registrationId: tab.registrationId, tabId: tab.tabId });
      assert.equal(repeat.outcome, CONFIRM_OUTCOME.ALREADY_BOUND);
      assert.equal(repeat.binding.bindingId, registration.binding.bindingId);
    }
  });
}

test('cancellations interleaved with confirmations never leave a half-bound registration', () => {
  const random = mulberry32(777);
  const flow = makeFlow();

  const tabs = Array.from({ length: 10 }, (_, index) => ({
    registrationId: `reg-${index}`,
    tabId: `tab-${index}`,
    localProfileId: `profile-${index}`,
    issuer: 'idp-a',
    subject: `subject-${index}`,
    eventId: `evt-${index}`,
  }));

  for (const tab of tabs) {
    flow.startBinding(tab);
    flow.receiveIdentityReturn(tab);
  }

  const actions = shuffle(tabs, random);
  const cancelled = new Set();

  for (const tab of actions) {
    if (random() < 0.4) {
      const result = flow.cancelBinding({ registrationId: tab.registrationId, tabId: tab.tabId });
      assert.equal(result.registration.status, STATUS.CANCELLED);
      cancelled.add(tab.registrationId);
      assert.throws(
        () => flow.confirmBinding({ registrationId: tab.registrationId, tabId: tab.tabId }),
        (error) => error.code === 'REGISTRATION_CANCELLED',
      );
    } else {
      const result = flow.confirmBinding({ registrationId: tab.registrationId, tabId: tab.tabId });
      assert.equal(result.outcome, CONFIRM_OUTCOME.BOUND);
    }
  }

  for (const tab of tabs) {
    const registration = flow.getRegistration(tab.registrationId);
    assert.ok(
      registration.status === STATUS.CANCELLED || registration.status === STATUS.BOUND,
      `unexpected terminal status ${registration.status}`,
    );
    if (registration.status === STATUS.BOUND) {
      assert.ok(registration.binding !== null);
      assert.equal(registration.binding.localProfileId, tab.localProfileId);
    } else {
      assert.equal(registration.binding, null);
      assert.ok(registration.cancelledAt !== null);
    }
  }
  assert.equal(flow.listBindings().length, tabs.length - cancelled.size);
});
