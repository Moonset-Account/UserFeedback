import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createResourceStore,
  OwnershipConflictError,
} from '../src/resource-model.mjs';

function freshStore() {
  return createResourceStore({});
}

test('repeated application of the same declaration is idempotent', () => {
  const store = freshStore();

  const first = store.apply('a', { spec: { replicas: 3, image: 'nginx:1' } });
  assert.equal(first.resource.spec.replicas, 3);
  assert.deepEqual(first.changedPaths, ['spec.image', 'spec.replicas']);
  assert.deepEqual(store.ownersOf('spec.replicas'), ['a']);

  const second = store.apply('a', { spec: { replicas: 3, image: 'nginx:1' } });
  assert.equal(second.resource.spec.replicas, 3);
  assert.deepEqual(second.changedPaths, []);
  assert.deepEqual(store.ownersOf('spec.replicas'), ['a']);
  assert.deepEqual(store.ownedBy('a'), ['spec.image', 'spec.replicas']);
});

test('a declared object is owned leaf by leaf, not as one blob', () => {
  const store = freshStore();
  store.apply('a', { spec: { config: { level: 1 } } });

  assert.deepEqual(store.ownersOf('spec.config.level'), ['a']);
  assert.deepEqual(store.ownersOf('spec.config'), []);
  assert.deepEqual(store.ownedBy('a'), ['spec.config.level']);
});

test('two controllers declaring the same value co-own the field', () => {
  const store = freshStore();
  store.apply('a', { spec: { replicas: 3 } });

  const result = store.apply('b', { spec: { replicas: 3 } });
  assert.equal(result.resource.spec.replicas, 3);
  assert.deepEqual(store.ownersOf('spec.replicas'), ['a', 'b']);
  assert.deepEqual(result.changedPaths, []);
});

test('omitting a co-owned field revokes only the omitting controller share', () => {
  const store = freshStore();
  store.apply('a', { spec: { replicas: 3, image: 'nginx:1' } });
  store.apply('b', { spec: { replicas: 3 } });
  assert.deepEqual(store.ownersOf('spec.replicas'), ['a', 'b']);

  const afterA = store.apply('a', { spec: { image: 'nginx:1' } });
  assert.equal(afterA.resource.spec.replicas, 3, 'field survives while b owns it');
  assert.deepEqual(store.ownersOf('spec.replicas'), ['b']);

  const afterB = store.apply('b', {});
  assert.equal(Object.hasOwn(afterB.resource.spec ?? {}, 'replicas'), false);
  assert.deepEqual(store.ownersOf('spec.replicas'), []);
});

test('the last owner leaving removes the field from the resource', () => {
  const store = freshStore();
  store.apply('a', { spec: { replicas: 3 } });

  store.apply('a', {});
  assert.deepEqual(store.resource, {});
  assert.deepEqual(store.ownedBy('a'), []);
});

test('overwriting another controller different value rejects the whole apply with the conflict path', () => {
  const store = freshStore();
  store.apply('a', { spec: { replicas: 3, image: 'nginx:1' } });

  const before = store.snapshot();
  assert.throws(
    () => store.apply('b', { spec: { replicas: 5 } }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.replicas']);
      assert.equal(error.conflicts.length, 1);
      assert.deepEqual(error.conflicts[0], {
        path: 'spec.replicas',
        reason: 'value',
        currentValue: 3,
        requestedValue: 5,
        owners: ['a'],
      });
      return true;
    },
  );

  assert.deepEqual(store.snapshot(), before, 'resource and owners are untouched');
  assert.deepEqual(store.ownersOf('spec.replicas'), ['a']);
  assert.equal(store.resource.spec.replicas, 3);
});

test('a failed apply with new fields and a conflict rolls back both resource and ownership', () => {
  const store = freshStore();
  store.apply('a', {
    spec: {
      replicas: 3,
      containers: [{ name: 'web', image: 'nginx:1' }],
    },
  });
  const before = store.snapshot();

  assert.throws(
    () => store.apply('b', {
      spec: {
        replicas: 5,
        createdAt: 'controller-b',
        containers: [
          { name: 'web', image: 'nginx:1' },
          { name: 'db', image: 'postgres:16' },
        ],
      },
    }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.replicas']);
      return true;
    },
  );

  const after = store.snapshot();
  assert.deepEqual(after, before, 'resource and full owner table unchanged');
  assert.equal(Object.hasOwn(store.resource.spec, 'createdAt'), false);
  assert.deepEqual(store.ownersOf('spec.createdAt'), []);
  assert.deepEqual(store.ownersOf('spec.containers[name=db].image'), []);
  assert.deepEqual(store.ownedBy('b'), []);
});

test('a conflict is reported even when the conflicting field is nested in a container list', () => {
  const store = freshStore();
  store.apply('a', {
    spec: { containers: [{ name: 'web', image: 'nginx:1' }] },
  });
  const before = store.snapshot();

  assert.throws(
    () => store.apply('b', {
      spec: {
        containers: [
          { name: 'web', image: 'nginx:2' },
          { name: 'db', image: 'postgres:16' },
        ],
      },
    }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.containers[name=web].image']);
      assert.deepEqual(error.conflicts[0].owners, ['a']);
      assert.equal(error.conflicts[0].currentValue, 'nginx:1');
      assert.equal(error.conflicts[0].requestedValue, 'nginx:2');
      return true;
    },
  );

  assert.deepEqual(store.snapshot(), before);
});

test('explicit force transfers the field to the applicant', () => {
  const store = freshStore();
  store.apply('a', { spec: { replicas: 3 } });

  const forced = store.apply('b', { spec: { replicas: 5 } }, { force: true });
  assert.equal(forced.resource.spec.replicas, 5);
  assert.deepEqual(store.ownersOf('spec.replicas'), ['b']);

  // The previous owner can no longer write a different value without forcing.
  assert.throws(
    () => store.apply('a', { spec: { replicas: 6 } }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.replicas']);
      assert.deepEqual(error.conflicts[0].owners, ['b']);
      return true;
    },
  );

  // A's omission cannot remove B's field.
  store.apply('a', {});
  assert.equal(store.resource.spec.replicas, 5);
});

test('force can be scoped to a single conflict path', () => {
  const store = freshStore();
  store.apply('a', { spec: { replicas: 3, mode: 'stable' } });

  // Scoping force to replicas does not excuse the unforced mode conflict:
  // the whole apply is still rejected atomically.
  const before = store.snapshot();
  assert.throws(
    () => store.apply('b', { spec: { replicas: 5, mode: 'fast' } }, { force: ['spec.replicas'] }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.mode']);
      return true;
    },
  );
  assert.deepEqual(store.snapshot(), before);

  store.apply('b', { spec: { replicas: 5, mode: 'stable' } }, { force: ['spec.replicas'] });
  assert.equal(store.resource.spec.replicas, 5);
  assert.equal(store.resource.spec.mode, 'stable', 'agreeing field stays co-owned');
  assert.deepEqual(store.ownersOf('spec.replicas'), ['b']);
  assert.deepEqual(store.ownersOf('spec.mode'), ['a', 'b']);
});

test('reordering a container list never transfers ownership between controllers', () => {
  const store = freshStore();
  store.apply('a', {
    spec: {
      containers: [
        { name: 'web', image: 'nginx:1' },
        { name: 'side', image: 'envoy:1' },
      ],
    },
  });

  // b only reorders the existing elements by name; it declares no fields.
  store.apply('b', {
    spec: { containers: [{ name: 'side' }, { name: 'web' }] },
  });

  assert.deepEqual(store.ownedBy('b'), []);
  assert.deepEqual(store.ownersOf('spec.containers[name=side].image'), ['a']);
  assert.deepEqual(store.ownersOf('spec.containers[name=web].image'), ['a']);
  assert.equal(
    store.resource.spec.containers.find((entry) => entry.name === 'side').image,
    'envoy:1',
  );

  // If reordering had moved ownership, b could now overwrite 'side'.
  assert.throws(
    () => store.apply('b', {
      spec: { containers: [{ name: 'side', image: 'envoy:2' }] },
    }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.containers[name=side].image']);
      return true;
    },
  );
});

test('list identity is the element name, so the same values co-own after a reorder', () => {
  const store = freshStore();
  store.apply('a', {
    spec: {
      containers: [
        { name: 'web', image: 'nginx:1' },
        { name: 'side', image: 'envoy:1' },
      ],
    },
  });

  const result = store.apply('b', {
    spec: {
      containers: [
        { name: 'side', image: 'envoy:1' },
        { name: 'web', image: 'nginx:1' },
      ],
    },
  });

  assert.deepEqual(result.changedPaths, [], 'same values at the same element names changed nothing');
  assert.deepEqual(store.ownersOf('spec.containers[name=side].image'), ['a', 'b']);
  assert.deepEqual(store.ownersOf('spec.containers[name=web].image'), ['a', 'b']);

  // a is no longer the sole owner: its omission must not delete either field.
  store.apply('a', { spec: { containers: [] } });
  assert.deepEqual(
    store.resource.spec.containers.map((entry) => entry.name).sort(),
    ['side', 'web'],
  );
  assert.equal(store.resource.spec.containers.find((entry) => entry.name === 'web').image, 'nginx:1');
  assert.deepEqual(store.ownersOf('spec.containers[name=web].image'), ['b']);
});

test('list elements can be released independently and disappear when unowned', () => {
  const store = freshStore();
  store.apply('a', {
    spec: {
      containers: [
        { name: 'web', image: 'nginx:1' },
        { name: 'side', image: 'envoy:1' },
      ],
    },
  });
  store.apply('b', {
    spec: { containers: [{ name: 'side', image: 'envoy:1' }] },
  });

  // a drops both elements; 'side' is co-owned and must stay.
  store.apply('a', { spec: { containers: [] } });
  assert.deepEqual(
    store.resource.spec.containers.map((entry) => entry.name),
    ['side'],
  );
  assert.deepEqual(store.ownersOf('spec.containers[name=side].image'), ['b']);
  assert.equal(store.resource.spec.containers[0].image, 'envoy:1');

  // b was the last owner, so the element and the now-empty list disappear.
  store.apply('b', {});
  assert.deepEqual(store.resource, {});
});

test('array values without usable names are treated as single leaf fields', () => {
  const store = freshStore();
  store.apply('a', { spec: { args: ['--one'] } });
  assert.deepEqual(store.ownersOf('spec.args'), ['a']);

  store.apply('b', { spec: { args: ['--one'] } });
  assert.deepEqual(store.ownersOf('spec.args'), ['a', 'b']);

  assert.throws(
    () => store.apply('b', { spec: { args: ['--two'] } }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.args']);
      return true;
    },
  );

  store.apply('b', { spec: { args: ['--two'] } }, { force: true });
  assert.deepEqual(store.ownersOf('spec.args'), ['b']);
  assert.deepEqual(store.resource.spec.args, ['--two']);

  // Arrays with duplicate names are not container lists either.
  const dup = freshStore();
  dup.apply('a', { spec: { items: [{ name: 'x', v: 1 }, { name: 'x', v: 2 }] } });
  assert.deepEqual(dup.ownedBy('a'), ['spec.items']);
});

test('replacing an object with a scalar conflicts on the owning descendants path unless forced', () => {
  const store = freshStore();
  store.apply('a', { spec: { config: { level: 1 } } });
  const before = store.snapshot();

  assert.throws(
    () => store.apply('b', { spec: { config: 9, extra: true } }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.config']);
      assert.equal(error.conflicts[0].reason, 'shape');
      return true;
    },
  );
  assert.deepEqual(store.snapshot(), before);
  assert.deepEqual(store.ownersOf('spec.extra'), []);

  store.apply('b', { spec: { config: 9, extra: true } }, { force: true });
  assert.deepEqual(store.resource.spec, { config: 9, extra: true });
  assert.deepEqual(store.ownersOf('spec.config'), ['b']);
  assert.deepEqual(store.ownersOf('spec.config.level'), []);
});

test('replacing a leaf with a nested object conflicts on the ancestor path unless forced', () => {
  const store = freshStore();
  store.apply('a', { spec: { config: 5 } });
  const before = store.snapshot();

  assert.throws(
    () => store.apply('b', { spec: { config: { level: 1 } } }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.config']);
      assert.equal(error.conflicts[0].reason, 'shape');
      assert.deepEqual(error.conflicts[0].owners, ['a']);
      return true;
    },
  );
  assert.deepEqual(store.snapshot(), before);

  store.apply('b', { spec: { config: { level: 1 } } }, { force: 'spec.config' });
  assert.deepEqual(store.resource.spec.config, { level: 1 });
  assert.deepEqual(store.ownersOf('spec.config'), [], 'the replaced leaf claim is gone');
  assert.deepEqual(store.ownersOf('spec.config.level'), ['b']);
});

test('callers cannot mutate the store through returned resources or declarations', () => {
  const store = freshStore();
  const declaration = { spec: { replicas: 3 } };
  const result = store.apply('a', declaration);

  declaration.spec.replicas = 99;
  result.resource.spec.replicas = 42;
  store.snapshot().resource.spec.replicas = 77;

  assert.equal(store.resource.spec.replicas, 3);
  assert.deepEqual(store.ownersOf('spec.replicas'), ['a']);
});

test('invalid arguments are rejected before any state change', () => {
  const store = freshStore();
  store.apply('a', { spec: { replicas: 3 } });
  const before = store.snapshot();

  assert.throws(() => store.apply('', { spec: {} }), TypeError);
  assert.throws(() => store.apply('b', null), TypeError);
  assert.throws(() => store.apply('b', { spec: {} }, { force: 7 }), TypeError);

  assert.deepEqual(store.snapshot(), before);
});

test('a co-owner may not unilaterally change the shared value', () => {
  const store = freshStore();
  store.apply('a', { spec: { replicas: 3 } });
  store.apply('b', { spec: { replicas: 3 } });
  const before = store.snapshot();

  assert.throws(
    () => store.apply('a', { spec: { replicas: 4 } }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.replicas']);
      assert.deepEqual(error.conflicts[0].owners, ['b'], 'the co-owner is named, not the applicant');
      return true;
    },
  );
  assert.deepEqual(store.snapshot(), before);
});

test('an unowned pre-existing value may be claimed, then treated as owned', () => {
  const store = createResourceStore({ spec: { replicas: 3 } });

  store.apply('a', { spec: { replicas: 3 } });
  assert.deepEqual(store.ownersOf('spec.replicas'), ['a']);

  assert.throws(
    () => store.apply('b', { spec: { replicas: 4 } }),
    (error) => {
      assert.deepEqual(error.paths, ['spec.replicas']);
      return true;
    },
  );
});

test('forced transfer survives the former co-owner omitting the field', () => {
  const store = freshStore();
  store.apply('a', { spec: { replicas: 3 } });
  store.apply('b', { spec: { replicas: 3 } });
  assert.deepEqual(store.ownersOf('spec.replicas'), ['a', 'b']);

  store.apply('b', { spec: { replicas: 5 } }, { force: 'spec.replicas' });
  assert.deepEqual(store.ownersOf('spec.replicas'), ['b']);

  store.apply('a', {});
  assert.equal(store.resource.spec.replicas, 5);
  assert.deepEqual(store.ownersOf('spec.replicas'), ['b']);
});

test('force does not reach unrelated sibling paths', () => {
  const store = freshStore();
  store.apply('a', { spec: { rolling: { batch: 1 }, mode: 'stable' } });
  const before = store.snapshot();

  assert.throws(
    () => store.apply('b', { spec: { rolling: { batch: 2 }, mode: 'fast' } }, { force: 'spec.rolling' }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.mode']);
      return true;
    },
  );
  assert.deepEqual(store.snapshot(), before);

  const result = store.apply(
    'b',
    { spec: { rolling: { batch: 2 }, mode: 'stable' } },
    { force: 'spec.rolling' },
  );
  assert.deepEqual(result.resource.spec.rolling, { batch: 2 });
  assert.deepEqual(store.ownersOf('spec.rolling.batch'), ['b']);
  assert.deepEqual(store.ownersOf('spec.mode'), ['a', 'b']);
});

test('interleaved controllers: reorder, new element, release and conflict in one session', () => {
  const store = freshStore();
  store.apply('a', {
    spec: {
      replicas: 3,
      template: {
        containers: [
          { name: 'web', image: 'nginx:1', ports: [{ name: 'http', port: 80 }] },
        ],
      },
    },
  });
  store.apply('b', {
    spec: {
      replicas: 3,
      template: {
        containers: [
          { name: 'web', image: 'nginx:1' },
          { name: 'side', image: 'envoy:1' },
        ],
      },
    },
  });

  assert.deepEqual(store.ownedBy('a'), [
    'spec.replicas',
    'spec.template.containers[name=web].image',
    'spec.template.containers[name=web].ports[name=http].port',
  ]);
  assert.deepEqual(store.ownedBy('b'), [
    'spec.replicas',
    'spec.template.containers[name=side].image',
    'spec.template.containers[name=web].image',
  ], 'b agreed with a on the web image and replicas, so it co-owns those leaves');

  // a reorders the list, re-affirms replicas, drops its image share and adds a
  // new element-level field. All of that is legal because nothing it writes is
  // owned elsewhere.
  store.apply('a', {
    spec: {
      replicas: 3,
      template: {
        containers: [
          { name: 'side' },
          { name: 'web', resources: { cpu: '100m' } },
        ],
      },
    },
  });

  assert.deepEqual(store.ownersOf('spec.template.containers[name=web].image'), ['b']);
  assert.equal(
    store.resource.spec.template.containers.find((entry) => entry.name === 'web').image,
    'nginx:1',
    'the image survives a owner departure',
  );
  assert.deepEqual(store.ownersOf('spec.template.containers[name=web].resources.cpu'), ['a']);
  assert.deepEqual(store.ownersOf('spec.template.containers[name=side].image'), ['b']);
  assert.equal(store.resource.spec.replicas, 3);

  const beforeFailure = store.snapshot();
  assert.throws(
    () => store.apply('a', {
      spec: {
        replicas: 9,
        template: {
          containers: [
            { name: 'side', image: 'envoy:2' },
            { name: 'web', image: 'nginx:1', resources: { cpu: '200m' } },
          ],
        },
      },
    }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, [
        'spec.replicas',
        'spec.template.containers[name=side].image',
      ]);
      return true;
    },
  );
  assert.deepEqual(store.snapshot(), beforeFailure, 'multi-conflict apply changed nothing');
  assert.deepEqual(store.ownersOf('spec.template.containers[name=web].resources.cpu'), ['a']);
  assert.equal(
    store.resource.spec.template.containers.find((entry) => entry.name === 'web').resources.cpu,
    '100m',
  );
});

test('nested list elements are addressed by every name on the path', () => {
  const store = freshStore();
  store.apply('a', {
    spec: { containers: [{ name: 'web', env: [{ name: 'LOG', value: 'info' }] }] },
  });

  assert.deepEqual(store.ownedBy('a'), ['spec.containers[name=web].env[name=LOG].value']);
  assert.deepEqual(store.ownersOf('spec.containers[name=web].env'), []);
});

test('names containing path punctuation stay distinct', () => {
  const store = freshStore();
  store.apply('a', { spec: { containers: [{ name: 'a.b', image: 'x' }] } });
  store.apply('b', { spec: { containers: [{ name: 'a', image: 'y' }] } });

  assert.deepEqual(store.ownersOf('spec.containers[name=a].image'), ['b']);
  assert.deepEqual(store.ownersOf('spec.containers[name=a.b].image'), ['a']);

  // b already owns [name=a], so changing it alone succeeds.
  store.apply('b', { spec: { containers: [{ name: 'a', image: 'z' }] } });
  assert.equal(
    store.resource.spec.containers.find((entry) => entry.name === 'a').image,
    'z',
  );

  // Forcing [name=a] must not reach [name=a.b]; the element name is a whole
  // path segment, not a raw string prefix.
  const before = store.snapshot();
  assert.throws(
    () => store.apply('b', {
      spec: {
        containers: [
          { name: 'a', image: 'z' },
          { name: 'a.b', image: 'hijacked' },
        ],
      },
    }, { force: 'spec.containers[name=a]' }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.containers[name=a.b].image']);
      return true;
    },
  );
  assert.deepEqual(store.snapshot(), before);
});

test('empty declarations are no-ops and never materialize structure', () => {
  const store = createResourceStore({ spec: { replicas: 3 } });

  const result = store.apply('a', { spec: {}, metadata: {}, items: [] });
  assert.deepEqual(result.resource, { spec: { replicas: 3 } });
  assert.deepEqual(result.changedPaths, []);
  assert.deepEqual(store.ownedBy('a'), [], 'empty declarations claim nothing');

  // An empty subtree cannot erase a value that is present but unowned.
  store.apply('a', { spec: {} });
  assert.equal(store.resource.spec.replicas, 3);
});

test('removing a leaf prunes empty parents but preserves siblings owned elsewhere', () => {
  const store = freshStore();
  store.apply('a', { spec: { config: { token: 't' }, other: { level: 1 } } });
  store.apply('b', { spec: { config: { level: 2 } } });

  store.apply('a', { spec: { other: { level: 1 } } });
  assert.deepEqual(store.resource.spec.config, { level: 2 });
  assert.deepEqual(store.ownersOf('spec.config.token'), []);

  store.apply('b', { spec: { other: { level: 1 } } });
  assert.deepEqual(store.resource.spec.other, { level: 1 });
  assert.deepEqual(store.ownersOf('spec.other.level'), ['a', 'b']);
});

test('a sole owner may change a field value, shape or type without forcing', () => {
  const store = freshStore();
  store.apply('a', { spec: { config: { level: 1 }, replicas: 3 } });

  // same leaf, new value
  store.apply('a', { spec: { config: { level: 2 }, replicas: 3 } });
  assert.equal(store.resource.spec.config.level, 2);

  // object -> scalar: the applicant's own descendants are released
  store.apply('a', { spec: { config: 9, replicas: 3 } });
  assert.equal(store.resource.spec.config, 9);
  assert.deepEqual(store.ownersOf('spec.config.level'), []);
  assert.deepEqual(store.ownersOf('spec.config'), ['a']);

  // scalar -> object again
  store.apply('a', { spec: { config: { level: 3 }, replicas: 3 } });
  assert.deepEqual(store.resource.spec.config, { level: 3 });
  assert.deepEqual(store.ownersOf('spec.config'), []);
  assert.deepEqual(store.ownersOf('spec.config.level'), ['a']);
});

test('a sole owner may switch an object into a named container list and back', () => {
  const store = freshStore();
  store.apply('a', { spec: { box: { size: 1 } } });

  store.apply('a', { spec: { box: [{ name: 'main', size: 2 }] } });
  assert.deepEqual(store.resource.spec.box, [{ name: 'main', size: 2 }]);
  assert.deepEqual(store.ownersOf('spec.box.size'), []);
  assert.deepEqual(store.ownersOf('spec.box[name=main].size'), ['a']);

  store.apply('a', { spec: { box: { size: 3 } } });
  assert.deepEqual(store.resource.spec.box, { size: 3 });
  assert.deepEqual(store.ownersOf('spec.box[name=main].size'), []);
  assert.deepEqual(store.ownersOf('spec.box.size'), ['a']);
});

test('list values that are owned as a leaf are reported on the leaf path', () => {
  const store = freshStore();
  store.apply('a', { spec: { free: [1, 2] } });
  const before = store.snapshot();

  assert.throws(
    () => store.apply('b', { spec: { free: [{ name: 'x', v: 1 }] } }),
    (error) => {
      assert.ok(error instanceof OwnershipConflictError);
      assert.deepEqual(error.paths, ['spec.free']);
      assert.equal(error.conflicts[0].reason, 'shape');
      return true;
    },
  );
  assert.deepEqual(store.snapshot(), before);

  store.apply('b', { spec: { free: [{ name: 'x', v: 1 }] } }, { force: true });
  assert.deepEqual(store.resource.spec.free, [{ name: 'x', v: 1 }]);
  assert.deepEqual(store.ownersOf('spec.free'), []);
  assert.deepEqual(store.ownersOf('spec.free[name=x].v'), ['b']);
});

test('list element removal only drops the element once its last field owner leaves', () => {
  const store = freshStore();
  store.apply('a', { spec: { containers: [{ name: 'web', image: 'nginx:1', port: 80 }] } });
  store.apply('b', { spec: { containers: [{ name: 'web', port: 80 }] } });

  // a drops everything it declared for the element; port is co-owned.
  store.apply('a', { spec: { containers: [] } });
  assert.deepEqual(store.resource.spec.containers, [{ name: 'web', port: 80 }]);
  assert.deepEqual(store.ownersOf('spec.containers[name=web].port'), ['b']);

  // b drops port too; the element has no fields left and disappears.
  store.apply('b', { spec: {} });
  assert.deepEqual(store.resource, {});
});
