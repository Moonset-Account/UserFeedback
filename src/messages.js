'use strict';

// SCRAM 消息的编解码工具。所有“按实际交换字节核验”的逻辑都直接消费
// 这里返回的原始字节，禁止在核验路径上重新拼装历史消息。

const { ScramError, CODES } = require('./errors');

const MECHANISMS = Object.freeze({
  BASIC: 'SCRAM-SHA-256',
  PLUS: 'SCRAM-SHA-256-PLUS',
});

// RFC 9266: tls-exporter 通道绑定名（不包含 "p=tls-exporter" 之外的前缀）。
const TLS_EXPORTER = 'tls-exporter';

/** RFC 5802 §5.1 SASLprep 用户名转义。 */
function saslNameEscape(name) {
  return name.replace(/=/g, '=3D').replace(/,/g, '=2C');
}

/** RFC 5802 §5.1 SASLprep 用户名反转义。 */
function saslNameUnescape(name) {
  return name.replace(/=3D/g, '=').replace(/=2C/g, ',');
}

/**
 * 解析 "k1=v1,k2=v2,..." 属性串为 Map。
 * 键必须为单字符；值中允许出现 '='（如 base64）。
 */
function parseAttributes(text) {
  const attrs = new Map();
  for (const pair of text.split(',')) {
    if (pair.length < 3 || pair[1] !== '=') {
      throw new ScramError(CODES.INVALID_MESSAGE, `malformed attribute pair: ${pair}`);
    }
    const key = pair[0];
    if (attrs.has(key)) {
      throw new ScramError(CODES.INVALID_MESSAGE, `duplicate attribute: ${key}`);
    }
    attrs.set(key, pair.slice(2));
  }
  return attrs;
}

function requireAttr(attrs, key) {
  if (!attrs.has(key)) {
    throw new ScramError(CODES.INVALID_MESSAGE, `missing required attribute: ${key}`);
  }
  return attrs.get(key);
}

/**
 * 拆分 client-first-message：
 *   gs2-header "," client-first-message-bare
 * PLUS 的 GS2 头为单字段 "p=tls-exporter,"（第一个逗号即结束）；
 * 非 PLUS 的 GS2 头为 "n,,"（到第二个逗号结束）。
 */
function splitClientFirst(raw) {
  const text = raw.toString('utf8');
  const firstComma = text.indexOf(',');
  if (firstComma === -1) {
    throw new ScramError(CODES.INVALID_MESSAGE, 'malformed client-first-message');
  }
  const firstField = text.slice(0, firstComma);
  let headerEnd;
  if (firstField.startsWith('p=')) {
    headerEnd = firstComma + 1;
  } else if (firstField === 'n' || firstField === 'y') {
    const secondComma = text.indexOf(',', firstComma + 1);
    if (secondComma === -1) {
      throw new ScramError(CODES.INVALID_MESSAGE, 'malformed client-first-message');
    }
    headerEnd = secondComma + 1;
  } else {
    throw new ScramError(
      CODES.INVALID_MESSAGE,
      `unsupported GS2 header flag: ${firstField}`,
    );
  }
  return {
    gs2Header: text.slice(0, headerEnd),
    bare: text.slice(headerEnd),
  };
}

function parseClientFirstBare(bare) {
  const attrs = parseAttributes(bare);
  return {
    username: saslNameUnescape(requireAttr(attrs, 'n')),
    nonce: requireAttr(attrs, 'r'),
  };
}

/** 构造 server-first-message 字节（按实际发送内容留存）。 */
function buildServerFirst({ nonce, salt, iterations }) {
  const { toBase64 } = require('./primitives');
  return Buffer.from(
    `r=${nonce},s=${toBase64(salt)},i=${iterations}`,
    'utf8',
  );
}

/**
 * 拆分 client-final-message：
 *   channel-binding-final "," client-final-message-without-proof
 * without-proof 从第一个逗号开始。
 */
function splitClientFinal(raw) {
  const text = raw.toString('utf8');
  const firstComma = text.indexOf(',');
  if (firstComma === -1) {
    throw new ScramError(CODES.INVALID_MESSAGE, 'malformed client-final-message');
  }
  return {
    channelBindingFinal: text.slice(0, firstComma),
    withoutProof: text.slice(firstComma + 1),
  };
}

function parseClientFinalWithoutProof(withoutProof) {
  const attrs = parseAttributes(withoutProof);
  return {
    nonce: requireAttr(attrs, 'r'),
    proof: requireAttr(attrs, 'p'),
  };
}

/** 构造 server-final-message 字节。 */
function buildServerFinal(verifier) {
  const { toBase64 } = require('./primitives');
  return Buffer.from(`v=${toBase64(verifier)}`, 'utf8');
}

module.exports = {
  MECHANISMS,
  TLS_EXPORTER,
  saslNameEscape,
  saslNameUnescape,
  parseAttributes,
  requireAttr,
  splitClientFirst,
  parseClientFirstBare,
  buildServerFirst,
  splitClientFinal,
  parseClientFinalWithoutProof,
  buildServerFinal,
};
