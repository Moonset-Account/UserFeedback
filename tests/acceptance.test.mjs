import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  sealManifest,
  createDisclosure,
  verifyDisclosure,
  serializeRecord,
  parseRecord,
  serializeBundle,
  parseBundle,
  leafDigest,
} from '../src/index.js';

const ENTRIES = [
  { name: '合同正本.pdf', content: 'contract-body-机密' },
  { name: '往来邮件.eml', content: 'email-body-机密' },
  { name: '现场照片.jpg', content: Buffer.from([0x89, 0x50, 0x4e, 0x47]) }, // 二进制内容
  { name: '重复声明.txt', content: 'DUPLICATE-BODY' },
  { name: '重复声明副本.txt', content: 'DUPLICATE-BODY' }, // 同内容、不同位置
  { name: '内部审计.docx', content: 'audit-body-机密' },
];

function sealSample() {
  return sealManifest(ENTRIES, { sealedAt: '2026-11-19T00:00:00.000Z' });
}

function deepCloneBundle(bundle) {
  return parseBundle(serializeBundle(bundle));
}

describe('可接受结果：合法披露副本通过验证', () => {
  test('部分位置批准、交错遮盖的副本通过（经 JSON 序列化往返）', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    // 交错批准：0、2、4，其余位置被遮盖
    const approved = [0, 2, 4];
    const bundle = createDisclosure(sealedPackage, approved);

    // 复核端独立取得可信根（模拟带外渠道：先序列化再解析）
    const recordForReviewer = parseRecord(serializeRecord(sealedRecord));
    const bundleForReviewer = deepCloneBundle(bundle);

    const result = verifyDisclosure(bundleForReviewer, recordForReviewer, approved);
    assert.equal(result.ok, true);
    assert.equal(result.verifiedCount, 3);
    assert.equal(result.leafCount, ENTRIES.length);
  });

  test('全部位置批准也通过，且严格按位置升序', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [5, 3, 1, 4, 2, 0]; // 乱序提供，内部排序处理
    const bundle = createDisclosure(sealedPackage, approved);
    assert.deepEqual(bundle.entries.map((e) => e.position), [0, 1, 2, 3, 4, 5]);
    const result = verifyDisclosure(bundle, sealedRecord, [0, 1, 2, 3, 4, 5]);
    assert.equal(result.ok, true);
    assert.equal(result.verifiedCount, 6);
  });

  test('空批准清单生成空副本，验证通过（最小披露边界）', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const bundle = createDisclosure(sealedPackage, []);
    assert.deepEqual(bundle.entries, []);
    const result = verifyDisclosure(bundle, sealedRecord, []);
    assert.equal(result.ok, true);
    assert.equal(result.verifiedCount, 0);
  });

  test('二进制内容经编解码后仍可验证', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const bundle = parseBundle(serializeBundle(createDisclosure(sealedPackage, [2])));
    assert.ok(Buffer.isBuffer(bundle.entries[0].content));
    assert.deepEqual(bundle.entries[0].content, ENTRIES[2].content);
    assert.equal(verifyDisclosure(bundle, sealedRecord, [2]).ok, true);
  });
});

describe('交叉约束：重复内容在两个位置各自验证', () => {
  test('同内容不同位置的两条目独立盐、独立承诺、各自证明通过', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const bundle = createDisclosure(sealedPackage, [3, 4]);
    const [a, b] = bundle.entries;
    assert.equal(a.content, b.content);
    assert.notDeepEqual(a.salt, b.salt); // 盐独立
    assert.notDeepEqual(a.digest, b.digest); // 位置 + 盐使承诺不同
    // 两条目均携带非空证明：层级严格递增且落在树深范围内（提升层允许跳号）
    assert.ok(a.proof.length > 0 && b.proof.length > 0);
    const maxLevel = Math.ceil(Math.log2(ENTRIES.length));
    for (const entry of [a, b]) {
      let previousLevel = -1;
      for (const step of entry.proof) {
        assert.ok(step.level > previousLevel && step.level < maxLevel);
        previousLevel = step.level;
        assert.ok(step.side === 'left' || step.side === 'right');
        assert.equal(step.digest.length, 32);
      }
    }
    assert.equal(verifyDisclosure(bundle, sealedRecord, [3, 4]).ok, true);
  });

  test('独立小封存中的同内容两位置也各自验证成功', () => {
    const seal = sealManifest([
      { name: 'a.txt', content: 'DUPLICATE-BODY' },
      { name: 'b.txt', content: 'DUPLICATE-BODY' },
    ]);
    const bundle = createDisclosure(seal.sealedPackage, [0, 1]);
    assert.notDeepEqual(bundle.entries[0].salt, bundle.entries[1].salt);
    assert.notDeepEqual(bundle.entries[0].digest, bundle.entries[1].digest);
    assert.equal(verifyDisclosure(bundle, seal.sealedRecord, [0, 1]).ok, true);
  });
});

describe('应被阻止：换序、缺失、未批准、篡改均失败', () => {
  test('副本条目换序 → ORDER_VIOLATION', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [0, 2, 4];
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, approved));
    bundle.entries = [bundle.entries[2], bundle.entries[0], bundle.entries[1]];
    const result = verifyDisclosure(bundle, sealedRecord, approved);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'ORDER_VIOLATION');
  });

  test('缺少获批条目 → MISSING_POSITION', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [0, 2, 4];
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, approved));
    bundle.entries = bundle.entries.filter((e) => e.position !== 2);
    const result = verifyDisclosure(bundle, sealedRecord, approved);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'MISSING_POSITION');
    assert.equal(result.position, 2);
  });

  test('出现未批准条目 → UNAPPROVED_POSITION', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, [0, 1]));
    const result = verifyDisclosure(bundle, sealedRecord, [0]); // 复核方只批准 0
    assert.equal(result.ok, false);
    assert.equal(result.code, 'UNAPPROVED_POSITION');
    assert.equal(result.position, 1);
  });

  test('证据内容变化（副本自带摘要未同步）→ LEAF_DIGEST_MISMATCH', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [0, 1];
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, approved));
    bundle.entries[0].content = '被篡改的内容';
    const result = verifyDisclosure(bundle, sealedRecord, approved);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'LEAF_DIGEST_MISMATCH');
    assert.equal(result.position, 0);
  });

  test('证据内容变化且攻击者重算叶子摘要 → 证明层 ROOT_MISMATCH', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [0, 1];
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, approved));
    const entry = bundle.entries[0];
    entry.content = '被篡改的内容';
    // 攻击者同步伪造叶子摘要，但无法伪造包含证明
    entry.digest = leafDigest({
      position: entry.position, salt: entry.salt, name: entry.name, content: entry.content,
    });
    const result = verifyDisclosure(bundle, sealedRecord, approved);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'ROOT_MISMATCH');
    assert.equal(result.position, 0);
  });

  test('盐被替换 → LEAF_DIGEST_MISMATCH', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [1];
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, approved));
    bundle.entries[0].salt[0] ^= 0x01;
    const result = verifyDisclosure(bundle, sealedRecord, approved);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'LEAF_DIGEST_MISMATCH');
  });

  test('名称被替换 → LEAF_DIGEST_MISMATCH', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [1];
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, approved));
    bundle.entries[0].name = '伪造名称.eml';
    const result = verifyDisclosure(bundle, sealedRecord, approved);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'LEAF_DIGEST_MISMATCH');
  });

  test('条目互换位置并重算承诺 → 证明层 ROOT_MISMATCH', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [0, 1];
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, approved));
    const first = bundle.entries[0];
    const second = bundle.entries[1];
    // 交换位置标签并按新位置重算叶子（内容、盐随之归属到错误位置）
    first.position = 1;
    first.digest = leafDigest({
      position: 1, salt: first.salt, name: first.name, content: first.content,
    });
    second.position = 0;
    second.digest = leafDigest({
      position: 0, salt: second.salt, name: second.name, content: second.content,
    });
    bundle.entries = [second, first]; // 保持升序，排除 ORDER_VIOLATION
    const result = verifyDisclosure(bundle, sealedRecord, approved);
    assert.equal(result.ok, false);
    // 原证明的方向/层级与新位置不再匹配（PROOF_MALFORMED）；即便重算证明，
    // 无原叶子也无法得到可信根（ROOT_MISMATCH）——两种失败均属有效拒绝。
    assert.ok(result.code === 'PROOF_MALFORMED' || result.code === 'ROOT_MISMATCH',
      `意外的错误码: ${result.code}`);
  });

  test('使用另一次封存的可信根 → ROOT_MISMATCH', () => {
    const { sealedPackage } = sealSample();
    const otherSeal = sealManifest(ENTRIES);
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, [0, 2]));
    const result = verifyDisclosure(bundle, otherSeal.sealedRecord, [0, 2]);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'ROOT_MISMATCH');
  });

  test('可信根被篡改 → ROOT_MISMATCH', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const tamperedRecord = parseRecord(serializeRecord(sealedRecord));
    tamperedRecord.root[0] ^= 0xff;
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, [0]));
    const result = verifyDisclosure(bundle, tamperedRecord, [0]);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'ROOT_MISMATCH');
  });

  test('证明摘要被篡改 → ROOT_MISMATCH', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [0];
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, approved));
    bundle.entries[0].proof[0].digest[0] ^= 0xff;
    const result = verifyDisclosure(bundle, sealedRecord, approved);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'ROOT_MISMATCH');
  });

  test('证明方向被伪造 → PROOF_MALFORMED', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [0];
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, approved));
    bundle.entries[0].proof[0].side = 'left';
    const result = verifyDisclosure(bundle, sealedRecord, approved);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'PROOF_MALFORMED');
  });

  test('条目位置被伪造为越界值 → POSITION_OUT_OF_RANGE', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const approved = [0];
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, approved));
    bundle.entries[0].position = 99;
    const result = verifyDisclosure(bundle, sealedRecord, approved);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'POSITION_OUT_OF_RANGE');
  });

  test('副本与记录 leafCount 不一致 → LEAF_COUNT_MISMATCH', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, [0]));
    bundle.leafCount = 5;
    const result = verifyDisclosure(bundle, sealedRecord, [0]);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'LEAF_COUNT_MISMATCH');
  });

  test('批准清单含越界或重复位置 → APPROVED_LIST_MALFORMED', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const bundle = deepCloneBundle(createDisclosure(sealedPackage, [0]));
    assert.equal(verifyDisclosure(bundle, sealedRecord, [99]).code, 'APPROVED_LIST_MALFORMED');
    assert.equal(verifyDisclosure(bundle, sealedRecord, [0, 0]).code, 'APPROVED_LIST_MALFORMED');
  });
});

describe('边界与交错场景', () => {
  test('单条目封存：空证明、空批准与全披露都成立', () => {
    const single = sealManifest([{ name: 'solo.txt', content: 'only-one' }]);
    assert.equal(verifyDisclosure(
      createDisclosure(single.sealedPackage, []), single.sealedRecord, [],
    ).ok, true);
    const full = createDisclosure(single.sealedPackage, [0]);
    assert.deepEqual(full.entries[0].proof, []);
    assert.equal(verifyDisclosure(full, single.sealedRecord, [0]).ok, true);
  });

  test('两次封存同清单：独立盐产生不同可信根，交叉验证全部失败', () => {
    const first = sealSample();
    const second = sealSample();
    assert.ok(!first.sealedRecord.root.equals(second.sealedRecord.root));
    const bundleFromFirst = deepCloneBundle(createDisclosure(first.sealedPackage, [0]));
    assert.equal(verifyDisclosure(bundleFromFirst, second.sealedRecord, [0]).code, 'ROOT_MISMATCH');
    // 各次封存自身仍然有效
    assert.equal(
      verifyDisclosure(bundleFromFirst, first.sealedRecord, [0]).ok,
      true,
    );
  });

  test('生成阶段拒绝非法批准位置（越界/重复）', () => {
    const { sealedPackage } = sealSample();
    assert.throws(() => createDisclosure(sealedPackage, [6]), /超出范围/);
    assert.throws(() => createDisclosure(sealedPackage, [0, 0]), /重复/);
  });

  test('封存拒绝空清单与非法条目', () => {
    assert.throws(() => sealManifest([]), /非空数组/);
    assert.throws(() => sealManifest([{ name: '', content: 'x' }]), /name/);
    assert.throws(() => sealManifest([{ name: 'n', content: 42 }]), /content/);
  });
});

describe('最小披露：副本不暴露被遮盖条目', () => {
  test('序列化副本中不出现被遮盖条目的名称、内容或盐', () => {
    // 独立封存：每个条目的名称与内容字符串全局唯一，杜绝跨条目巧合匹配
    const uniqueEntries = [
      { name: 'ALPHA-名称', content: 'ALPHA-唯一内容-9f3a' },
      { name: 'BRAVO-名称', content: 'BRAVO-唯一内容-1c7d' },
      { name: 'CHARLIE-名称', content: 'CHARLIE-唯一内容-5e2b' },
      { name: 'DELTA-名称', content: 'DELTA-唯一内容-8a4f' },
    ];
    const seal = sealManifest(uniqueEntries);
    const approved = [0, 2]; // 1、3 被遮盖
    const bundle = createDisclosure(seal.sealedPackage, approved);
    const jsonText = serializeBundle(bundle);
    const parsed = parseBundle(jsonText);

    // 字段级检查：副本只包含批准位置
    assert.deepEqual(parsed.entries.map((e) => e.position), approved);
    for (const hiddenIndex of [1, 3]) {
      assert.ok(!jsonText.includes(uniqueEntries[hiddenIndex].name),
        `被遮盖条目 ${uniqueEntries[hiddenIndex].name} 的名称泄露`);
      assert.ok(!jsonText.includes(uniqueEntries[hiddenIndex].content),
        `被遮盖条目 ${uniqueEntries[hiddenIndex].name} 的内容泄露`);
      assert.ok(!jsonText.includes(seal.sealedPackage.entries[hiddenIndex].salt.toString('base64')),
        `位置 ${hiddenIndex} 的盐泄露`);
    }
    // 批准条目本身的字段完整保留
    assert.deepEqual(parsed.entries.map((e) => e.name), [uniqueEntries[0].name, uniqueEntries[2].name]);
    assert.deepEqual(parsed.entries.map((e) => e.content), [uniqueEntries[0].content, uniqueEntries[2].content]);
    // 证明只含 level/side/32 字节摘要，不含兄弟条目的名称/内容/盐等明文
    for (const entry of bundle.entries) {
      for (const step of entry.proof) {
        assert.equal(step.digest.length, 32);
        assert.deepEqual(Object.keys(step).sort(), ['digest', 'level', 'side']);
      }
    }
  });

  test('披露副本不携带可信根，复核端必须使用独立来源', () => {
    const { sealedPackage } = sealSample();
    const bundle = createDisclosure(sealedPackage, [0]);
    assert.equal(bundle.root, undefined);
    const jsonText = serializeBundle(bundle);
    assert.ok(!jsonText.includes('root'));
    assert.ok(!jsonText.includes('sealedAt'));
  });

  test('编解码往返后结构与字节一致', () => {
    const { sealedRecord, sealedPackage } = sealSample();
    const bundle = createDisclosure(sealedPackage, [0, 3, 4]);
    const roundTripBundle = parseBundle(serializeBundle(bundle));
    const roundTripRecord = parseRecord(serializeRecord(sealedRecord));
    assert.ok(roundTripRecord.root.equals(sealedRecord.root));
    assert.deepEqual(
      roundTripBundle.entries.map((e) => e.position),
      bundle.entries.map((e) => e.position),
    );
    for (const [i, entry] of bundle.entries.entries()) {
      assert.ok(roundTripBundle.entries[i].salt.equals(entry.salt));
      assert.ok(roundTripBundle.entries[i].digest.equals(entry.digest));
      assert.equal(roundTripBundle.entries[i].name, entry.name);
    }
    assert.equal(verifyDisclosure(roundTripBundle, roundTripRecord, [0, 3, 4]).ok, true);
  });
});
