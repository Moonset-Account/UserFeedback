import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import {
  computeCommitment,
  safeEqualDigest,
  toHex,
  fromHex,
  FIELD_ORDER,
} from '../src/digest.mjs';

const BASE = {
  project: 'phoenix',
  reporter: 'alice',
  finding: 'CVE-SYNTH-2026-0001',
  salt: 's4lt',
};

describe('存证摘要：固定顺序 + 四字节大端长度前缀 + SHA-256', () => {
  test('已知答案（KAT）与手工字节拼接结果一致', () => {
    const expected = '5168130ab8eeece2aa00929776c93ae63bc1ab3d25f91e0531786e0b07d71607';
    assert.equal(toHex(computeCommitment(BASE)), expected);

    // 手工构造同样的字节流，独立验证长度前缀与拼接顺序。
    const manual = Buffer.concat([
      ...FIELD_ORDER.map((key) => {
        const payload = Buffer.from(BASE[key], 'utf8');
        const header = Buffer.alloc(4);
        header.writeUInt32BE(payload.length, 0);
        return Buffer.concat([header, payload]);
      }),
    ]);
    const manualDigest = createHash('sha256').update(manual).digest('hex');
    assert.equal(manualDigest, expected);
  });

  test('字段顺序是承诺的一部分：交换项目与报告人得到不同摘要', () => {
    const swapped = computeCommitment({
      ...BASE,
      project: BASE.reporter,
      reporter: BASE.project,
    });
    assert.notEqual(toHex(swapped), toHex(computeCommitment(BASE)));
    assert.equal(
      toHex(swapped),
      '09c6ad1f617827b6b070557d13be8205ab75779d47e09cc193edae980b5f1844',
    );
  });

  test('长度前缀消除边界歧义：(f1+f2 拼接) 与 (f1, f2 分字段) 摘要不同', () => {
    const split = computeCommitment({ ...BASE, finding: 'f1' });
    const merged = computeCommitment({ ...BASE, finding: 'f1f2' });
    const crossField = computeCommitment({
      ...BASE,
      project: BASE.project + 'f2',
      finding: 'f1',
    });
    assert.notEqual(toHex(split), toHex(merged));
    assert.notEqual(toHex(split), toHex(crossField));
    assert.equal(
      toHex(split),
      'aef0c1a282bcfce51946d71b1b31a1e413e27aabf6ee4a4babdf075e8f9b866c',
    );
  });

  test('多字节 UTF-8 按字节长度编码，结果稳定可复现', () => {
    const unicode = {
      project: '朱雀项目',
      reporter: '研究员甲',
      finding: '指纹-🔒-01',
      salt: '盐值🧂',
    };
    const first = computeCommitment(unicode);
    const second = computeCommitment(unicode);
    assert.equal(first.length, 32);
    assert.ok(first.equals(second));
    assert.equal(
      toHex(first),
      'c0a89e1f5cd6554c0e578a3cc6a68ce3d563b343c92884884b080c54a51cabe6',
    );
  });

  test('任一字段变化（包括盐）都会改变摘要', () => {
    const baseline = toHex(computeCommitment(BASE));
    for (const key of FIELD_ORDER) {
      const mutated = { ...BASE, [key]: `${BASE[key]}x` };
      assert.notEqual(toHex(computeCommitment(mutated)), baseline, `字段 ${key} 变化未反映到摘要`);
    }
  });

  test('非法输入被拒绝', () => {
    assert.throws(() => computeCommitment({ ...BASE, project: 123 }), TypeError);
    assert.throws(() => computeCommitment(null), TypeError);
    assert.throws(() => fromHex('abc'), TypeError);
    assert.throws(() => fromHex('z'.repeat(64)), TypeError);
    assert.equal(fromHex(toHex(computeCommitment(BASE))).equals(computeCommitment(BASE)), true);
  });

  test('safeEqualDigest 常量时间比较：等长相等为真，长度不符安全返回假', () => {
    const a = computeCommitment(BASE);
    const b = computeCommitment(BASE);
    const c = computeCommitment({ ...BASE, salt: 'other' });
    assert.equal(safeEqualDigest(a, b), true);
    assert.equal(safeEqualDigest(a, c), false);
    assert.equal(safeEqualDigest(a, Buffer.alloc(16)), false);
    assert.equal(safeEqualDigest(a, Buffer.alloc(32)), false);
  });
});
