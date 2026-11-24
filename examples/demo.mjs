import { createBindingFlow } from '../src/binding-flow.mjs';

const flow = createBindingFlow();

// Two tabs start bindings to different local profiles, both choosing the same
// enterprise identity source.
flow.startBinding({
  registrationId: 'reg-tab-a',
  tabId: 'tab-a',
  localProfileId: 'profile-work',
  issuer: 'https://idp.example.com/tenant-a',
});
flow.startBinding({
  registrationId: 'reg-tab-b',
  tabId: 'tab-b',
  localProfileId: 'profile-personal',
  issuer: 'https://idp.example.com/tenant-a',
});

// The identity service returns the same verified subject to both, out of order.
for (const registrationId of ['reg-tab-b', 'reg-tab-a']) {
  const result = flow.receiveIdentityReturn({
    registrationId,
    issuer: 'https://idp.example.com/tenant-a',
    subject: 'employee-123',
    eventId: `evt-${registrationId}`,
  });
  console.log(registrationId, '->', result.outcome, result.registration.status);
}

// tab-a confirms first and wins the identity.
const winner = flow.confirmBinding({ registrationId: 'reg-tab-a', tabId: 'tab-a' });
console.log('tab-a confirm:', winner.outcome, winner.binding);

// Re-confirming the winner returns the very same binding.
const repeat = flow.confirmBinding({ registrationId: 'reg-tab-a', tabId: 'tab-a' });
console.log('tab-a repeat:', repeat.outcome, repeat.binding.bindingId === winner.binding.bindingId);

// tab-b loses: it keeps its own profile and learns who occupies the identity.
const loser = flow.confirmBinding({ registrationId: 'reg-tab-b', tabId: 'tab-b' });
console.log('tab-b confirm:', loser.outcome, loser.conflict.occupiedBy);
console.log('tab-b profile kept:', flow.getRegistration('reg-tab-b').localProfileId);

// A cancelled registration has an explicit, terminal state.
flow.startBinding({
  registrationId: 'reg-tab-c',
  tabId: 'tab-c',
  localProfileId: 'profile-side',
  issuer: 'https://idp.example.com/tenant-a',
});
const cancelled = flow.cancelBinding({
  registrationId: 'reg-tab-c',
  tabId: 'tab-c',
  reason: 'USER_CLOSED_TAB',
});
console.log('tab-c cancel:', cancelled.registration.status, cancelled.registration.cancelReason);
