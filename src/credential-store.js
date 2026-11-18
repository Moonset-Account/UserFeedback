/**
 * 凭据绑定存储：
 * - credentialId 全局唯一，记录它首次绑定到的账户；
 * - 同一账户重复绑定同一 credentialId 与跨账户抢占都在这一层显式区分；
 * - 登记失败不会写入任何绑定。
 */
export class CredentialStore {
  #bindings = new Map();

  /**
   * 尝试绑定。成功返回 { bound:true, binding }；冲突返回
   * { bound:false, reason, existingAccountId }。
   */
  tryBind({ credentialId, accountId, registrationId, credential, now }) {
    const existing = this.#bindings.get(credentialId);
    if (existing) {
      const reason =
        existing.accountId === accountId
          ? 'credential_already_bound'
          : 'credential_bound_to_other_account';
      return { bound: false, reason, existingAccountId: existing.accountId, existing };
    }
    const binding = Object.freeze({
      credentialId,
      accountId,
      registrationId,
      publicKey: credential.publicKey,
      counter: credential.counter,
      transports: [...credential.transports],
      deviceLabel: credential.deviceLabel,
      verifiedAt: credential.verifiedAt,
      boundAt: now,
    });
    this.#bindings.set(credentialId, binding);
    return { bound: true, binding };
  }

  findByCredentialId(credentialId) {
    const binding = this.#bindings.get(credentialId);
    return binding ? { ...binding, transports: [...binding.transports] } : null;
  }

  listForAccount(accountId) {
    return [...this.#bindings.values()]
      .filter((b) => b.accountId === accountId)
      .sort((a, b) => a.boundAt.localeCompare(b.boundAt))
      .map((b) => ({ ...b, transports: [...b.transports] }));
  }

  get size() {
    return this.#bindings.size;
  }
}
