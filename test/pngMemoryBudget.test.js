'use strict';

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  buildBufferPlan,
  validateMetadata,
  adam7PassDimensions,
  PngMemoryBudget,
  STAGES,
} from '../src/pngMemoryBudget.js';
import { RecordingAllocator } from '../src/recordingAllocator.js';

const here = dirname(fileURLToPath(import.meta.url));
const fixture = (name) => JSON.parse(readFileSync(join(here, 'fixtures', name), 'utf8'));

const MB = 1024 * 1024;

/**
 * 按 plan 的阶段定义模拟一次完整解码：
 * 每个缓冲在“首次出现”的阶段分配，在“最后出现”的阶段结束时释放，
 * 从而精确复现各阶段同时存活缓冲的峰值。
 */
function simulateFullDecode(budget, taskId, plan) {
  const live = new Map();
  const lastStage = new Map();
  for (const stage of plan.stages) {
    for (const b of stage.buffers) lastStage.set(b.label, stage.stage);
  }
  for (const stage of plan.stages) {
    for (const b of stage.buffers) {
      if (!live.has(b.label)) {
        const res = budget.allocate({ taskId, stage: stage.stage, label: b.label, bytes: b.bytes });
        assert.equal(res.decision, 'allow', `阶段 ${stage.stage} 分配 ${b.label} 应通过：${res.reason ?? ''}`);
        live.set(b.label, res.handle);
      }
    }
    for (const [label, handle] of [...live.entries()]) {
      if (lastStage.get(label) === stage.stage) {
        budget.free(handle);
        live.delete(label);
      }
    }
  }
  assert.equal(live.size, 0, '完整解码结束后应无存活句柄（输出阶段之后）');
}

describe('元数据校验', () => {
  test('合法小图元数据通过校验', () => {
    const check = validateMetadata(fixture('small_rgba.json'));
    assert.equal(check.ok, true);
    assert.equal(check.channels, 4);
    assert.equal(check.bitsPerPixel, 32);
  });

  test('位深与颜色类型非法组合返回 invalid_fields', () => {
    const check = validateMetadata(fixture('invalid_bitdepth.json'));
    assert.equal(check.ok, false);
    assert.equal(check.reason, 'invalid_fields');
    assert.ok(check.problems.some((p) => p.includes('bitDepth')));
  });

  test('缺少字段返回 missing_fields，非对象输入返回 metadata_not_object', () => {
    assert.equal(validateMetadata(fixture('missing_fields.json')).reason, 'missing_fields');
    assert.equal(validateMetadata(null).reason, 'metadata_not_object');
    assert.equal(validateMetadata(fixture('bad_stage.json')).reason, 'invalid_fields');
  });
});

describe('基于行缓冲与并存缓冲的预算推导', () => {
  test('非交错 RGBA 8-bit 16x16：行缓冲与各阶段并存缓冲计算正确', () => {
    const plan = buildBufferPlan(fixture('small_rgba.json'));
    // 1 字节 filter + 16*4 像素
    assert.equal(plan.rowBytesPacked, 1 + 16 * 4);
    assert.equal(plan.rowBytesExpanded, 1 + 16 * 4);
    assert.equal(plan.imagePackedBytes, 16 * (1 + 64));
    assert.equal(plan.imageExpandedBytes, 16 * (1 + 64));
    // scanline/unfilter 是最宽阶段：inflate + packed_image + 两行缓冲
    const scanline = plan.stages.find((s) => s.stage === 'scanline');
    assert.equal(scanline.buffers.length, 4);
    const expectedPeak =
      plan.inflateBytes + plan.imagePackedBytes + plan.rowBytesPacked + plan.rowBytesExpanded;
    assert.equal(plan.peakBytes, expectedPeak);
  });

  test('子字节像素（1-bit 调色板）：打包行宽按 ceil(宽*bpp/8)，展开行宽按字节对齐', () => {
    const plan = buildBufferPlan(fixture('small_palette.json'));
    // 32 个 1-bit 像素 = 4 字节 + 1 字节 filter
    assert.equal(plan.rowBytesPacked, 5);
    // 展开为每像素 1 字节：32 + 1
    assert.equal(plan.rowBytesExpanded, 33);
    assert.ok(plan.rowBytesExpanded > plan.rowBytesPacked);
  });

  test('Adam7 交错：七趟子帧尺寸符合规范，行缓冲以最宽趟（最后一趟为全宽）为准', () => {
    const plan = buildBufferPlan(fixture('adam7_rgb.json'));
    assert.equal(plan.interlaced, true);

    const dims = adam7PassDimensions(64, 48);
    // 规范 §8.6：第 7 趟覆盖剩余全部像素（64x24），第 2 趟为 8x6
    assert.deepEqual(
      dims.map((d) => [d.width, d.height]),
      [
        [8, 6],
        [8, 6],
        [16, 6],
        [16, 12],
        [32, 12],
        [32, 24],
        [64, 24],
      ],
    );
    // 最宽趟是第 7 趟（全宽 64）：RGB 8-bit => 1 + 64*3
    assert.equal(plan.passRowBytesPacked, 1 + 64 * 3);
    assert.equal(plan.passRowBytesPacked, plan.rowBytesPacked);
    // 按趟拼接（每趟各自带 filter 字节）会略大于整图估算
    const nonInterlacedPacked = 48 * (1 + 64 * 3);
    assert.ok(plan.imagePackedBytes > nonInterlacedPacked);
    assert.equal(plan.imagePackedBytes, 9306);
  });

  test('小尺寸 Adam7 会出现空趟（宽或高为 0），空趟不产生行缓冲', () => {
    const dims = adam7PassDimensions(3, 3);
    const empty = dims.filter((d) => d.width === 0 || d.height === 0);
    assert.ok(empty.length >= 1);
    const plan = buildBufferPlan({
      width: 3,
      height: 3,
      bitDepth: 8,
      colorType: 6,
      interlaceMethod: 1,
      stage: 'header',
    });
    assert.ok(plan.passRowBytesPacked > 0);
    assert.ok(Number.isSafeInteger(plan.peakBytes));
  });

  test('压缩文件大小不参与预算：两份相同 IHDR 元数据的峰值完全一致', () => {
    const a = buildBufferPlan({ ...fixture('small_rgba.json'), compressedBytes: 1 });
    const b = buildBufferPlan({ ...fixture('small_rgba.json'), compressedBytes: 99_000_000 });
    assert.equal(a.peakBytes, b.peakBytes);
  });

  test('元数据不足时 buildBufferPlan 抛出带 INSUFFICIENT_METADATA 错误码的异常', () => {
    assert.throws(
      () => buildBufferPlan(fixture('missing_fields.json')),
      (err) => err.code === 'INSUFFICIENT_METADATA' && err.reason === 'missing_fields',
    );
  });
});

describe('分配前判定：允许 / 拒绝 / 元数据不足', () => {
  test('合法小图：准入通过且完整解码模拟的全部分配成功', () => {
    const budget = new PngMemoryBudget({ globalBudgetBytes: 64 * MB, perImageBudgetBytes: 16 * MB });
    const metadata = fixture('small_rgba.json');
    const admit = budget.admit({ taskId: 'small-ok', metadata });
    assert.equal(admit.decision, 'allow');
    simulateFullDecode(budget, 'small-ok', admit.plan);
    budget.release('small-ok');
    assert.equal(budget.stats().globalUsedBytes, 0);
  });

  test('元数据不足：返回 insufficient_metadata 且不占用任何预算、不建立任务', () => {
    const budget = new PngMemoryBudget({ globalBudgetBytes: 64 * MB, perImageBudgetBytes: 16 * MB });
    for (const name of ['missing_fields.json', 'invalid_bitdepth.json', 'bad_stage.json']) {
      const res = budget.admit({ taskId: `bad-${name}`, metadata: fixture(name) });
      assert.equal(res.decision, 'insufficient_metadata', name);
    }
    assert.equal(budget.stats().activeTasks, 0);
    assert.equal(budget.stats().globalUsedBytes, 0);
  });

  test('解压炸弹：按单图上限拒绝，reason 明确指向 per_image 并给出超出字节数', () => {
    const budget = new PngMemoryBudget({ globalBudgetBytes: 256 * MB, perImageBudgetBytes: 256 * MB });
    const res = budget.admit({ taskId: 'bomb', metadata: fixture('decompression_bomb.json') });
    assert.equal(res.decision, 'deny');
    assert.equal(res.reason, 'per_image_limit_exceeded');
    assert.equal(res.limit, 'per_image');
    assert.ok(res.overBytes > 0);
    // 峰值约 1.49GB，而全局预算 256MB —— 说明单图与全局是两条独立约束，
    // 该任务在准入阶段就被单图层拦截，从未进入分配器。
    assert.ok(res.peakBytes > budget.globalBudgetBytes);
    assert.equal(budget.stats().activeTasks, 0);
    assert.equal(budget.stats().globalUsedBytes, 0);
  });
});

describe('记录式分配器：恰好达限通过、超限拒绝且现场不变', () => {
  test('恰好等于容量上限的分配通过，再多 1 字节被拒', () => {
    const allocator = new RecordingAllocator({ capacityBytes: 1024 });
    const first = allocator.allocate({ taskId: 't', stage: 'inflate', label: 'a', bytes: 1024 });
    assert.equal(first.ok, true);
    assert.equal(allocator.usedBytes, 1024);
    assert.equal(allocator.peakBytes, 1024);

    const over = allocator.allocate({ taskId: 't', stage: 'inflate', label: 'b', bytes: 1 });
    assert.equal(over.ok, false);
    assert.equal(over.reason, 'capacity_exceeded');
    // 拒绝不改变已用量
    assert.equal(allocator.usedBytes, 1024);
    // 拒绝也留在审计记录里
    assert.ok(allocator.records.some((r) => r.event === 'allocate' && r.decision === 'deny'));

    allocator.free(first.handle);
    // 释放后预算恢复，同等分配可以再次成功
    const again = allocator.allocate({ taskId: 't', stage: 'inflate', label: 'c', bytes: 1024 });
    assert.equal(again.ok, true);
  });

  test('非法尺寸被拒且不产生句柄', () => {
    const allocator = new RecordingAllocator({ capacityBytes: 1024 });
    assert.equal(allocator.allocate({ taskId: 't', stage: 'output', label: 'x', bytes: 0 }).ok, false);
    assert.equal(allocator.allocate({ taskId: 't', stage: 'output', label: 'x', bytes: -1 }).ok, false);
    assert.equal(allocator.usedBytes, 0);
    assert.equal(allocator.free({ id: 'nope' }).ok, false);
  });
});

describe('单图上限与并发总预算的双层约束', () => {
  test('单图：恰好达到单图预算的完整解码通过，存活缓冲达峰后多 1 字节被拒并标明 per_image', () => {
    const plan = buildBufferPlan(fixture('small_rgba.json'));
    const budget = new PngMemoryBudget({
      globalBudgetBytes: 10 * plan.peakBytes,
      perImageBudgetBytes: plan.peakBytes,
    });
    const admit = budget.admit({ taskId: 'exact-single', metadata: fixture('small_rgba.json') });
    assert.equal(admit.decision, 'allow');

    // 完整解码过程中的每次分配都必须落在单图预留内
    simulateFullDecode(budget, 'exact-single', plan);
    assert.equal(budget.stats().globalUsedBytes, 0);

    // 重新进入 scanline 阶段并持有全部并存缓冲，恰好打满单图峰值
    const handles = [];
    for (const b of plan.stages.find((s) => s.stage === 'scanline').buffers) {
      const r = budget.allocate({ taskId: 'exact-single', stage: 'scanline', label: `retain-${b.label}`, bytes: b.bytes });
      assert.equal(r.decision, 'allow', r.reason ?? '');
      handles.push(r.handle);
    }
    const taskSummary = budget.stats().taskSummaries.find((t) => t.taskId === 'exact-single');
    assert.equal(taskSummary.usedBytes, plan.peakBytes);
    assert.equal(taskSummary.remainingBytes, 0);

    // 计划外再多申请 1 字节：单图预留首先拦截（全局预算其实非常充裕）
    const over = budget.allocate({ taskId: 'exact-single', stage: 'output', label: 'extra', bytes: 1 });
    assert.equal(over.decision, 'deny');
    assert.equal(over.limit, 'per_image');
    assert.equal(over.reason, 'per_image_limit_exceeded');
    assert.equal(over.overBytes, 1);
    // 拒绝不产生句柄、不改变已用量
    assert.equal(budget.stats().taskSummaries.find((t) => t.taskId === 'exact-single').usedBytes, plan.peakBytes);

    for (const h of handles) budget.free(h);
    budget.release('exact-single');
  });

  test('单图：准入阶段峰值超限时直接拒绝，无需等到分配', () => {
    const plan = buildBufferPlan(fixture('small_rgba.json'));
    const budget = new PngMemoryBudget({
      globalBudgetBytes: 100 * MB,
      perImageBudgetBytes: plan.peakBytes - 1,
    });
    const res = budget.admit({ taskId: 'tight', metadata: fixture('small_rgba.json') });
    assert.equal(res.decision, 'deny');
    assert.equal(res.limit, 'per_image');
    assert.equal(res.overBytes, 1);
  });

  test('并发：单图均合规但总和触及全局预算，最后一个任务被拒并标明 global', () => {
    const plan = buildBufferPlan(fixture('small_rgba.json'));
    const budget = new PngMemoryBudget({
      globalBudgetBytes: plan.peakBytes * 2,
      perImageBudgetBytes: plan.peakBytes,
    });
    const a = budget.admit({ taskId: 'task-a', metadata: fixture('small_rgba.json') });
    const b = budget.admit({ taskId: 'task-b', metadata: fixture('small_rgba.json') });
    const c = budget.admit({ taskId: 'task-c', metadata: fixture('small_rgba.json') });
    assert.equal(a.decision, 'allow');
    assert.equal(b.decision, 'allow');
    // 准入只受单图约束，第三张同样准入；约束在真实分配时生效
    assert.equal(c.decision, 'allow');

    // A、B 各自分配到其峰值（恰好填满全局预算）
    const inflateA = budget.allocate({ taskId: 'task-a', stage: 'inflate', label: 'inflate_output', bytes: plan.inflateBytes });
    assert.equal(inflateA.decision, 'allow');
    const inflateB = budget.allocate({ taskId: 'task-b', stage: 'inflate', label: 'inflate_output', bytes: plan.inflateBytes });
    assert.equal(inflateB.decision, 'allow');

    // 用最小合法分配把 A、B 各自推到其单图峰值
    const fillToPeak = (taskId, usedSoFar) =>
      budget.allocate({
        taskId,
        stage: 'output',
        label: 'filler',
        bytes: plan.peakBytes - usedSoFar,
      });
    assert.equal(fillToPeak('task-a', plan.inflateBytes).decision, 'allow');
    assert.equal(fillToPeak('task-b', plan.inflateBytes).decision, 'allow');
    assert.equal(budget.stats().globalUsedBytes, plan.peakBytes * 2);

    // 此时 C 的任何分配都必须被全局预算拒绝
    const denied = budget.allocate({ taskId: 'task-c', stage: 'inflate', label: 'inflate_output', bytes: 1 });
    assert.equal(denied.decision, 'deny');
    assert.equal(denied.limit, 'global');
    assert.equal(denied.reason, 'global_budget_exceeded');
    assert.ok(denied.overBytes > 0);
    // 拒绝不改变全局已用量，也不产生句柄
    assert.equal(budget.stats().globalUsedBytes, plan.peakBytes * 2);
  });

  test('拒绝原因可区分：单图峰值合规但全局装不下时返回 global 限制', () => {
    const plan = buildBufferPlan(fixture('small_rgba.json'));
    // 全局预算只够装下 inflate 缓冲：单图峰值本身远大于此，但准入阶段只校验单图上限
    const budget = new PngMemoryBudget({
      globalBudgetBytes: plan.inflateBytes,
      perImageBudgetBytes: plan.peakBytes,
    });
    const admit = budget.admit({ taskId: 'g', metadata: fixture('small_rgba.json') });
    assert.equal(admit.decision, 'allow');

    // 第一次分配恰好填满全局预算，且远未触及单图峰值
    const first = budget.allocate({
      taskId: 'g',
      stage: 'inflate',
      label: 'inflate_output',
      bytes: plan.inflateBytes,
    });
    assert.equal(first.decision, 'allow');
    assert.equal(budget.stats().globalUsedBytes, plan.inflateBytes);

    // 第二次分配本身也在单图预留内，但全局已无空间
    const denied = budget.allocate({
      taskId: 'g',
      stage: 'scanline',
      label: 'row_packed',
      bytes: plan.rowBytesPacked,
    });
    assert.equal(denied.decision, 'deny');
    assert.equal(denied.limit, 'global');
    assert.equal(denied.reason, 'global_budget_exceeded');
    assert.equal(denied.overBytes, plan.rowBytesPacked);
    assert.equal(budget.stats().globalUsedBytes, plan.inflateBytes);
  });
});

describe('并发任务取消与提前失败后的预算归还', () => {
  test('任务取消：release 归还全部存活缓冲，后续任务可复用同一预算', () => {
    const plan = buildBufferPlan(fixture('small_rgba.json'));
    const budget = new PngMemoryBudget({
      globalBudgetBytes: plan.peakBytes,
      perImageBudgetBytes: plan.peakBytes,
    });

    const first = budget.admit({ taskId: 'cancelled', metadata: fixture('small_rgba.json') });
    assert.equal(first.decision, 'allow');
    for (const b of plan.stages.find((s) => s.stage === 'scanline').buffers) {
      assert.equal(
        budget.allocate({ taskId: 'cancelled', stage: 'scanline', label: b.label, bytes: b.bytes }).decision,
        'allow',
      );
    }
    const usedBeforeCancel = budget.stats().globalUsedBytes;
    assert.ok(usedBeforeCancel > 0);

    const released = budget.release('cancelled', 'cancelled_by_client');
    assert.equal(released.decision, 'allow');
    assert.ok(released.freedBytes === usedBeforeCancel);
    assert.ok(released.handleCount >= 1);
    assert.equal(budget.stats().globalUsedBytes, 0);
    assert.equal(budget.stats().activeTasks, 0);

    // 预算全部归还：新任务可以完整跑通，证明没有泄漏
    const second = budget.admit({ taskId: 'replacement', metadata: fixture('small_rgba.json') });
    assert.equal(second.decision, 'allow');
    simulateFullDecode(budget, 'replacement', second.plan);
    assert.equal(budget.stats().globalUsedBytes, 0);
  });

  test('提前失败：unfilter 阶段出错时 release 回收已分配缓冲，并发邻居不受影响', () => {
    const plan = buildBufferPlan(fixture('small_rgba.json'));
    const budget = new PngMemoryBudget({
      globalBudgetBytes: plan.peakBytes * 2,
      perImageBudgetBytes: plan.peakBytes,
    });

    const neighbor = budget.admit({ taskId: 'neighbor', metadata: fixture('small_rgba.json') });
    const doomed = budget.admit({ taskId: 'doomed', metadata: fixture('small_rgba.json') });
    assert.equal(neighbor.decision, 'allow');
    assert.equal(doomed.decision, 'allow');

    // 邻居先占一部分
    const neighborAlloc = budget.allocate({
      taskId: 'neighbor',
      stage: 'inflate',
      label: 'inflate_output',
      bytes: plan.inflateBytes,
    });
    assert.equal(neighborAlloc.decision, 'allow');
    const neighborUsed = budget.stats().globalUsedBytes;

    // 失败任务在 scanline 阶段分配了若干缓冲后报错
    const scanlineBuffers = plan.stages.find((s) => s.stage === 'scanline').buffers;
    const doomedHandles = [];
    for (const b of scanlineBuffers.slice(0, 2)) {
      const r = budget.allocate({ taskId: 'doomed', stage: 'scanline', label: b.label, bytes: b.bytes });
      assert.equal(r.decision, 'allow');
      doomedHandles.push(r.handle);
    }
    const doomedUsed = budget.stats().taskSummaries.find((t) => t.taskId === 'doomed').usedBytes;
    assert.ok(doomedUsed > 0);

    // 提前失败 -> 仅回收该任务
    const cleanup = budget.release('doomed', 'unfilter_error');
    assert.equal(cleanup.freedBytes, doomedUsed);
    assert.equal(cleanup.handleCount, doomedHandles.length);

    const stats = budget.stats();
    // 邻居的预算原封不动
    assert.equal(stats.globalUsedBytes, neighborUsed);
    assert.equal(stats.activeTasks, 1);
    assert.ok(stats.taskSummaries.some((t) => t.taskId === 'neighbor'));
    assert.ok(!stats.taskSummaries.some((t) => t.taskId === 'doomed'));

    // 回收的预算立即可用：邻居可以继续分配
    const continueAlloc = budget.allocate({
      taskId: 'neighbor',
      stage: 'scanline',
      label: 'row_packed',
      bytes: plan.rowBytesPacked,
    });
    assert.equal(continueAlloc.decision, 'allow');
  });

  test('重复释放未知任务被拒且不影响账目', () => {
    const budget = new PngMemoryBudget({ globalBudgetBytes: 1024, perImageBudgetBytes: 1024 });
    const res = budget.release('never-existed');
    assert.equal(res.decision, 'deny');
    assert.equal(res.reason, 'unknown_task');
    assert.equal(res.freedBytes, 0);
  });
});

describe('分配开销与审计记录', () => {
  test('perAllocationOverheadBytes 被计入每次分配与峰值预算', () => {
    const overhead = 16;
    const plan = buildBufferPlan(fixture('small_palette.json'), { perAllocationOverheadBytes: overhead });
    const scanline = plan.stages.find((s) => s.stage === 'scanline');
    assert.equal(scanline.overheadBytes, scanline.buffers.length * overhead);
    assert.equal(scanline.totalBytes, scanline.stageBytes + scanline.overheadBytes);
    assert.ok(plan.peakBytes > buildBufferPlan(fixture('small_palette.json')).peakBytes);

    const budget = new PngMemoryBudget({
      globalBudgetBytes: 16 * MB,
      perImageBudgetBytes: plan.peakBytes,
      perAllocationOverheadBytes: overhead,
    });
    const admit = budget.admit({ taskId: 'oh', metadata: fixture('small_palette.json') });
    assert.equal(admit.decision, 'allow');
    const r = budget.allocate({
      taskId: 'oh',
      stage: 'inflate',
      label: 'inflate_output',
      bytes: plan.inflateBytes,
    });
    assert.equal(r.decision, 'allow');
    assert.equal(r.chargedBytes, plan.inflateBytes + overhead);
  });

  test('审计记录按顺序覆盖每次允许与拒绝，含任务批量归还', () => {
    const plan = buildBufferPlan(fixture('small_rgba.json'));
    const budget = new PngMemoryBudget({
      globalBudgetBytes: plan.peakBytes,
      perImageBudgetBytes: plan.peakBytes,
    });
    budget.admit({ taskId: 'audit', metadata: fixture('small_rgba.json') });
    const ok = budget.allocate({
      taskId: 'audit',
      stage: 'inflate',
      label: 'inflate_output',
      bytes: plan.inflateBytes,
    });
    const deny = budget.allocate({
      taskId: 'audit',
      stage: 'output',
      label: 'too-big',
      bytes: plan.peakBytes,
    });
    assert.equal(ok.decision, 'allow');
    assert.equal(deny.decision, 'deny');

    const release = budget.release('audit', 'test');
    const decisions = budget.stats().decisionRecords;
    const allocatorRecords = budget.stats().allocatorRecords;

    // 管理器决策记录：覆盖准入、单图层拒绝（未到达分配器）与批量归还
    assert.ok(decisions.some((r) => r.decision === 'allow' && r.event === 'allocate'));
    assert.ok(decisions.some((r) => r.decision === 'deny' && r.reason === 'per_image_limit_exceeded'));
    const releaseRecord = decisions.find((r) => r.event === 'release');
    assert.equal(releaseRecord.reason, 'test');
    assert.equal(releaseRecord.freedBytes, release.freedBytes);

    // 底层分配器记录：真实分配与 release_task 同样可审计
    assert.ok(allocatorRecords.some((r) => r.event === 'allocate' && r.decision === 'allow'));
    assert.ok(allocatorRecords.some((r) => r.event === 'release_task' && r.reason === 'test'));

    // 审计记录只追加：seq 严格单调递增
    decisions.forEach((r, i) => i === 0 || assert.ok(r.seq > decisions[i - 1].seq));
  });
});

describe('端到端：合法小图全阶段流水线', () => {
  test('两张不同格式的合法小图均可通过全部分配判定', () => {
    const budget = new PngMemoryBudget({ globalBudgetBytes: 64 * MB, perImageBudgetBytes: 16 * MB });
    for (const [taskId, name] of [['rgba', 'small_rgba.json'], ['palette', 'small_palette.json']]) {
      const metadata = fixture(name);
      const admit = budget.admit({ taskId, metadata });
      assert.equal(admit.decision, 'allow', `${name} 应被允许`);
      assert.ok(admit.peakBytes > 0);
      assert.ok(admit.peakBytes <= 16 * MB);
      simulateFullDecode(budget, taskId, admit.plan);
    }
    // 阶段集合完整且有序
    assert.deepEqual(
      buildBufferPlan(fixture('small_rgba.json')).stages.map((s) => s.stage),
      STAGES,
    );
  });
});
