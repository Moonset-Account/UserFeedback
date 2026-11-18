import { TrustedCredential } from './trusted-credential.js';

/**
 * 验签组件接口（本地合成实现）。
 *
 * 真实环境中该组件负责校验 attestation/客户端数据与一次性挑战；本模块只消费其
 * “已验签”的产物。这里提供可编排的本地实现，用于在测试中模拟：
 *   - 验签成功 -> 返回 TrustedCredential；
 *   - 验签失败 -> 返回 { ok:false, reason }，由登记服务落为 rejected 终结态；
 *   - 直接抛出普通对象 -> 登记服务必须拒绝（只认真正的 TrustedCredential 实例）。
 */
export class LocalVerifier {
  #results;

  /**
   * @param {Map<string, TrustedCredential | { ok:false, reason:string }>} challengeResults
   *        key 为挑战值，value 为预置的验签结果。
   */
  constructor(challengeResults = new Map()) {
    this.#results = challengeResults;
  }

  /**
   * 模拟验签组件回调。返回 TrustedCredential 表示可信结果；
   * 返回普通对象或失败结构表示不可信，登记服务应拒绝完成。
   */
  verify({ challenge, credentialId, publicKey = null, counter = 0, transports = [], deviceLabel = null }) {
    const preset = this.#results.get(challenge);
    if (preset === undefined) {
      return { ok: false, reason: 'unknown_challenge_for_verifier' };
    }
    if (preset instanceof TrustedCredential) {
      return preset;
    }
    if (preset && preset.ok === false) {
      return { ok: false, reason: preset.reason ?? 'verification_failed' };
    }
    return new TrustedCredential({
      credentialId: credentialId ?? preset.credentialId,
      publicKey: publicKey ?? preset.publicKey ?? null,
      counter: counter ?? preset.counter ?? 0,
      transports: transports.length ? transports : preset.transports ?? [],
      deviceLabel: deviceLabel ?? preset.deviceLabel ?? null,
    });
  }
}

/**
 * 便捷工厂：让某个挑战验签成功并产出可信凭据。
 */
export function makeVerifiedCredential(payload) {
  return new TrustedCredential(payload);
}
