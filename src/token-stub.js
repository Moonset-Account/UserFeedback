import { Reason } from './reasons.js';

/**
 * 可信测试桩：模拟生产侧的令牌签发与核验服务。
 *
 * 职责边界：
 *  - 桩负责密码学层面的“真伪核验”（这里用 HMAC 风格的完整性标签代替真实签名），
 *    并在核验通过后回传令牌绑定的属性 { sessionId, action, generation, exp }。
 *  - 桩 *不* 负责会话是否仍有效、动作是否匹配当前请求、代际是否过期等业务判断——
 *    这些正是 FormValidator 要回归的安全逻辑。
 *
 * 令牌是不透明字符串，测试代码不允许绕过 verify() 自行解读。
 */
export class TrustedTokenStub {
  #secret;
  #clock;
  #issued; // tokenId -> 属性记录，保证 verify 只认本桩签发过的令牌

  /**
   * @param {import('./clock.js').Clock} clock
   * @param {string} [secret] 桩内密钥（测试固定值即可，不参与安全边界）
   */
  constructor(clock, secret = 'test-stub-secret') {
    this.#clock = clock;
    this.#secret = secret;
    this.#issued = new Map();
  }

  #tag(payloadB64) {
    // 轻量完整性标签：FNV 风格哈希 + 密钥混合。仅用于测试桩防篡改演示，非真实加密。
    let h = 0x811c9dc5;
    const material = `${this.#secret}:${payloadB64}`;
    for (let i = 0; i < material.length; i += 1) {
      h ^= material.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    return (h >>> 0).toString(16).padStart(8, '0');
  }

  /**
   * 签发一个绑定具体会话/动作/代际/有效期的不透明令牌。
   * @returns {{ token: string, attributes: object }}
   */
  issue({ sessionId, action, generation, ttlMs }) {
    if (!sessionId || !action || !Number.isInteger(generation) || generation < 1) {
      throw new TypeError('issue 参数非法：sessionId/action/generation 必填且 generation >= 1');
    }
    if (!Number.isFinite(ttlMs) || ttlMs <= 0) {
      throw new TypeError('issue 参数非法：ttlMs 必须为正数');
    }
    const iat = this.#clock.now();
    const attributes = { sessionId, action, generation, iat, exp: iat + ttlMs };
    const payloadB64 = Buffer.from(JSON.stringify(attributes), 'utf8').toString('base64url');
    const tokenId = `${payloadB64}.${this.#tag(payloadB64)}`;
    this.#issued.set(tokenId, { ...attributes });
    return { token: tokenId, attributes: { ...attributes } };
  }

  /**
   * 核验令牌：只回答“这是不是本桩签发且未被篡改的令牌”。
   * 不判断业务状态（会话轮换、动作绑定、代际宽限均由校验器处理）。
   * @returns {{ valid: true, attributes: object } | { valid: false, reason: string }}
   */
  verify(token) {
    if (typeof token !== 'string' || !token.includes('.')) {
      return { valid: false, reason: Reason.TOKEN_INVALID };
    }
    const dot = token.lastIndexOf('.');
    const payloadB64 = token.slice(0, dot);
    const tag = token.slice(dot + 1);
    const known = this.#issued.get(token);
    if (!known || this.#tag(payloadB64) !== tag) {
      return { valid: false, reason: Reason.TOKEN_INVALID };
    }
    return { valid: true, attributes: { ...known } };
  }

  /**
   * 构造一个内容被篡改的令牌（用于负向测试）。
   * 篡改方式：翻转 payload 后重算标签会被“未签发记录”拦截；
   * 这里保留真实签发记录但破坏标签，模拟传输/存储层被改写。
   */
  tamper(validToken) {
    const dot = validToken.lastIndexOf('.');
    const badTag = 'deadbeef';
    return `${validToken.slice(0, dot)}.${badTag}`;
  }
}
