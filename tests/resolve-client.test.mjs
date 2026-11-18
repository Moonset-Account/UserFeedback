import { test } from 'node:test';
import assert from 'node:assert/strict';
import { resolveClientAddress, REASONS } from '../src/resolve-client.mjs';

// 两层反向代理的入口配置：内层 10.0.0.0/8，外层 172.16.0.0/12
const twoLayerEntry = {
  name: 'edge',
  trustedProxies: ['10.0.0.0/8', '172.16.0.0/12'],
  maxHops: 4,
  onInvalid: 'reject',
};

test('可接受：两层可信代理链解析出真实客户端', () => {
  const result = resolveClientAddress({
    remoteAddress: '10.0.0.2',
    forwardedFor: '203.0.113.7, 172.16.5.4',
    entry: twoLayerEntry,
  });
  assert.equal(result.status, 'resolved');
  assert.equal(result.clientAddress, '203.0.113.7');
  assert.equal(result.evidence.source, 'forwarded-chain');
  // 证据：连接对端与链尾一跳均受信，首个不受信的跳是链首
  assert.deepEqual(result.evidence.trustedHops, ['10.0.0.2', '172.16.5.4']);
  assert.equal(result.evidence.untrustedHop, '203.0.113.7');
  assert.deepEqual(result.evidence.usedSegment, ['203.0.113.7']);
});

test('可接受：直连（无转发链）使用连接对端', () => {
  const result = resolveClientAddress({
    remoteAddress: '198.51.100.9',
    entry: twoLayerEntry,
  });
  assert.equal(result.status, 'resolved');
  assert.equal(result.clientAddress, '198.51.100.9');
  assert.equal(result.evidence.source, 'connection');
  assert.equal(result.evidence.reason, 'direct-connection');
});

test('可接受：链中混有不受信的跳，停在首个不受信处', () => {
  // 链尾 10.1.1.1 受信，172.20.0.1 受信，8.8.8.8 不受信 -> 客户端为 8.8.8.8，
  // 链首 1.2.3.4 是不可信的更早声明，不能采信
  const result = resolveClientAddress({
    remoteAddress: '10.0.0.2',
    forwardedFor: '1.2.3.4, 8.8.8.8, 172.20.0.1, 10.1.1.1',
    entry: twoLayerEntry,
  });
  assert.equal(result.status, 'resolved');
  assert.equal(result.clientAddress, '8.8.8.8');
  assert.deepEqual(result.evidence.trustedHops, ['10.0.0.2', '10.1.1.1', '172.20.0.1']);
  assert.equal(result.evidence.untrustedHop, '8.8.8.8');
  assert.deepEqual(result.evidence.usedSegment, ['1.2.3.4', '8.8.8.8']);
});

test('可接受：整条链均受信时取链首声明', () => {
  const result = resolveClientAddress({
    remoteAddress: '10.0.0.2',
    forwardedFor: '10.2.3.4, 172.16.9.9',
    entry: twoLayerEntry,
  });
  assert.equal(result.status, 'resolved');
  assert.equal(result.clientAddress, '10.2.3.4');
  assert.equal(result.evidence.untrustedHop, null);
});

test('拒绝：转发链格式错误且入口策略为 reject', () => {
  const result = resolveClientAddress({
    remoteAddress: '10.0.0.2',
    forwardedFor: '203.0.113.7, not-an-ip, 172.16.5.4',
    entry: twoLayerEntry,
  });
  assert.equal(result.status, 'rejected');
  assert.equal(result.clientAddress, null);
  assert.equal(result.evidence.reason, REASONS.MALFORMED_CHAIN);
  assert.deepEqual(result.evidence.declaredChain, ['203.0.113.7', 'not-an-ip', '172.16.5.4']);
});

test('拒绝：转发链超过入口允许的最大跳数', () => {
  const result = resolveClientAddress({
    remoteAddress: '10.0.0.2',
    forwardedFor: '1.1.1.1, 2.2.2.2, 3.3.3.3, 4.4.4.4, 5.5.5.5',
    entry: twoLayerEntry, // maxHops: 4
  });
  assert.equal(result.status, 'rejected');
  assert.equal(result.evidence.reason, REASONS.HOP_LIMIT_EXCEEDED);
  assert.equal(result.evidence.maxHops, 4);
});

test('拒绝：连接对端地址非法', () => {
  const result = resolveClientAddress({
    remoteAddress: '999.1.2.3',
    forwardedFor: '203.0.113.7',
    entry: twoLayerEntry,
  });
  assert.equal(result.status, 'rejected');
  assert.equal(result.evidence.reason, REASONS.INVALID_PEER);
});

test('边界：跳数恰好等于 maxHops 时接受，多一跳则拒绝', () => {
  const atLimit = resolveClientAddress({
    remoteAddress: '10.0.0.2',
    forwardedFor: '203.0.113.7, 172.16.5.4, 10.3.3.3, 10.4.4.4', // 恰好 4 跳
    entry: twoLayerEntry,
  });
  assert.equal(atLimit.status, 'resolved');
  assert.equal(atLimit.clientAddress, '203.0.113.7');

  const overLimit = resolveClientAddress({
    remoteAddress: '10.0.0.2',
    forwardedFor: '203.0.113.7, 172.16.5.4, 10.3.3.3, 10.4.4.4, 10.5.5.5', // 5 跳
    entry: twoLayerEntry,
  });
  assert.equal(overLimit.status, 'rejected');
  assert.equal(overLimit.evidence.reason, REASONS.HOP_LIMIT_EXCEEDED);
});

test('交错：不受信的连接对端声明的链不参与判断，回退为连接对端', () => {
  const result = resolveClientAddress({
    remoteAddress: '203.0.113.99', // 不在 trustedProxies 内
    forwardedFor: '1.2.3.4, 5.6.7.8',
    entry: twoLayerEntry,
  });
  assert.equal(result.status, 'resolved');
  assert.equal(result.clientAddress, '203.0.113.99');
  assert.equal(result.evidence.source, 'connection');
  assert.equal(result.evidence.reason, 'peer-not-trusted-chain-ignored');
});

test('交错：格式错误且入口策略为 fallback 时回退到连接对端并注明证据', () => {
  const result = resolveClientAddress({
    remoteAddress: '10.0.0.2',
    forwardedFor: '203.0.113.7,, 172.16.5.4', // 空段 -> 格式错误
    entry: { ...twoLayerEntry, onInvalid: 'fallback' },
  });
  assert.equal(result.status, 'resolved');
  assert.equal(result.clientAddress, '10.0.0.2');
  assert.equal(result.evidence.source, 'connection');
  assert.equal(result.evidence.reason, `fallback:${REASONS.MALFORMED_CHAIN}`);
});

test('交错：超跳数且入口策略为 fallback 时回退到连接对端', () => {
  const result = resolveClientAddress({
    remoteAddress: '10.0.0.2',
    forwardedFor: '1.1.1.1, 2.2.2.2, 3.3.3.3, 4.4.4.4, 5.5.5.5',
    entry: { ...twoLayerEntry, onInvalid: 'fallback' },
  });
  assert.equal(result.status, 'resolved');
  assert.equal(result.clientAddress, '10.0.0.2');
  assert.equal(result.evidence.reason, `fallback:${REASONS.HOP_LIMIT_EXCEEDED}`);
});

test('交错：链同时格式错误且超跳数时，格式错误优先判定', () => {
  const result = resolveClientAddress({
    remoteAddress: '10.0.0.2',
    forwardedFor: '1.1.1.1, bogus, 3.3.3.3, 4.4.4.4, 5.5.5.5',
    entry: twoLayerEntry,
  });
  assert.equal(result.status, 'rejected');
  assert.equal(result.evidence.reason, REASONS.MALFORMED_CHAIN);
});

test('可接受：IPv6 代理链解析', () => {
  const result = resolveClientAddress({
    remoteAddress: '2001:db8::2',
    forwardedFor: '2001:db8:1::9, 2001:db8::1',
    entry: {
      name: 'v6-edge',
      trustedProxies: ['2001:db8::/32'],
      maxHops: 4,
      onInvalid: 'reject',
    },
  });
  assert.equal(result.status, 'resolved');
  assert.equal(result.clientAddress, '2001:db8:1::9');
  assert.equal(result.evidence.source, 'forwarded-chain');
});

test('可接受：CIDR 边界地址（子网首尾）按受信处理', () => {
  const entry = { name: 'edge', trustedProxies: ['10.0.0.0/30'], maxHops: 2, onInvalid: 'reject' };
  // 10.0.0.0/30 覆盖 10.0.0.0 - 10.0.0.3
  const inRange = resolveClientAddress({
    remoteAddress: '10.0.0.3',
    forwardedFor: '203.0.113.7',
    entry,
  });
  assert.equal(inRange.status, 'resolved');
  assert.equal(inRange.clientAddress, '203.0.113.7');

  const outOfRange = resolveClientAddress({
    remoteAddress: '10.0.0.4',
    forwardedFor: '203.0.113.7',
    entry,
  });
  assert.equal(outOfRange.status, 'resolved');
  assert.equal(outOfRange.clientAddress, '10.0.0.4');
  assert.equal(outOfRange.evidence.reason, 'peer-not-trusted-chain-ignored');
});
