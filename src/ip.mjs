// IP 解析与 CIDR 匹配工具（支持 IPv4 / IPv6，含 IPv4 映射的 IPv6）。

/**
 * 解析 IPv4 地址，返回 4 字节 Uint8Array；非法返回 null。
 */
export function parseIpv4(input) {
  if (typeof input !== 'string') return null;
  const parts = input.split('.');
  if (parts.length !== 4) return null;
  const bytes = new Uint8Array(4);
  for (let i = 0; i < 4; i += 1) {
    const p = parts[i];
    if (!/^\d{1,3}$/.test(p)) return null;
    // 禁止前导零（"01"），避免八进制歧义
    if (p.length > 1 && p[0] === '0') return null;
    const n = Number(p);
    if (n > 255) return null;
    bytes[i] = n;
  }
  return bytes;
}

function parseHextet(token) {
  if (!/^[0-9a-fA-F]{1,4}$/.test(token)) return null;
  return parseInt(token, 16);
}

/**
 * 解析 IPv6 地址（支持 "::" 压缩与末尾嵌入 IPv4），返回 16 字节 Uint8Array；非法返回 null。
 */
export function parseIpv6(input) {
  if (typeof input !== 'string') return null;
  let s = input;
  if (s.includes('%')) return null; // 不接受 zone id

  // 末尾嵌入 IPv4，如 ::ffff:192.0.2.1 —— 先取出，按两段 hextet 参与计数
  let tailGroups = [];
  const lastColon = s.lastIndexOf(':');
  if (lastColon !== -1 && s.slice(lastColon + 1).includes('.')) {
    const v4 = parseIpv4(s.slice(lastColon + 1));
    if (!v4) return null;
    tailGroups = [(v4[0] << 8) | v4[1], (v4[2] << 8) | v4[3]];
    s = s.slice(0, lastColon); // 去掉 ":ipv4"，保留前面的 "::" 或 ":"
    // 形如 "::192.0.2.1" 截断后剩 ":"，需要补回 "::" 的压缩标记
    if (s.endsWith(':') && !s.endsWith('::')) s += ':';
  }

  const dcParts = s.split('::');
  if (dcParts.length > 2) return null;

  const parseSide = (side) => {
    if (side === '') return [];
    const tokens = side.split(':');
    const out = [];
    for (const t of tokens) {
      const v = parseHextet(t);
      if (v === null) return null;
      out.push(v);
    }
    return out;
  };

  const left = parseSide(dcParts[0]);
  if (left === null) return null;
  let right = [];
  if (dcParts.length === 2) {
    right = parseSide(dcParts[1]);
    if (right === null) return null;
  }

  const fixed = left.length + right.length + tailGroups.length;
  if (dcParts.length === 2) {
    if (fixed > 8) return null;
  } else if (fixed !== 8) {
    return null;
  }

  const groups = new Array(8).fill(0);
  for (let i = 0; i < left.length; i += 1) groups[i] = left[i];
  for (let i = 0; i < right.length; i += 1) {
    groups[8 - tailGroups.length - right.length + i] = right[i];
  }
  for (let i = 0; i < tailGroups.length; i += 1) {
    groups[8 - tailGroups.length + i] = tailGroups[i];
  }

  const bytes = new Uint8Array(16);
  for (let i = 0; i < 8; i += 1) {
    bytes[i * 2] = (groups[i] >> 8) & 0xff;
    bytes[i * 2 + 1] = groups[i] & 0xff;
  }
  return bytes;
}

/**
 * 解析 IP 字符串，返回 { version: 4|6, bytes: Uint8Array }；非法返回 null。
 */
export function parseIp(input) {
  if (typeof input !== 'string') return null;
  const s = input.trim();
  if (s === '') return null;
  if (s.includes(':')) {
    const bytes = parseIpv6(s);
    return bytes ? { version: 6, bytes } : null;
  }
  const bytes = parseIpv4(s);
  return bytes ? { version: 4, bytes } : null;
}

/**
 * 解析 CIDR（如 "10.0.0.0/8"、"2001:db8::/32"），返回 { version, bytes, bits }；非法返回 null。
 * 不带 "/" 的视为单主机（/32 或 /128）。
 */
export function parseCidr(input) {
  if (typeof input !== 'string') return null;
  const s = input.trim();
  const slash = s.indexOf('/');
  if (slash === -1) {
    const ip = parseIp(s);
    if (!ip) return null;
    return { version: ip.version, bytes: ip.bytes, bits: ip.version === 4 ? 32 : 128 };
  }
  const ip = parseIp(s.slice(0, slash));
  if (!ip) return null;
  const lenStr = s.slice(slash + 1);
  if (!/^\d{1,3}$/.test(lenStr)) return null;
  const bits = Number(lenStr);
  const max = ip.version === 4 ? 32 : 128;
  if (bits > max) return null;
  return { version: ip.version, bytes: ip.bytes, bits };
}

/**
 * 判断 ip 是否落在 cidr 内。两者都需可解析且版本一致，否则返回 false。
 */
export function matchCidr(ipInput, cidrInput) {
  const ip = parseIp(ipInput);
  const cidr = parseCidr(cidrInput);
  if (!ip || !cidr || ip.version !== cidr.version) return false;
  const fullBytes = Math.floor(cidr.bits / 8);
  const remBits = cidr.bits % 8;
  for (let i = 0; i < fullBytes; i += 1) {
    if (ip.bytes[i] !== cidr.bytes[i]) return false;
  }
  if (remBits > 0) {
    const mask = (0xff << (8 - remBits)) & 0xff;
    if ((ip.bytes[fullBytes] & mask) !== (cidr.bytes[fullBytes] & mask)) return false;
  }
  return true;
}
