'use strict';

// SCRAM-SHA-256 客户端（支持 basic 与 tls-exporter PLUS）。
// 客户端保留每次实际发出/收到的消息字节，AuthMessage 严格基于这些字节拼接，
// 并独立核验服务端签名 ServerSignature，不依赖服务端自报成功。

const {
  pbkdf2,
  hmac,
  sha256,
  xor,
  toBase64,
  fromBase64,
  timingSafeEqual,
  randomNonce,
} = require('./primitives');
const {
  MECHANISMS,
  TLS_EXPORTER,
  saslNameEscape,
  parseAttributes,
  requireAttr,
} = require('./messages');
const { ScramError, CODES } = require('./errors');

class ScramClient {
  /**
   * @param {object} opts
   * @param {string} opts.username
   * @param {string} opts.password 仅在客户端内存中用于派生，不入库
   * @param {'SCRAM-SHA-256'|'SCRAM-SHA-256-PLUS'} [opts.mechanism]
   * @param {import('./connection').TlsConnectionAdapter} [opts.connection] PLUS 必传
   * @param {string} [opts.clientNonce] 固定随机量入口（测试夹具用）
   */
  constructor({ username, password, mechanism = MECHANISMS.BASIC, connection, clientNonce }) {
    if (!username || !password) {
      throw new TypeError('username and password are required');
    }
    if (mechanism === MECHANISMS.PLUS && !connection) {
      throw new ScramError(
        CODES.CHANNEL_BINDING_UNSUPPORTED,
        'PLUS mechanism requires a TLS connection adapter',
      );
    }
    this.username = username;
    this.password = password;
    this.mechanism = mechanism;
    this.connection = connection ?? null;
    this._clientNonce = clientNonce ?? randomNonce();
    this._phase = 'init';
  }

  /** 阶段一：生成 client-first-message（原始字节）。 */
  firstMessage() {
    const gs2 = this.mechanism === MECHANISMS.PLUS ? `p=${TLS_EXPORTER},` : 'n,,';
    const bare = `n=${saslNameEscape(this.username)},r=${this._clientNonce}`;
    this._firstRaw = Buffer.from(`${gs2}${bare}`, 'utf8');
    this._firstBare = bare;
    this._phase = 'first-sent';
    return this._firstRaw;
  }

  /**
   * 阶段二：消费实际收到的 server-first-message 字节，生成 client-final-message。
   */
  finalMessage(serverFirstRaw) {
    if (this._phase !== 'first-sent') {
      throw new ScramError(CODES.INVALID_MESSAGE, 'finalMessage must follow firstMessage');
    }
    const serverFirstText = serverFirstRaw.toString('utf8');
    const attrs = parseAttributes(serverFirstText);
    const fullNonce = requireAttr(attrs, 'r');
    const salt = fromBase64(requireAttr(attrs, 's'));
    const iterations = Number(requireAttr(attrs, 'i'));
    if (!Number.isInteger(iterations) || iterations <= 0) {
      throw new ScramError(CODES.INVALID_MESSAGE, `invalid iteration count: ${iterations}`);
    }
    if (!fullNonce.startsWith(this._clientNonce)) {
      throw new ScramError(CODES.INVALID_MESSAGE, 'server nonce does not extend client nonce');
    }

    // 通道绑定输入（RFC 9266）：gs2-header 拼接 tls-exporter 导出字节。
    let gs2Header;
    if (this.mechanism === MECHANISMS.PLUS) {
      gs2Header = `p=${TLS_EXPORTER},`;
      const exporter = this.connection.exportChannelBinding(TLS_EXPORTER);
      this._channelBindingInput = Buffer.concat([
        Buffer.from(gs2Header, 'utf8'),
        exporter,
      ]);
    } else {
      gs2Header = 'n,,';
      this._channelBindingInput = Buffer.from(gs2Header, 'utf8');
    }

    const saltedPassword = pbkdf2(this.password, salt, iterations);
    const clientKey = hmac(saltedPassword, 'Client Key');
    const storedKey = sha256(clientKey);
    const withoutProof = `c=${toBase64(this._channelBindingInput)},r=${fullNonce}`;
    const authMessage = Buffer.from(
      `${this._firstBare},${serverFirstText},${withoutProof}`,
      'utf8',
    );
    const clientSignature = hmac(storedKey, authMessage);
    const proof = xor(clientKey, clientSignature);

    this._fullNonce = fullNonce;
    this._salt = salt;
    this._iterations = iterations;
    this._authMessage = authMessage;
    this._storedKey = storedKey;
    this._finalRaw = Buffer.from(`${withoutProof},p=${toBase64(proof)}`, 'utf8');
    this._phase = 'final-sent';
    return this._finalRaw;
  }

  /** 已交换消息派生的 AuthMessage（核验后只读，便于外部独立重算签名）。 */
  get authMessage() {
    return this._authMessage;
  }

  /** 客户端实际使用的通道绑定输入（PLUS 为 gs2 头 + 导出字节）。 */
  get channelBindingInput() {
    return this._channelBindingInput;
  }

  get fullNonce() {
    return this._fullNonce;
  }

  /**
   * 客户端独立核验服务端签名：
   * v == HMAC(ServerKey, AuthMessage)，其中 ServerKey 由密码重新派生。
   * 不接受服务端“成功”标志，只认密码学校验结果。
   */
  verifyServerFinal(serverFinalRaw) {
    if (this._phase !== 'final-sent') {
      throw new ScramError(CODES.INVALID_MESSAGE, 'verifyServerFinal called out of order');
    }
    const attrs = parseAttributes(serverFinalRaw.toString('utf8'));
    const verifier = fromBase64(requireAttr(attrs, 'v'));

    // 重新派生需要 salt/iterations：从已核验的 server-first 消息无法直接拿到，
    // 因此 finalMessage 时同步留存派生所需材料。
    const saltedPassword = pbkdf2(this.password, this._salt, this._iterations);
    const serverKey = hmac(saltedPassword, 'Server Key');
    const expected = hmac(serverKey, this._authMessage);
    if (!timingSafeEqual(verifier, expected)) {
      throw new ScramError(CODES.INVALID_PROOF, 'server signature verification failed');
    }
    this._phase = 'verified';
    return true;
  }
}

module.exports = { ScramClient };
