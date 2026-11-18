// 入口：网关客户端地址解析模块
export { resolveClientAddress, normalizeEntry, REASONS } from './src/resolve-client.mjs';
export { parseIp, parseIpv4, parseIpv6, parseCidr, matchCidr } from './src/ip.mjs';
