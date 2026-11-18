/**
 * 验签组件产出的“可信凭据结果”。
 *
 * 登记服务只接受 TrustedCredential 的实例（而不是任意普通对象），
 * 以此在模块边界上区分“验签组件已核验的结果”和未经核验的外部输入。
 * 实例创建后即冻结，防止在服务内部流转时被篡改。
 */
export class TrustedCredential {
  constructor({
    credentialId,
    publicKey = null,
    counter = 0,
    transports = [],
    deviceLabel = null,
    verifiedAt = new Date().toISOString(),
  } = {}) {
    if (!credentialId || typeof credentialId !== 'string') {
      throw new TypeError('TrustedCredential requires a non-empty string credentialId');
    }
    this.credentialId = credentialId;
    this.publicKey = publicKey == null ? null : String(publicKey);
    this.counter = Number.isFinite(counter) ? Number(counter) : 0;
    this.transports = Object.freeze(Array.isArray(transports) ? [...transports] : []);
    this.deviceLabel = deviceLabel == null ? null : String(deviceLabel);
    this.verifiedAt = String(verifiedAt);
    Object.freeze(this);
  }
}
