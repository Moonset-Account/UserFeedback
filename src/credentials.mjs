import { createHash } from 'node:crypto'

/**
 * A stable operation credential for one (run, step, phase) triple.
 *
 * The credential is derived only from durable identity, never from an attempt
 * counter, local time or randomness.  Every attempt of the same command phase
 * therefore carries the same credential, which lets the called service
 * de-duplicate retries and replays and guarantees a single effect.
 *
 * @param {string} runId
 * @param {string} stepId
 * @param {'forward'|'compensate'} phase
 * @returns {string}
 */
export function credentialFor(runId, stepId, phase) {
  const digest = createHash('sha256')
    .update(`${runId}\u0000${stepId}\u0000${phase}`)
    .digest('hex')
  return `op_${digest.slice(0, 32)}`
}
