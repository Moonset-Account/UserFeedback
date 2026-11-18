import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createAdmissionGateway, AdmissionError, CODES } from '../src/admission.js';

/* ---------- 可替换解码器夹具 ---------- */

/** 直通解码器：普通表单用，输出即输入。 */
function identityDecoder() {
  return {
    update: (chunk) => chunk,
    finalize: () => null,
  };
}

/** 展开解码器：模拟解压/转码，输出是输入的 factor 倍，finalize 再吐出 tail。 */
function expandingDecoder({ factor = 2, tail = Buffer.alloc(0) } = {}) {
  const state = { destroyed: false };
  return {
    update(chunk) {
      const buf = Buffer.from(chunk);
      const out = Buffer.alloc(buf.length * factor);
      for (let i = 0; i < factor; i += 1) buf.copy(out, i * buf.length);
      return out;
    },
    finalize: () => tail,
    destroy() {
      state.destroyed = true;
    },
    get destroyed() {
      return state.destroyed;
    },
  };
}

function makeGateway(overrides = {}) {
  return createAdmissionGateway({
    routes: {
      form: {
        maxUploadBytes: 64,
        maxDecodedBytes: 64,
        decoder: identityDecoder,
        ...overrides.form,
      },
      doc: {
        maxUploadBytes: 16,
        maxDecodedBytes: 32,
        decoder: () => expandingDecoder({ factor: 2 }),
        ...overrides.doc,
      },
    },
  });
}

/* ---------- 可接受结果：共用入口 + 待定区不可见 + 提交后可见 ---------- */

test('普通表单与文档上传共用入口，读完并过限额前文档不可见', async () => {
  const gw = makeGateway();

  // 普通表单走同一入口。
  const form = gw.admit('form', 'form-1');
  form.write('name=ada&');
  form.write('role=admin');
  const formResult = await form.end();
  assert.equal(formResult.uploadBytes, 19);
  assert.equal(gw.getVisible('form-1').body.toString(), 'name=ada&role=admin');

  // 文档上传：分块流入待定区，end 之前不可见。
  const doc = gw.admit('doc', 'doc-1');
  doc.write(Buffer.alloc(8, 0x61)); // 上传 8B，解码出 16B
  assert.equal(gw.isPending('doc-1'), true);
  assert.equal(gw.getVisible('doc-1'), undefined, '未 commit 前不得可见');
  doc.write(Buffer.alloc(8, 0x62)); // 上传合计 16B，解码合计 32B，恰好顶到限额
  const docResult = await doc.end();
  assert.deepEqual(
    { uploadBytes: docResult.uploadBytes, decodedBytes: docResult.decodedBytes },
    { uploadBytes: 16, decodedBytes: 32 },
  );
  assert.equal(gw.isPending('doc-1'), false);
  const visible = gw.getVisible('doc-1');
  assert.equal(visible.body.length, 32);
  assert.equal(visible.body.subarray(0, 8).toString(), 'aaaaaaaa');
});

/* ---------- 应被阻止：上传字节超限 ---------- */

test('上传字节超过路由限额即拒绝并清理待定状态', () => {
  const gw = makeGateway();
  const s = gw.admit('doc', 'doc-overflow');
  s.write(Buffer.alloc(16)); // 顶到上传限额，仍合法
  assert.throws(
    () => s.write(Buffer.alloc(1)),
    (err) => err instanceof AdmissionError && err.code === CODES.UPLOAD_LIMIT,
  );
  assert.equal(gw.isPending('doc-overflow'), false, '超限后待定状态必须被清理');
  assert.equal(gw.getVisible('doc-overflow'), undefined);
});

/* ---------- 应被阻止：解码输出晚些才超限（finalize 阶段） ---------- */

test('解码输出在 finalize 时才超限：拒绝、清理待定区并销毁解码器', async () => {
  const decoder = expandingDecoder({ factor: 2, tail: Buffer.alloc(1) });
  const gw = createAdmissionGateway({
    routes: {
      doc: { maxUploadBytes: 16, maxDecodedBytes: 32, decoder: () => decoder },
    },
  });
  const s = gw.admit('doc', 'doc-late');
  s.write(Buffer.alloc(16)); // 解码出 32B，仍在限额内
  await assert.rejects(
    () => s.end(), // finalize 再吐 1B，解码输出 33B > 32B
    (err) => err instanceof AdmissionError && err.code === CODES.DECODED_LIMIT,
  );
  assert.equal(gw.isPending('doc-late'), false);
  assert.equal(gw.getVisible('doc-late'), undefined);
  assert.equal(decoder.destroyed, true, '失败清理必须销毁解码器释放资源');
});

/* ---------- 边界：恰好等于限额可接受，多一字节即拒绝 ---------- */

test('限额边界：等于限额通过，超出一字节被拒', async () => {
  const gw = makeGateway();

  const ok = gw.admit('form', 'edge-ok');
  ok.write('x'.repeat(64)); // 恰好 64B
  await assert.doesNotReject(() => ok.end());
  assert.equal(gw.getVisible('edge-ok').body.length, 64);

  const bad = gw.admit('form', 'edge-bad');
  bad.write('x'.repeat(64));
  assert.throws(() => bad.write('y'), (err) => err.code === CODES.UPLOAD_LIMIT);
});

/* ---------- 交错：客户端取消后同一业务请求可重试 ---------- */

test('客户端取消清理待定状态，同一 requestId 可重试成功', async () => {
  const gw = makeGateway();

  const first = gw.admit('doc', 'retry-1');
  first.write(Buffer.alloc(8, 0x61));
  first.abort('client cancelled');
  assert.equal(gw.isPending('retry-1'), false);
  assert.equal(gw.getVisible('retry-1'), undefined);
  assert.throws(() => first.write(Buffer.alloc(1)), (err) => err.code === CODES.SESSION_CLOSED);

  const second = gw.admit('doc', 'retry-1'); // 同一业务请求重试
  second.write(Buffer.alloc(16, 0x63));
  await second.end();
  assert.equal(gw.getVisible('retry-1').body.length, 32);
});

/* ---------- 交错：下游写入失败清理待定状态，重试互不影响 ---------- */

test('下游写入失败清理待定状态；失败请求可重试，并行请求预算不受影响', async () => {
  let failOnce = true;
  const sink = {
    async write() {
      if (failOnce) {
        failOnce = false;
        throw new Error('disk full');
      }
    },
  };
  const gw = createAdmissionGateway({
    routes: {
      doc: {
        maxUploadBytes: 16,
        maxDecodedBytes: 32,
        decoder: () => expandingDecoder({ factor: 2 }),
        sink,
      },
    },
  });

  // 并行请求 B 先占用自己的预算。
  const b = gw.admit('doc', 'req-B');
  b.write(Buffer.alloc(8, 0x62)); // B 已用上传 8B / 解码 16B

  // 请求 A 走完流程但下游写失败。
  const a1 = gw.admit('doc', 'req-A');
  a1.write(Buffer.alloc(16, 0x61));
  await assert.rejects(
    () => a1.end(),
    (err) => err.code === CODES.DOWNSTREAM_FAILURE && err.cause?.message === 'disk full',
  );
  assert.equal(gw.isPending('req-A'), false, '下游失败后待定状态必须清理');
  assert.equal(gw.getVisible('req-A'), undefined);

  // B 的预算不受 A 失败影响，继续写满自己的限额并提交。
  b.write(Buffer.alloc(8, 0x62));
  await assert.doesNotReject(() => b.end());
  assert.equal(gw.getVisible('req-B').body.length, 32);

  // A 用同一 requestId 重试，预算从零重新计。
  const a2 = gw.admit('doc', 'req-A');
  a2.write(Buffer.alloc(16, 0x61));
  await a2.end();
  assert.equal(gw.getVisible('req-A').body.length, 32);
});

/* ---------- 在途冲突与未知路由 ---------- */

test('同一 requestId 在途时拒绝重复准入；未知路由直接拒绝', () => {
  const gw = makeGateway();
  gw.admit('form', 'dup-1');
  assert.throws(() => gw.admit('form', 'dup-1'), (err) => err.code === CODES.DUPLICATE_REQUEST);
  assert.throws(() => gw.admit('nope', 'x'), (err) => err.code === CODES.UNKNOWN_ROUTE);
});
