/**
 * 离线握手头解析器。
 *
 * 输入为“捕获到的原始握手头字符串”（离线数据，可来自日志/抓包/夹具），
 * 输出结构化的客户端声明。本模块只做语法解析与协议级（RFC）校验，
 * 不做任何策略判定——策略判定全部在 policies/ 内完成，保证两者可独立回归。
 */

import {
  PMD,
  RFC_WINDOW_BITS_MAX,
  RFC_WINDOW_BITS_MIN,
} from './constants.js';

const KNOWN_PMD_KEYS = new Set(Object.values(PMD));
const WINDOW_KEYS = new Set([
  PMD.SERVER_MAX_WINDOW_BITS,
  PMD.CLIENT_MAX_WINDOW_BITS,
]);
const FLAG_KEYS = new Set([
  PMD.SERVER_NO_CONTEXT_TAKEOVER,
  PMD.CLIENT_NO_CONTEXT_TAKEOVER,
]);

/**
 * 按逗号切分头字段，兼容 RFC 7230 的引号字符串（引号内逗号不切分）。
 */
function splitElements(headerValue) {
  const elements = [];
  let current = '';
  let inQuotes = false;
  for (const ch of headerValue) {
    if (ch === '"') inQuotes = !inQuotes;
    if (ch === ',' && !inQuotes) {
      elements.push(current.trim());
      current = '';
    } else {
      current += ch;
    }
  }
  if (current.trim() !== '') elements.push(current.trim());
  return elements;
}

function parseParam(token) {
  const eq = token.indexOf('=');
  if (eq === -1) {
    return { key: token.trim().toLowerCase(), isFlag: true, value: null };
  }
  const key = token.slice(0, eq).trim().toLowerCase();
  let value = token.slice(eq + 1).trim();
  if (value.startsWith('"') && value.endsWith('"') && value.length >= 2) {
    value = value.slice(1, -1);
  }
  return { key, isFlag: false, value };
}

/**
 * 解析单个 extension-element，例如：
 *   permessage-deflate; client_max_window_bits=15; client_no_context_takeover
 */
function parseExtensionElement(rawElement, order) {
  const segments = rawElement.split(';').map((s) => s.trim()).filter((s) => s !== '');
  const name = (segments[0] ?? '').toLowerCase();
  const offer = {
    name,
    raw: rawElement,
    order,
    malformed: false,
    malformedReason: null,
    params: {},
    presentKeys: [],
    unknownParams: [],
  };
  if (name === '') {
    offer.malformed = true;
    offer.malformedReason = '扩展名为空';
    return offer;
  }

  const seen = new Set();
  for (const token of segments.slice(1)) {
    const { key, isFlag, value } = parseParam(token);

    if (seen.has(key)) {
      offer.malformed = true;
      offer.malformedReason = `参数 ${key} 在同一扩展声明中重复出现`;
    }
    seen.add(key);

    if (name !== 'permessage-deflate' || !KNOWN_PMD_KEYS.has(key)) {
      offer.unknownParams.push({ key, isFlag, value });
      continue;
    }

    if (FLAG_KEYS.has(key) && !isFlag) {
      offer.malformed = true;
      offer.malformedReason = `无值参数 ${key} 不允许携带赋值`;
    }

    let parsedValue = value;
    if (WINDOW_KEYS.has(key)) {
      if (isFlag) {
        // client_max_window_bits 允许以无值形式出现（RFC 7692 7.1.2：
        // 客户端用它通告“支持接收该参数”），server_max_window_bits 无值则畸形。
        if (key === PMD.SERVER_MAX_WINDOW_BITS) {
          offer.malformed = true;
          offer.malformedReason = 'server_max_window_bits 必须携带整数值';
        }
        parsedValue = null;
      } else if (!/^\d+$/.test(value)) {
        offer.malformed = true;
        offer.malformedReason = `参数 ${key} 的值 "${value}" 不是整数`;
      } else {
        parsedValue = Number(value);
        if (parsedValue < RFC_WINDOW_BITS_MIN || parsedValue > RFC_WINDOW_BITS_MAX) {
          offer.malformed = true;
          offer.malformedReason =
            `参数 ${key}=${parsedValue} 超出 RFC 7692 合法区间 ` +
            `[${RFC_WINDOW_BITS_MIN}, ${RFC_WINDOW_BITS_MAX}]`;
        }
      }
    }

    offer.params[key] = { present: true, isFlag, value: parsedValue };
    offer.presentKeys.push(key);
  }

  return offer;
}

/**
 * 解析客户端握手头。
 * @param {{secWebSocketProtocol?: string, secWebSocketExtensions?: string}} rawHeaders
 * @returns {{subprotocols: string[], extensionOffers: object[], parseErrors: string[]}}
 */
export function parseClientOffer(rawHeaders = {}) {
  const subprotocols = (rawHeaders.secWebSocketProtocol ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s !== '');

  const extHeader = rawHeaders.secWebSocketExtensions ?? '';
  const extensionOffers = extHeader === ''
    ? []
    : splitElements(extHeader).map((el, i) => parseExtensionElement(el, i));

  const parseErrors = extensionOffers
    .filter((o) => o.malformed)
    .map((o) => `扩展声明 #${o.order} (${o.name}): ${o.malformedReason}`);

  return { subprotocols, extensionOffers, parseErrors };
}
