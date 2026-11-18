import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseIp, parseIpv4, parseIpv6, parseCidr, matchCidr } from '../src/ip.mjs';

test('IPv4 解析：合法与非法', () => {
  assert.ok(parseIpv4('192.168.0.1'));
  assert.ok(parseIpv4('0.0.0.0'));
  assert.ok(parseIpv4('255.255.255.255'));
  assert.equal(parseIpv4('256.0.0.1'), null);
  assert.equal(parseIpv4('1.2.3'), null);
  assert.equal(parseIpv4('1.2.3.4.5'), null);
  assert.equal(parseIpv4('01.2.3.4'), null); // 前导零拒绝
  assert.equal(parseIpv4('a.b.c.d'), null);
  assert.equal(parseIpv4(''), null);
});

test('IPv6 解析：完整、压缩与嵌入 IPv4', () => {
  assert.ok(parseIpv6('2001:0db8:0000:0000:0000:0000:0000:0001'));
  assert.ok(parseIpv6('2001:db8::1'));
  assert.ok(parseIpv6('::'));
  assert.ok(parseIpv6('::1'));
  assert.ok(parseIpv6('::ffff:192.0.2.1'));
  assert.ok(parseIpv6('::192.0.2.1'));
  assert.ok(parseIpv6('1::192.0.2.1'));
  assert.equal(parseIpv6('2001:db8:::1'), null);
  assert.equal(parseIpv6('12345::'), null);
  assert.equal(parseIpv6('1:2:3:4:5:6:7:8:9'), null);
  assert.equal(parseIpv6('1:2:3:4:5:6:7'), null);
  assert.equal(parseIpv6('fe80::1%eth0'), null); // 不接受 zone id
  assert.equal(parseIpv6('::ffff:999.0.2.1'), null);
});

test('parseIp 自动识别版本', () => {
  assert.equal(parseIp('10.0.0.1').version, 4);
  assert.equal(parseIp('2001:db8::1').version, 6);
  assert.equal(parseIp('garbage'), null);
  assert.equal(parseIp(''), null);
  assert.equal(parseIp(null), null);
});

test('CIDR 匹配：IPv4', () => {
  assert.equal(matchCidr('10.1.2.3', '10.0.0.0/8'), true);
  assert.equal(matchCidr('11.0.0.1', '10.0.0.0/8'), false);
  assert.equal(matchCidr('10.0.0.3', '10.0.0.0/30'), true);
  assert.equal(matchCidr('10.0.0.4', '10.0.0.0/30'), false);
  assert.equal(matchCidr('10.0.0.1', '10.0.0.1'), true); // 单主机
  assert.equal(matchCidr('10.0.0.1', '10.0.0.2'), false);
  assert.equal(matchCidr('10.0.0.1', '0.0.0.0/0'), true);
});

test('CIDR 匹配：IPv6 与跨版本', () => {
  assert.equal(matchCidr('2001:db8:1::9', '2001:db8::/32'), true);
  assert.equal(matchCidr('2001:db9::1', '2001:db8::/32'), false);
  assert.equal(matchCidr('::ffff:192.0.2.1', '::/0'), true);
  assert.equal(matchCidr('10.0.0.1', '2001:db8::/32'), false); // 版本不一致
  assert.equal(matchCidr('10.0.0.1', 'bad-cidr'), false);
});

test('parseCidr 非法输入', () => {
  assert.equal(parseCidr('10.0.0.0/33'), null);
  assert.equal(parseCidr('2001:db8::/129'), null);
  assert.equal(parseCidr('10.0.0.0/x'), null);
  assert.equal(parseCidr('nope/8'), null);
});
