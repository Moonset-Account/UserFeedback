import { AdapterRegistry } from './src/index.mjs';

/**
 * End-to-end demonstration of hot-swapping an adapter version underneath
 * live scoring sessions.
 *
 * The weight scheme makes mixing obvious at a glance:
 *   version v, block b, index i  ->  v * 100 + b * 10 + i
 * so a score built purely from v1 blocks versus v2 blocks differs by ~100
 * per unit of input, and a score that mixed the two would land between the
 * pure values with blocks reporting different leading digits.
 */

const DIM = 2;
const INPUT = [1, 1];

function weightsFor(version, blockId) {
  return Array.from({ length: DIM }, (_, i) => version * 100 + blockId * 10 + i);
}

function load(registry, version, blocks) {
  const attempt = registry.beginLoad({ version, blocks, slotCount: blocks.length });
  for (const blockId of blocks) attempt.deliver(blockId, weightsFor(version, blockId));
  return attempt;
}

const registry = new AdapterRegistry({ slots: 8 });

console.log('== publish v1 (blocks 0,1) ==');
load(registry, 1, [0, 1]);
console.log('published version:', registry.publishedVersion);
console.log('slots used/free   :', registry.pool.used, '/', registry.pool.free);

console.log('\n== session A opens on v1 and scores ==');
const sessionA = registry.openSession();
const aBefore = sessionA.score(INPUT);
console.log(`session ${aBefore.sessionId} version ${aBefore.version} score ${aBefore.score}`);
console.log('  per-block weights:', aBefore.blocks.map((b) => `[${b.blockId}] ${b.weights.join(',')}`).join('  '));

console.log('\n== start loading v2 (blocks 0,1); sessions keep serving v1 ==');
const attempt = registry.beginLoad({ version: 2, blocks: [0, 1], slotCount: 2 });
console.log('published version during load:', registry.publishedVersion);
const midLoad = registry.openSession();
console.log(`session ${midLoad.id} opened mid-load pins version`, midLoad.version);
attempt.deliver(0, weightsFor(2, 0));
attempt.deliver(1, weightsFor(2, 1));
console.log('published version after load :', registry.publishedVersion);
console.log('retired (still pinned)       :', registry.retiredVersions);

console.log('\n== session B opens after the swap and scores the same input ==');
const sessionB = registry.openSession();
const bScore = sessionB.score(INPUT);
console.log(`session ${bScore.sessionId} version ${bScore.version} score ${bScore.score}`);
console.log('  per-block weights:', bScore.blocks.map((b) => `[${b.blockId}] ${b.weights.join(',')}`).join('  '));

console.log('\n== session A scores the same input again, after the publish ==');
const aAfter = sessionA.score(INPUT);
console.log(`session ${aAfter.sessionId} version ${aAfter.version} score ${aAfter.score}`);
console.log('  per-block weights:', aAfter.blocks.map((b) => `[${b.blockId}] ${b.weights.join(',')}`).join('  '));

const mixedExample = Math.round((aAfter.score + bScore.score) / 2);
console.log('\n== how to read the results ==');
console.log(`pure v1 score ${aBefore.score} / ${aAfter.score}, pure v2 score ${bScore.score}`);
console.log(`a mixed generation would read something like ${mixedExample} — not observed.`);
console.log(
  `A stayed on v1 across the publish: ${aBefore.score === aAfter.score && aAfter.version === 1}`,
);
console.log(`B opened on v2: ${bScore.version === 2 && bScore.score !== aAfter.score}`);
console.log(
  'every block in A used v1 weights:',
  aAfter.blocks.every((b) => b.weights[0] === 100 + b.blockId * 10),
);
console.log(
  'every block in B used v2 weights:',
  bScore.blocks.every((b) => b.weights[0] === 200 + b.blockId * 10),
);

console.log('\n== reclaim retired v1 once its last pinned session ends ==');
console.log('slots used before ending A/B:', registry.pool.used);
sessionA.end();
sessionB.end();
midLoad.end();
console.log('retired after all sessions ended:', registry.retiredVersions);
console.log('slots used/free now:', registry.pool.used, '/', registry.pool.free);
console.log('\npublish log:', registry.publishLog().map((p) => `v${p.version}#g${p.generation}`).join(', '));
console.log('event trace :', registry.events.map((e) => e.type).join(' -> '));
