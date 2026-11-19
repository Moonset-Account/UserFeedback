'use strict';

// SCRAM-SHA-256 服务端工作节点（无状态逻辑 + 共享存储）。
//
// 阶段一 start()：核验 GS2 头/机制、加载账户、签发挑战并冻结：
//   - 账户的凭据版本（密码重置后旧挑战立即失效）
//   - 原连接标识与 tls-exporter 绑定字节（PLUS 必须在原连接上完成）
//   - 实际收到的 client-first-message 原始字节
//
// 阶段二 final()：严格按“实际交换的消息字节”重建 AuthMessage 核验客户端证明，
// 成功后原子创建至多一个登录会话，并返回客户端可独立校验的服务端签名。
// 同一最终消息在任意工作节点重送，返回同一份回执（幂等）。

const crypto = require('node:crypto');

const {
  hmac,
  sha256,
  xor,
  fromBase64,
  toBase64,
  timingSafeEqual,
  randomNonce,
} = require('./primitives');
const {
  MECHANISMS,
  TLS_EXPORTER,
  splitClientFirst,
  parseClientFirstBare,
  buildServerFirst,
  splitClientFinal,
  parseClientFinalWithoutProof,
  buildServerFinal,
} = require('./messages');
const { ScramError, CODES } = require('./errors');
const { createSessionId } = require('./storage');

class ScramServer {
  /**
   * @param {object} stores 四个共享存储（多节点共享同一实例/同一后端）
   * @param {string} [workerId] 节点标识，便于观察“哪个节点创建了会话”
   * @param {Function} [nonceGenerator] 可注入的服务端 nonce 生成器（夹具用）
   */
  constructor(stores, { workerId = 'worker', nonceGenerator = randomNonce } = {}) {
    this.workerId = workerId;
    this._nonceGenerator = nonceGenerator;
    this.accounts = stores.accounts;
    this.challenges = stores.challenges;
    this.idempotency = stores.idempotency;
    this.sessions = stores.sessions;
  }

  /**
   * 阶段一。
   * @param {Buffer|string} clientFirstRaw 实际收到的 client-first-message 字节
   * @param {import('./connection').TlsConnectionAdapter} [connection] 本次连接适配器
   * @returns {{conversationId: string, serverFirstRaw: Buffer}}
   */
  start(clientFirstRaw, connection) {
    const raw = Buffer.from(clientFirstRaw);
    const { gs2Header, bare } = splitClientFirst(raw);

    // 由 GS2 头判定机制并做硬性约束。
    let mechanism;
    if (gs2Header === `p=${TLS_EXPORTER},`) {
      mechanism = MECHANISMS.PLUS;
      if (!connection) {
        throw new ScramError(
          CODES.CHANNEL_BINDING_REQUIRED,
          'PLUS exchange requires a TLS connection adapter',
        );
      }
      // 连接必须能提供本次连接的绑定字节。
      connection.exportChannelBinding(TLS_EXPORTER);
    } else if (gs2Header === 'n,,') {
      mechanism = MECHANISMS.BASIC;
    } else {
      throw new ScramError(
        CODES.CHANNEL_BINDING_UNSUPPORTED,
        `unsupported GS2 header: ${gs2Header}`,
      );
    }

    const { username, nonce: clientNonce } = parseClientFirstBare(bare);
    const account = this.accounts.get(username);

    const serverNonce = this._nonceGenerator();
    const fullNonce = `${clientNonce}${serverNonce}`;

    const serverFirstRaw = buildServerFirst({
      nonce: fullNonce,
      salt: account.salt,
      iterations: account.iterations,
    });

    // 冻结挑战上下文：后续核验只信任这里留存的字节与连接信息。
    this.challenges.put({
      nonce: fullNonce,
      username,
      mechanism,
      clientFirstRaw: Buffer.from(raw),
      clientFirstBare: bare,
      serverFirstRaw: Buffer.from(serverFirstRaw),
      credentialVersion: account.credentialVersion,
      connectionId: connection ? connection.connectionId : null,
      bindingBytes: connection ? connection.exportChannelBinding(TLS_EXPORTER) : null,
      completed: false,
      createdAt: Date.now(),
    });

    return { conversationId: fullNonce, serverFirstRaw };
  }

  /**
   * 阶段二。
   * @param {Buffer|string} clientFinalRaw 实际收到的 client-final-message 字节
   * @param {import('./connection').TlsConnectionAdapter} [connection] 本次连接适配器
   * @returns {Promise<object>} 登录回执（含 serverFinalRaw，客户端可独立校验）
   */
  async final(clientFinalRaw, connection) {
    const raw = Buffer.from(clientFinalRaw);

    // 幂等键 = 最终消息字节的摘要。任何工作节点对同一字节流得到同一键。
    const idempotencyKey = crypto.createHash('sha256').update(raw).digest('hex');
    const existing = this.idempotency.get(idempotencyKey);
    if (existing) {
      return { ...existing, replayed: true, handledBy: this.workerId };
    }

    const { channelBindingFinal, withoutProof } = splitClientFinal(raw);
    const { nonce, proof: proofB64 } = parseClientFinalWithoutProof(withoutProof);
    // RFC 5802 AuthMessage 第三段是完整的 client-final-message-without-proof，
    // 即从原始字节中去掉结尾 ",p=<proof>"，必须保留通道绑定属性 c=。
    const rawText = raw.toString('utf8');
    const proofSuffix = `,p=${proofB64}`;
    if (!rawText.endsWith(proofSuffix)) {
      throw new ScramError(CODES.INVALID_MESSAGE, 'client-final proof must be the final attribute');
    }
    const finalWithoutProof = rawText.slice(0, rawText.length - proofSuffix.length);
    const challenge = this.challenges.get(nonce);

    if (challenge.completed) {
      throw new ScramError(
        CODES.CHALLENGE_COMPLETED,
        'challenge already consumed by a different final message',
      );
    }

    // PLUS 必须在原连接上完成，且绑定字节必须与阶段一冻结值逐字节一致。
    if (challenge.mechanism === MECHANISMS.PLUS) {
      if (!connection) {
        throw new ScramError(
          CODES.CHANNEL_BINDING_REQUIRED,
          'PLUS final requires the original TLS connection',
        );
      }
      if (connection.connectionId !== challenge.connectionId) {
        throw new ScramError(
          CODES.CONNECTION_MISMATCH,
          'final message submitted on a different connection than the challenge',
        );
      }
      const currentBinding = connection.exportChannelBinding(TLS_EXPORTER);
      if (!timingSafeEqual(currentBinding, challenge.bindingBytes)) {
        throw new ScramError(
          CODES.CHANNEL_BINDING_MISMATCH,
          'tls-exporter binding bytes do not match the original connection',
        );
      }
    }

    // 密码重置先于最终提交：账户凭据版本已推进，旧挑战作废。
    const account = this.accounts.get(challenge.username);
    if (account.credentialVersion !== challenge.credentialVersion) {
      throw new ScramError(
        CODES.STALE_CREDENTIAL_VERSION,
        'challenge invalidated by a password reset before final submission',
      );
    }

    // 按实际交换的消息字节重建通道绑定输入与 AuthMessage。
    let expectedChannelBinding;
    if (challenge.mechanism === MECHANISMS.PLUS) {
      expectedChannelBinding = Buffer.concat([
        Buffer.from(`p=${TLS_EXPORTER},`, 'utf8'),
        challenge.bindingBytes,
      ]);
    } else {
      expectedChannelBinding = Buffer.from('n,,', 'utf8');
    }
    if (channelBindingFinal !== `c=${toBase64(expectedChannelBinding)}`) {
      throw new ScramError(
        CODES.CHANNEL_BINDING_MISMATCH,
        'client-final channel-binding does not match the original connection data',
      );
    }

    const authMessage = Buffer.from(
      `${challenge.clientFirstBare},${challenge.serverFirstRaw.toString('utf8')},${finalWithoutProof}`,
      'utf8',
    );

    // 核验客户端证明：ClientKey = Proof XOR HMAC(StoredKey, AuthMessage)，
    // 再以 SHA-256(ClientKey) 与账户 StoredKey 恒定时间比对。
    const proof = fromBase64(proofB64);
    const clientSignature = hmac(account.storedKey, authMessage);
    const recoveredClientKey = xor(proof, clientSignature);
    if (!timingSafeEqual(sha256(recoveredClientKey), account.storedKey)) {
      throw new ScramError(CODES.INVALID_PROOF, 'client proof verification failed');
    }

    // 服务端签名：客户端可凭 HMAC(ServerKey, AuthMessage) 独立校验。
    const serverSignature = hmac(account.serverKey, authMessage);
    const serverFinalRaw = buildServerFinal(serverSignature);

    const session = {
      sessionId: createSessionId(),
      username: challenge.username,
      mechanism: challenge.mechanism,
      credentialVersion: account.credentialVersion,
      connectionId: challenge.connectionId,
      createdAt: new Date().toISOString(),
    };

    // 原子“至多一次”：并发双节点提交同一最终消息，仅一个节点插入会话成功。
    const receipt = {
      ok: true,
      replayed: false,
      handledBy: this.workerId,
      sessionId: session.sessionId,
      username: session.username,
      mechanism: session.mechanism,
      connectionId: session.connectionId,
      conversationId: nonce,
      serverFinalRaw,
      idempotencyKey,
    };
    const result = this.idempotency.putIfAbsent(idempotencyKey, receipt);
    if (!result.inserted) {
      // 竞态中落败的节点：返回先到节点创建的同一份回执，绝不新建会话。
      return { ...result.receipt, replayed: true, handledBy: this.workerId };
    }
    const created = this.sessions.insert(session);
    if (!created) {
      // 理论上不可达（新 UUID），保留防御性处理。
      throw new ScramError(CODES.INVALID_PROOF, 'session creation conflict');
    }
    this.challenges.complete(nonce);
    return receipt;
  }
}

module.exports = { ScramServer };
