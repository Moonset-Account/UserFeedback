// CORS 预检结果缓存（参照 Fetch 标准 "CORS-preflight cache" 建模）。
//
// 设计要点（对应题面约束）：
// 1. 每项许可按 (顶层站点分区, 请求源, 目标 URL) 三元组隔离；
// 2. 方法与每个请求头分别登记独立的过期时刻，命中不会刷新这些时刻；
// 3. 一次实际请求只有在方法与全部待检查头同时仍被有效许可覆盖时才能跳过预检；
// 4. 无凭据（通配）许可不能覆盖带凭据请求；
// 5. Authorization 头始终需要显式、未过期的许可，通配头 "*" 不能覆盖它；
// 6. 请求头名称按字节大小写等价处理（统一折叠为小写）；
// 7. check() 用结构化结果指出缺的是哪一项许可（方法 / 某个头 / 凭据不匹配）。

// Fetch 标准中 Access-Control-Max-Age 缺省值为 5 秒。
export const DEFAULT_MAX_AGE_SECONDS = 5;
// 通配符号，原样登记；匹配时再按凭据模式与 Authorization 规则解释。
export const WILDCARD = '*';

/**
 * 把任意时钟参数归一化为返回毫秒时间戳的函数。
 * 不传时使用系统时钟；传入数字时冻结为固定时钟（便于构造边界用例）。
 */
function normalizeClock(clock) {
  if (clock === undefined || clock === null) {
    return () => Date.now();
  }
  if (typeof clock === 'number') {
    const frozen = clock;
    return () => frozen;
  }
  if (typeof clock === 'function') {
    return clock;
  }
  if (typeof clock.now === 'function') {
    return () => clock.now();
  }
  throw new TypeError('clock 必须是函数、含 now() 的对象、数字或 undefined');
}

/** 分区键：允许字符串或任意可 JSON 化对象（如 { topLevelSite }）。 */
function normalizePartition(partition) {
  if (partition === undefined || partition === null || partition === '') {
    throw new TypeError('partition（顶层站点分区）不能为空');
  }
  if (typeof partition === 'string') return partition;
  return JSON.stringify(partition);
}

/** 源：解析为序列化后的 origin（scheme://host:port），非法即抛错。 */
function normalizeOrigin(origin) {
  if (typeof origin !== 'string' || origin.trim() === '') {
    throw new TypeError('origin 必须是非空字符串');
  }
  try {
    return new URL(origin).origin;
  } catch {
    throw new TypeError(`非法 origin: ${origin}`);
  }
}

/**
 * 目标 URL：比较时忽略片段（fragment）。
 * 返回 origin + pathname + search 三段，与标准 "same URL" 语义一致。
 */
function normalizeUrl(url) {
  if (typeof url !== 'string' || url.trim() === '') {
    throw new TypeError('url 必须是非空字符串');
  }
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new TypeError(`非法 url: ${url}`);
  }
  return {
    origin: parsed.origin,
    pathAndQuery: `${parsed.pathname}${parsed.search}`,
    href: `${parsed.origin}${parsed.pathname}${parsed.search}`,
  };
}

/** 请求头名称大小写等价：折叠为小写并去空白；空名非法。 */
function normalizeHeaderName(name) {
  if (typeof name !== 'string') {
    throw new TypeError(`请求头名称必须是字符串，收到: ${String(name)}`);
  }
  const lower = name.trim().toLowerCase();
  if (lower === '') {
    throw new TypeError('请求头名称不能为空');
  }
  return lower;
}

/** 方法按 Fetch 标准区分大小写（标准方法本身就是大写）。 */
function normalizeMethod(method) {
  if (typeof method !== 'string' || method.trim() === '') {
    throw new TypeError('method 必须是非空字符串');
  }
  return method;
}

function normalizeNameList(list, kind) {
  if (list === undefined || list === null) return [];
  if (!Array.isArray(list)) {
    throw new TypeError(`${kind} 必须是数组`);
  }
  const seen = new Set();
  const out = [];
  for (const item of list) {
    const value = kind === 'headers'
      ? normalizeHeaderName(item)
      : normalizeMethod(item);
    if (!seen.has(value)) {
      seen.add(value);
      out.push(value);
    }
  }
  return out;
}

function asFiniteNumber(value, label) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new TypeError(`${label} 必须是非负有限数字（秒）`);
  }
  return value;
}

/**
 * CORS 预检缓存。
 *
 * 存储结构：partition -> origin -> urlKey -> entry
 * entry = { credentials, methodExpires: Map, headerExpires: Map }
 */
export class PreflightCache {
  /**
   * @param {() => number|number|{now:() => number}} [clock] 可注入时钟
   */
  constructor(clock) {
    this._now = normalizeClock(clock);
    /** @type {Map<string, Map<string, Map<string, object>>>} */
    this._partitions = new Map();
  }

  /**
   * 登记一次预检响应许可。
   *
   * @param {object} options
   * @param {string|object} options.partition 顶层站点分区
   * @param {string} options.origin 请求源（预检的 Access-Control-Request-* 所属源）
   * @param {string} options.url 目标 URL（忽略 fragment）
   * @param {boolean} [options.credentials=false] 该许可是否来自带凭据请求
   * @param {number} [options.maxAge=5] Access-Control-Max-Age 秒数
   * @param {string[]} [options.allowMethods=[]] 显式许可的方法（'*' 为通配）
   * @param {string[]} [options.allowHeaders=[]] 显式许可的头名（'*' 为通配）
   * @returns {object} 写入后的条目快照
   */
  store({
    partition,
    origin,
    url,
    credentials = false,
    maxAge = DEFAULT_MAX_AGE_SECONDS,
    allowMethods = [],
    allowHeaders = [],
  }) {
    const partitionKey = normalizePartition(partition);
    const originKey = normalizeOrigin(origin);
    const urlKey = normalizeUrl(url);
    const methods = normalizeNameList(allowMethods, 'methods');
    const headers = normalizeNameList(allowHeaders, 'headers');
    const ttlSeconds = asFiniteNumber(maxAge, 'maxAge');
    if (typeof credentials !== 'boolean') {
      throw new TypeError('credentials 必须是布尔值');
    }

    const now = this._now();
    // maxAge=0 => 写入即过期（过期时刻等于当前时刻，匹配要求严格大于当前时刻）。
    const expiresAt = now + ttlSeconds * 1000;

    let origins = this._partitions.get(partitionKey);
    if (!origins) {
      origins = new Map();
      this._partitions.set(partitionKey, origins);
    }
    let urls = origins.get(originKey);
    if (!urls) {
      urls = new Map();
      origins.set(originKey, urls);
    }

    // 同一 (分区, 源, URL) 重复预检：更新对应字段的过期时刻。
    // 参照 Fetch 标准，credentials 标志以最近一次预检为准（整体覆盖）。
    const existing = urls.get(urlKey.href);
    const entry = existing ?? {
      credentials,
      methodExpires: new Map(),
      headerExpires: new Map(),
    };
    entry.credentials = credentials;
    for (const method of methods) {
      entry.methodExpires.set(method, expiresAt);
    }
    for (const header of headers) {
      entry.headerExpires.set(header, expiresAt);
    }
    urls.set(urlKey.href, entry);

    return this._snapshot(partitionKey, originKey, urlKey, entry);
  }

  /**
   * 判断一次实际请求能否跳过预检。
   *
   * @param {object} options
   * @param {string|object} options.partition 顶层站点分区
   * @param {string} options.origin 请求源
   * @param {string} options.url 目标 URL（忽略 fragment）
   * @param {boolean} [options.credentials=false] 实际请求是否带凭据
   * @param {string} options.method 实际请求方法
   * @param {string[]} [options.headers=[]] 待检查的 CORS-不安全请求头
   * @returns {{
   *   ok: boolean,
   *   reason: ('no-entry'|'credentials-mismatch'|'missing-permission'|null),
   *   missingMethods: string[],
   *   missingHeaders: string[],
   *   matchedEntry: object|null,
   *   checkedAt: number
   * }}
   */
  check({ partition, origin, url, credentials = false, method, headers = [] }) {
    const partitionKey = normalizePartition(partition);
    const originKey = normalizeOrigin(origin);
    const urlKey = normalizeUrl(url);
    const requestedMethod = normalizeMethod(method);
    const requestedHeaders = normalizeNameList(headers, 'headers');
    if (typeof credentials !== 'boolean') {
      throw new TypeError('credentials 必须是布尔值');
    }

    const now = this._now();
    const base = {
      ok: false,
      reason: null,
      missingMethods: [],
      missingHeaders: [],
      matchedEntry: null,
      checkedAt: now,
    };

    const entry = this._partitions
      .get(partitionKey)
      ?.get(originKey)
      ?.get(urlKey.href);

    if (!entry) {
      return { ...base, reason: 'no-entry' };
    }

    const snapshot = this._snapshot(partitionKey, originKey, urlKey, entry);
    base.matchedEntry = snapshot;

    // 带凭据请求只能命中带凭据登记的条目。
    // 无凭据申请得到的通配许可（credentials=false）永远不能覆盖带凭据请求，
    // 即使其中包含显式方法/头许可也不行——凭据模式是独立门槛。
    if (credentials && !entry.credentials) {
      return {
        ...base,
        reason: 'credentials-mismatch',
        missingMethods: [requestedMethod],
        missingHeaders: [...requestedHeaders],
      };
    }

    // 方法匹配：显式许可未过期；'*' 仅对无凭据请求有效。
    const methodCovered = this._isFieldCovered(
      entry.methodExpires, requestedMethod, credentials, now,
    );

    // 头匹配：逐个登记独立过期时刻。
    // Authorization 只能由显式、未过期的许可覆盖：通配 '*' 对它永远无效，
    // 即使请求不带凭据也一样（Fetch 标准 CORS-check 规则）。
    const missingHeaders = requestedHeaders.filter(
      (header) => !this._isFieldCovered(
        entry.headerExpires,
        header,
        credentials,
        now,
        header === 'authorization',
      ),
    );

    const missingMethods = methodCovered ? [] : [requestedMethod];
    const ok = methodCovered && missingHeaders.length === 0;

    return {
      ...base,
      ok,
      reason: ok ? null : 'missing-permission',
      missingMethods,
      missingHeaders,
    };
  }

  /**
   * 单个字段（方法或头）是否被有效许可覆盖。
   * - 显式许可未过期即可覆盖；
   * - 通配 '*' 不适用带凭据请求；
   * - denyWildcard（用于 Authorization）为真时通配一律不生效，
   *   即该字段必须存在显式且未过期的许可。
   */
  _isFieldCovered(expiresMap, field, credentials, now, denyWildcard = false) {
    const explicit = expiresMap.get(field);
    // 过期判定是严格大于：expiresAt === now 即视为已过期（maxAge=0 的边界）。
    if (explicit !== undefined && explicit > now) {
      return true;
    }
    if (credentials || denyWildcard) {
      return false;
    }
    const wildcard = expiresMap.get(WILDCARD);
    return wildcard !== undefined && wildcard > now;
  }

  _snapshot(partitionKey, originKey, urlKey, entry) {
    return {
      partition: partitionKey,
      origin: originKey,
      url: urlKey.href,
      credentials: entry.credentials,
      methodExpires: Object.fromEntries(entry.methodExpires),
      headerExpires: Object.fromEntries(entry.headerExpires),
    };
  }

  /** 查看某三元组下的条目（不改变任何过期时刻），不存在返回 null。 */
  peek({ partition, origin, url }) {
    const partitionKey = normalizePartition(partition);
    const originKey = normalizeOrigin(origin);
    const urlKey = normalizeUrl(url);
    const entry = this._partitions
      .get(partitionKey)
      ?.get(originKey)
      ?.get(urlKey.href);
    return entry ? this._snapshot(partitionKey, originKey, urlKey, entry) : null;
  }

  /** 清理所有字段均已过期的条目，返回清理掉的条目数。只读取时钟，不续期。 */
  purgeExpired() {
    const now = this._now();
    let removed = 0;
    for (const origins of this._partitions.values()) {
      for (const urls of origins.values()) {
        for (const [href, entry] of urls) {
          const methodLive = [...entry.methodExpires.values()].some((t) => t > now);
          const headerLive = [...entry.headerExpires.values()].some((t) => t > now);
          if (!methodLive && !headerLive) {
            urls.delete(href);
            removed += 1;
          }
        }
      }
    }
    // 清理空的 origin 层与 partition 层。
    for (const [, origins] of this._partitions) {
      for (const [ok, urls] of origins) {
        if (urls.size === 0) origins.delete(ok);
      }
    }
    for (const [pk, origins] of this._partitions) {
      if (origins.size === 0) this._partitions.delete(pk);
    }
    return removed;
  }

  clear() {
    this._partitions.clear();
  }

  get size() {
    let count = 0;
    for (const origins of this._partitions.values()) {
      for (const urls of origins.values()) {
        count += urls.size;
      }
    }
    return count;
  }
}

export default PreflightCache;
