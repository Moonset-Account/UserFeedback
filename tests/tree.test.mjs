import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  leafDigest,
  nodeDigest,
  generateSalt,
  SALT_BYTES,
  DIGEST_BYTES,
} from '../src/digest.js';
import {
  buildLevels,
  computeRoot,
  buildProof,
  verifyProof,
  treeDepth,
} from '../src/tree.js';

function sampleLeaves(count) {
  return Array.from({ length: count }, (_, index) =>
    leafDigest({
      position: index,
      salt: generateSalt(),
      name: `evidence-${index}`,
      content: `payload-${index}`,
    }),
  );
}

describe('叶子承诺原语', () => {
  test('同输入产生确定性摘要，且为固定 32 字节', () => {
    const salt = generateSalt();
    const a = leafDigest({ position: 2, salt, name: 'n', content: 'c' });
    const b = leafDigest({ position: 2, salt: Buffer.from(salt), name: 'n', content: 'c' });
    assert.equal(a.length, DIGEST_BYTES);
    assert.ok(a.equals(b));
  });

  test('盐长度必须为 16 字节', () => {
    assert.throws(() => leafDigest({
      position: 0, salt: Buffer.alloc(15), name: 'n', content: 'c',
    }), /16 字节/);
  });

  test('位置、盐、名称、内容任一变化都会改变叶子承诺', () => {
    const salt = generateSalt();
    const base = leafDigest({ position: 1, salt, name: 'name', content: 'body' });
    assert.ok(!base.equals(leafDigest({ position: 2, salt, name: 'name', content: 'body' })));
    assert.ok(!base.equals(leafDigest({ position: 1, salt: generateSalt(), name: 'name', content: 'body' })));
    assert.ok(!base.equals(leafDigest({ position: 1, salt, name: 'other', content: 'body' })));
    assert.ok(!base.equals(leafDigest({ position: 1, salt, name: 'name', content: 'changed' })));
  });

  test('长度前缀编码防止字段拼接歧义', () => {
    const salt = generateSalt();
    const splitA = leafDigest({ position: 0, salt, name: 'ab', content: 'c' });
    const splitB = leafDigest({ position: 0, salt, name: 'a', content: 'bc' });
    assert.ok(!splitA.equals(splitB));
  });

  test('相同内容在不同位置产生不同承诺（位置绑定）', () => {
    const saltA = generateSalt();
    const saltB = generateSalt();
    // 即使盐也相同，位置不同承诺也不同
    const atOne = leafDigest({ position: 1, salt: saltA, name: 'n', content: 'same' });
    const atTwo = leafDigest({ position: 2, salt: saltA, name: 'n', content: 'same' });
    assert.ok(!atOne.equals(atTwo));
    assert.notEqual(saltA.length, 0);
    assert.equal(saltB.length, SALT_BYTES);
  });
});

describe('有序摘要树', () => {
  test('深度计算', () => {
    assert.equal(treeDepth(1), 0);
    assert.equal(treeDepth(2), 1);
    assert.equal(treeDepth(3), 2);
    assert.equal(treeDepth(4), 2);
    assert.equal(treeDepth(5), 3);
  });

  test('奇数叶子向上提升，根可独立复算', () => {
    const leaves = sampleLeaves(3);
    const levels = buildLevels(leaves);
    assert.equal(levels[0].length, 3);
    assert.equal(levels[1].length, 2);
    assert.equal(levels[2].length, 1);
    const expectedLeft = nodeDigest(leaves[0], leaves[1]);
    const expectedPromoted = nodeDigest(leaves[2], leaves[2]);
    assert.ok(levels[1][0].equals(expectedLeft));
    assert.ok(levels[1][1].equals(expectedPromoted));
    assert.ok(levels[2][0].equals(nodeDigest(expectedLeft, expectedPromoted)));
    assert.ok(computeRoot(leaves).equals(levels[2][0]));
  });

  test('1 至 7 个叶子的全部位置包含证明均通过', () => {
    for (let count = 1; count <= 7; count += 1) {
      const leaves = sampleLeaves(count);
      const levels = buildLevels(leaves);
      const root = levels.at(-1)[0];
      for (let position = 0; position < count; position += 1) {
        const result = verifyProof({
          leaf: leaves[position],
          position,
          proof: buildProof(levels, position),
          expectedRoot: root,
          leafCount: count,
        });
        assert.equal(result.ok, true, `count=${count} position=${position} 应通过`);
      }
    }
  });

  test('单叶子树证明为空数组并通过', () => {
    const leaves = sampleLeaves(1);
    const levels = buildLevels(leaves);
    const proof = buildProof(levels, 0);
    assert.deepEqual(proof, []);
    assert.equal(verifyProof({
      leaf: leaves[0], position: 0, proof, expectedRoot: leaves[0], leafCount: 1,
    }).ok, true);
  });

  test('兄弟摘要被篡改导致根不一致', () => {
    // 2 叶子树只有一个证明步骤，不存在提升，篡改兄弟必然走到根比对
    const leaves = sampleLeaves(2);
    const levels = buildLevels(leaves);
    const proof = buildProof(levels, 0);
    const tampered = structuredClone(proof);
    tampered[0].digest = Buffer.alloc(32, 0x7e);
    const result = verifyProof({
      leaf: leaves[0], position: 0, proof: tampered,
      expectedRoot: levels.at(-1)[0], leafCount: 2,
    });
    assert.equal(result.ok, false);
    assert.equal(result.code, 'ROOT_MISMATCH');
  });

  test('证明方向、层级被伪造时拒绝', () => {
    const leaves = sampleLeaves(4);
    const levels = buildLevels(leaves);
    const root = levels.at(-1)[0];
    const proof = buildProof(levels, 0);

    const wrongSide = structuredClone(proof);
    wrongSide[0].side = 'left';
    assert.equal(verifyProof({
      leaf: leaves[0], position: 0, proof: wrongSide, expectedRoot: root, leafCount: 4,
    }).code, 'PROOF_MALFORMED');

    const wrongLevel = structuredClone(proof);
    wrongLevel[1].level = 0;
    assert.equal(verifyProof({
      leaf: leaves[0], position: 0, proof: wrongLevel, expectedRoot: root, leafCount: 4,
    }).code, 'PROOF_MALFORMED');
  });

  test('证明被截断或含多余步骤时拒绝', () => {
    const leaves = sampleLeaves(4);
    const levels = buildLevels(leaves);
    const root = levels.at(-1)[0];
    const proof = buildProof(levels, 0);

    assert.equal(verifyProof({
      leaf: leaves[0], position: 0, proof: proof.slice(0, 1), expectedRoot: root, leafCount: 4,
    }).code, 'PROOF_MALFORMED');

    assert.equal(verifyProof({
      leaf: leaves[0], position: 0,
      proof: [...proof, { level: 9, side: 'left', digest: Buffer.alloc(DIGEST_BYTES) }],
      expectedRoot: root, leafCount: 4,
    }).code, 'PROOF_MALFORMED');
  });

  test('位置越界被拒绝', () => {
    const leaves = sampleLeaves(3);
    const levels = buildLevels(leaves);
    assert.throws(() => buildProof(levels, 3), /超出清单范围/);
    assert.equal(verifyProof({
      leaf: leaves[0], position: 3, proof: [], expectedRoot: levels.at(-1)[0], leafCount: 3,
    }).code, 'POSITION_OUT_OF_RANGE');
  });

  test('提升层（奇数叶子的最后一个节点）证明可正确验证', () => {
    const leaves = sampleLeaves(5);
    const levels = buildLevels(leaves);
    const root = levels.at(-1)[0];
    // 位置 4 在第 0 层提升，其证明只包含第 1 层之后的步骤
    const proof = buildProof(levels, 4);
    assert.ok(proof.every((step) => step.level >= 1));
    assert.equal(verifyProof({
      leaf: leaves[4], position: 4, proof, expectedRoot: root, leafCount: 5,
    }).ok, true);
  });
});
