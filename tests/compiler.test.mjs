import { test } from "node:test";
import assert from "node:assert/strict";

import { DeferredCompiler, HandlerCompiler, makeResult } from "../src/compiler.mjs";

const request = (shape = [4, 4]) => ({
  modelVersion: "v1",
  shape,
  layout: "row-major",
  dtype: "float32",
});

test("DeferredCompiler records calls and resolves them in order", async () => {
  const compiler = new DeferredCompiler();
  const p1 = compiler.compile(request([1, 1]));
  const p2 = compiler.compile(request([2, 2]));

  assert.equal(compiler.callCount, 2);
  assert.equal(compiler.unresolved().length, 2);

  compiler.resolveNext(makeResult(compiler.calls[0], { plan: { name: "p1" }, occupiedBytes: 10 }));
  const r1 = await p1;
  assert.deepEqual(r1.plan, { name: "p1" });
  assert.equal(r1.occupiedBytes, 10);

  const boom = new Error("boom");
  compiler.rejectNext(boom);
  await assert.rejects(p2, /boom/);
  assert.equal(compiler.unresolved().length, 0);
});

test("DeferredCompiler.resolveNext on an empty queue throws", () => {
  const compiler = new DeferredCompiler();
  assert.throws(() => compiler.resolveNext({}), /no pending compilation/);
  assert.throws(() => compiler.rejectNext(new Error()), /no pending compilation/);
});

test("HandlerCompiler invokes the handler per request", async () => {
  const compiler = new HandlerCompiler((req) =>
    makeResult(req, { plan: { shape: [...req.shape] }, occupiedBytes: req.shape[0] * 4 }),
  );
  const result = await compiler.compile(request([3, 3]));
  assert.equal(compiler.callCount, 1);
  assert.deepEqual(result.plan, { shape: [3, 3] });
  assert.equal(result.occupiedBytes, 12);
});

test("HandlerCompiler requires a function", () => {
  assert.throws(() => new HandlerCompiler(null), TypeError);
});

test("makeResult defaults to an exact-shape guard and a unique plan", () => {
  const req = request([5, 5]);
  const a = makeResult(req);
  const b = makeResult(req);
  assert.notEqual(a.plan, b.plan);
  assert.deepEqual(a.guard.dims, [
    { min: 5, max: 5 },
    { min: 5, max: 5 },
  ]);
  assert.equal(a.guard.layout, "row-major");
  assert.equal(a.guard.dtype, "float32");
});
