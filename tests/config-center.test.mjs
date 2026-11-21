import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { ConfigCenter, ConfigReferenceError, ref, shadow } from '../src/config-center.mjs';

function setup() {
  const cc = new ConfigCenter();
  cc.registerEnvironment('test');
  cc.registerEnvironment('prod');
  cc.registerInstance('i-test-1', 'test');
  cc.registerInstance('i-test-2', 'test');
  cc.registerInstance('i-prod-1', 'prod');
  return cc;
}

describe('the scenario from the task: base endpoint references host, test env only covers host', () => {
  test('inherited reference resolves against the requesting instance effective config', () => {
    const cc = setup();
    const r = cc.publish([
      {
        layer: 'base',
        set: { host: 'base.internal', endpoint: ref('host') },
      },
      {
        layer: { env: 'test' },
        set: { host: 'test.internal' },
      },
    ]);
    assert.equal(r.ok, true);
    assert.equal(cc.version, 1);

    // base endpoint -> $ref host; test env overrides host only.
    assert.equal(cc.resolve('i-test-1', 'endpoint'), 'test.internal');
    assert.deepEqual(cc.getEffectiveConfig('i-test-1'), {
      host: 'test.internal',
      endpoint: 'test.internal',
    });
    // prod has no host override, so it resolves against the base value.
    assert.equal(cc.resolve('i-prod-1', 'endpoint'), 'base.internal');
    // The merged (pre-resolution) view still shows the inherited reference.
    assert.deepEqual(cc.getMergedConfig('i-test-1').endpoint, ref('host'));
  });

  test('an instance-level override of the referenced target wins over the env value', () => {
    const cc = setup();
    cc.publish([
      { layer: 'base', set: { host: 'base.internal', endpoint: ref('host') } },
      { layer: { env: 'test' }, set: { host: 'test.internal' } },
      { layer: { instance: 'i-test-1' }, set: { host: '127.0.0.1' } },
    ]);
    assert.equal(cc.resolve('i-test-1', 'endpoint'), '127.0.0.1');
    assert.equal(cc.resolve('i-test-2', 'endpoint'), 'test.internal');
  });
});

describe('normal layering: base < environment < instance', () => {
  test('string values propagate down and are overridden layer by layer', () => {
    const cc = setup();
    cc.publish([
      { layer: 'base', set: { a: 'base-a', b: 'base-b' } },
      { layer: { env: 'test' }, set: { b: 'env-b', c: 'env-c' } },
      { layer: { instance: 'i-test-1' }, set: { c: 'inst-c' } },
    ]);
    assert.deepEqual(cc.getEffectiveConfig('i-test-1'), {
      a: 'base-a',
      b: 'env-b',
      c: 'inst-c',
    });
    assert.deepEqual(cc.getEffectiveConfig('i-test-2'), {
      a: 'base-a',
      b: 'env-b',
      c: 'env-c',
    });
  });

  test('reference chains are followed until a literal is reached', () => {
    const cc = setup();
    cc.publish([
      {
        layer: 'base',
        set: {
          literal: 'final',
          middle: ref('literal'),
          top: ref('middle'),
        },
      },
    ]);
    assert.equal(cc.resolve('i-test-1', 'top'), 'final');
    assert.equal(cc.getEffectiveConfig('i-test-1').top, 'final');
  });
});

describe('deleting an override re-exposes the parent layer', () => {
  test('env override removed by delete falls back to the base value', () => {
    const cc = setup();
    cc.publish([
      { layer: 'base', set: { host: 'base.internal' } },
      { layer: { env: 'test' }, set: { host: 'test.internal' } },
    ]);
    assert.equal(cc.resolve('i-test-1', 'host'), 'test.internal');

    const r = cc.publish([{ layer: { env: 'test' }, delete: ['host'] }]);
    assert.equal(r.ok, true);
    assert.equal(cc.resolve('i-test-1', 'host'), 'base.internal');
  });

  test('instance override deleted re-exposes the env value, which still shadows base', () => {
    const cc = setup();
    cc.publish([
      { layer: 'base', set: { host: 'base.internal' } },
      { layer: { env: 'test' }, set: { host: 'test.internal' } },
      { layer: { instance: 'i-test-1' }, set: { host: 'inst.internal' } },
    ]);
    cc.publish([{ layer: { instance: 'i-test-1' }, delete: ['host'] }]);
    assert.equal(cc.resolve('i-test-1', 'host'), 'test.internal');
  });

  test('deleting a key that only exists on the layer is a no-op and does not touch the parent', () => {
    const cc = setup();
    cc.publish([
      { layer: 'base', set: { host: 'base.internal' } },
      { layer: { env: 'test' }, set: { onlyHere: 'x' } },
    ]);
    const r = cc.publish([{ layer: { env: 'test' }, delete: ['onlyHere', 'host'] }]);
    assert.equal(r.ok, true);
    assert.deepEqual(r.committed[0].deletedKeys, ['onlyHere']);
    assert.equal(cc.resolve('i-test-1', 'host'), 'base.internal');
  });
});

describe('explicit shadowing makes a key missing here and in inheriting lower layers', () => {
  test('a shadow tombstone removes an inherited key', () => {
    const cc = setup();
    cc.publish([
      { layer: 'base', set: { host: 'base.internal', keep: 'yes' } },
      { layer: { env: 'test' }, set: { host: shadow() } },
    ]);
    assert.equal('host' in cc.getEffectiveConfig('i-test-1'), false);
    assert.equal(cc.getEffectiveConfig('i-test-1').keep, 'yes');
  });

  test('a lower layer can re-introduce a shadowed key', () => {
    const cc = setup();
    cc.publish([
      { layer: 'base', set: { host: 'base.internal' } },
      { layer: { env: 'test' }, set: { host: shadow() } },
      { layer: { instance: 'i-test-1' }, set: { host: 'reborn.internal' } },
    ]);
    assert.equal(cc.resolve('i-test-1', 'host'), 'reborn.internal');
    // The sibling instance still sees the shadow.
    assert.equal('host' in cc.getEffectiveConfig('i-test-2'), false);
  });

  test('a reference landing on a shadowed key is dangling', () => {
    const cc = setup();
    cc.publish([{ layer: 'base', set: { host: 'base.internal' } }]);
    const r = cc.publish([
      {
        layer: { env: 'test' },
        set: { host: shadow(), endpoint: ref('host') },
      },
    ]);
    assert.equal(r.ok, false);
    assert.equal(r.errors.length, 2); // i-test-1 and i-test-2 both see it
    for (const err of r.errors) {
      assert.ok(err instanceof ConfigReferenceError);
      assert.equal(err.kind, 'dangling');
      assert.equal(err.originKey, 'endpoint');
      assert.deepEqual(err.chain, ['endpoint', 'host']);
      assert.ok(['i-test-1', 'i-test-2'].includes(err.instanceId));
    }
    // Rejected publish changes nothing.
    assert.equal(cc.getEffectiveConfig('i-test-1').host, 'base.internal');
  });
});

describe('atomic multi-layer publishes with pre-commit validation', () => {
  test('one publish may change base, env and instance together', () => {
    const cc = setup();
    const r = cc.publish([
      { layer: 'base', set: { host: 'base.internal', endpoint: ref('host') } },
      { layer: { env: 'test' }, set: { host: 'test.internal' } },
      { layer: { instance: 'i-test-1' }, set: { note: 'instance-only' } },
    ]);
    assert.equal(r.ok, true);
    assert.equal(cc.version, 1);
    assert.equal(r.committed.length, 3);
    assert.equal(cc.resolve('i-test-1', 'endpoint'), 'test.internal');
    assert.equal(cc.getEffectiveConfig('i-test-1').note, 'instance-only');
  });

  test('a dangling reference in any registered instance rejects the whole publish', () => {
    const cc = setup();
    cc.publish([{ layer: 'base', set: { host: 'base.internal' } }]);
    const before = cc.getEffectiveConfig('i-prod-1');

    const r = cc.publish([
      // base change that is fine on its own...
      { layer: 'base', set: { extra: 'value' } },
      // ...but the test env introduces a reference to a key nobody defines.
      { layer: { env: 'test' }, set: { endpoint: ref('missing-port') } },
    ]);

    assert.equal(r.ok, false);
    assert.equal(cc.version, 1); // unchanged by the rejected publish
    const err = r.errors[0];
    assert.equal(err.kind, 'dangling');
    assert.equal(err.instanceId, 'i-test-1');
    assert.deepEqual(err.chain, ['endpoint', 'missing-port']);
    // Neither layer change is visible.
    assert.deepEqual(cc.getEffectiveConfig('i-test-1'), { host: 'base.internal' });
    assert.deepEqual(cc.getEffectiveConfig('i-prod-1'), before);
  });

  test('all registered instances are checked: another instance failing blocks the publish', () => {
    const cc = setup();
    const r = cc.publish([
      {
        layer: { instance: 'i-prod-1' },
        set: { solo: ref('nowhere') },
      },
    ]);
    assert.equal(r.ok, false);
    assert.equal(r.errors[0].instanceId, 'i-prod-1');
    assert.equal(cc.version, 0);
    assert.deepEqual(cc.getMergedConfig('i-prod-1'), {});
  });

  test('a reference cycle is rejected with the concrete instance and full chain', () => {
    const cc = setup();
    const r = cc.publish([
      {
        layer: 'base',
        set: { a: ref('b'), b: ref('c'), c: ref('a') },
      },
    ]);
    assert.equal(r.ok, false);
    const err = r.errors[0];
    assert.equal(err.kind, 'cycle');
    assert.equal(err.instanceId, 'i-test-1');
    assert.equal(err.originKey, 'a');
    assert.deepEqual(err.chain, ['a', 'b', 'c', 'a']);
    assert.equal(cc.version, 0);
  });

  test('a self reference is a cycle with a two-key chain', () => {
    const cc = setup();
    const r = cc.publish([{ layer: 'base', set: { self: ref('self') } }]);
    assert.equal(r.ok, false);
    assert.equal(r.errors[0].kind, 'cycle');
    assert.deepEqual(r.errors[0].chain, ['self', 'self']);
  });

  test('a cycle formed only inside one test instance also blocks the publish', () => {
    const cc = setup();
    // Base keys are acyclic and visible to every instance.
    cc.publish([
      { layer: 'base', set: { x: 'literal-x', y: ref('x') } },
    ]);
    // Only i-test-1 gets an override that closes a loop: y -> x -> y.
    const r = cc.publish([
      { layer: { instance: 'i-test-1' }, set: { x: ref('y') } },
    ]);
    assert.equal(r.ok, false);
    // The failure is attributed to the single instance where the cycle exists.
    const instances = r.errors.map((e) => e.instanceId);
    assert.deepEqual([...new Set(instances)], ['i-test-1']);
    assert.ok(
      r.errors.some(
        (e) => e.kind === 'cycle' && e.originKey === 'x' &&
          (e.chain.join('->') === 'x->y->x' || e.chain.join('->') === 'y->x->y')
      )
    );
    // i-test-2 stays healthy and the previous published version remains live.
    assert.equal(cc.resolve('i-test-2', 'y'), 'literal-x');
    assert.equal(cc.version, 1);
    assert.deepEqual(cc.getMergedConfig('i-test-1').x, 'literal-x');
  });

  test('after a rejected publish, a corrected publish commits and bumps the version', () => {
    const cc = setup();
    const bad = cc.publish([
      { layer: 'base', set: { endpoint: ref('port') } },
    ]);
    assert.equal(bad.ok, false);
    assert.equal(cc.version, 0);

    const good = cc.publish([
      { layer: 'base', set: { endpoint: ref('port'), port: '8080' } },
    ]);
    assert.equal(good.ok, true);
    assert.equal(cc.version, 1);
    assert.equal(cc.resolve('i-test-1', 'endpoint'), '8080');
  });
});

describe('boundary cases', () => {
  test('with no registered instances, reference checks pass vacuously', () => {
    const cc = new ConfigCenter();
    cc.registerEnvironment('staging');
    const r = cc.publish([
      { layer: 'base', set: { endpoint: ref('ghost') } },
    ]);
    assert.equal(r.ok, true);
    assert.equal(cc.version, 1);
  });

  test('an instance registered later exposes the pre-existing dangling reference on read', () => {
    const cc = new ConfigCenter();
    cc.registerEnvironment('staging');
    cc.publish([{ layer: 'base', set: { endpoint: ref('ghost') } }]);
    cc.registerInstance('i-staging-1', 'staging');
    assert.throws(
      () => cc.getEffectiveConfig('i-staging-1'),
      (err) => err instanceof ConfigReferenceError && err.kind === 'dangling' &&
        err.instanceId === 'i-staging-1'
    );
  });

  test('resolve on a missing key reports dangling with a single-key chain', () => {
    const cc = setup();
    assert.throws(
      () => cc.resolve('i-test-1', 'nope'),
      (err) => err.kind === 'dangling' && err.originKey === 'nope' &&
        err.chain.length === 1
    );
  });

  test('set and delete touching the same key in one change: set wins', () => {
    const cc = setup();
    const r = cc.publish([
      {
        layer: 'base',
        set: { host: 'base.internal' },
        delete: ['host'],
      },
    ]);
    assert.equal(r.ok, true);
    assert.equal(cc.resolve('i-test-1', 'host'), 'base.internal');
  });

  test('empty publish is valid and still creates a new version', () => {
    const cc = setup();
    const r = cc.publish([]);
    assert.equal(r.ok, true);
    assert.equal(cc.version, 1);
  });

  test('invalid values and unknown scopes are rejected without mutating state', () => {
    const cc = setup();
    assert.throws(() => cc.publish([{ layer: 'base', set: { bad: 42 } }]), TypeError);
    assert.throws(() => cc.publish([{ layer: 'base', set: { bad: { other: 1 } } }]), TypeError);
    assert.throws(() => cc.publish([{ layer: { env: 'nope' }, set: { a: '1' } }]), /unknown environment/);
    assert.throws(() => cc.publish([{ layer: { instance: 'nope' }, set: { a: '1' } }]), /unknown instance/);
    assert.throws(() => ref(''), TypeError);
    assert.equal(cc.version, 0);
  });

  test('duplicate registration and unknown instance reads are rejected', () => {
    const cc = setup();
    assert.throws(() => cc.registerEnvironment('test'), /already registered/);
    assert.throws(() => cc.registerInstance('i-test-1', 'test'), /already registered/);
    assert.throws(() => cc.registerInstance('x', 'ghost-env'), /not registered/);
    assert.throws(() => cc.getEffectiveConfig('ghost'), /unknown instance/);
  });

  test('different environments are isolated: a shadow in test does not affect prod', () => {
    const cc = setup();
    cc.publish([
      { layer: 'base', set: { host: 'base.internal' } },
      { layer: { env: 'test' }, set: { host: shadow() } },
    ]);
    assert.equal('host' in cc.getEffectiveConfig('i-test-1'), false);
    assert.equal(cc.getEffectiveConfig('i-prod-1').host, 'base.internal');
  });
});
