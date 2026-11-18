import { LogicalClock } from './clock.mjs';
import {
  MeasurementRegistry,
  LeaseRegistry,
  IdentityRegistry,
  RevocationRegistry,
  ApprovalStore,
  DecisionLog
} from './stores.mjs';
import { Adjudicator } from './adjudicator.mjs';

export { REASONS, reason } from './reasons.mjs';
export { LogicalClock } from './clock.mjs';
export {
  MeasurementRegistry,
  LeaseRegistry,
  IdentityRegistry,
  RevocationRegistry,
  ApprovalStore,
  DecisionLog
} from './stores.mjs';
export { Adjudicator } from './adjudicator.mjs';

/**
 * 构造一套完整的本地裁决环境（各登记处共享同一逻辑时钟）。
 * 传入 clock 可实现测试间的确定性重放。
 */
export function createAdjudicationWorld({ clock } = {}) {
  const sharedClock = clock ?? new LogicalClock();
  const measurements = new MeasurementRegistry(sharedClock);
  const leases = new LeaseRegistry(sharedClock);
  const identities = new IdentityRegistry(sharedClock);
  const revocations = new RevocationRegistry(sharedClock);
  const approvals = new ApprovalStore(sharedClock);
  const decisions = new DecisionLog(sharedClock);

  const adjudicator = new Adjudicator({
    clock: sharedClock,
    measurements,
    leases,
    identities,
    revocations,
    approvals,
    decisions
  });

  return {
    clock: sharedClock,
    measurements,
    leases,
    identities,
    revocations,
    approvals,
    decisions,
    adjudicator
  };
}
