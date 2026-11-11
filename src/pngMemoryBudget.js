'use strict';

import { RecordingAllocator } from './recordingAllocator.js';

/**
 * PNG 解码内存预算
 *
 * 安全背景：压缩文件大小与解码峰值内存没有线性关系（解压炸弹 / 高宽声明）。
 * 本模块只依据 IHDR 已解析出的元数据（宽、高、位深、颜色类型、交错模式）
 * 与当前解码阶段，按“行缓冲 + 同时存活的其他缓冲”推导峰值预算，
 * 在真正分配之前给出 allow / deny / insufficient_metadata 判定。
 *
 * 参考：PNG 1.2 规范 §4.1（位深/颜色类型）、§8（扫描行过滤）、
 * §8.6（Adam7 交错）、§11.2（图像尺寸与缓冲区上限建议）。
 */

export const COLOR_TYPES = Object.freeze({
  0: { channels: 1, name: 'grayscale' },
  2: { channels: 3, name: 'truecolor' },
  3: { channels: 1, name: 'indexed' }, // 调色板索引每像素 1 个样本
  4: { channels: 2, name: 'grayscale_alpha' },
  6: { channels: 4, name: 'truecolor_alpha' },
});

export const ALLOWED_BIT_DEPTHS = Object.freeze({
  0: [1, 2, 4, 8, 16],
  2: [8, 16],
  3: [1, 2, 4, 8],
  4: [8, 16],
  6: [8, 16],
});

export const INTERLACE_NONE = 0;
export const INTERLACE_ADAM7 = 1;

/** 解码阶段（按真实流水线顺序） */
export const STAGES = Object.freeze(['header', 'inflate', 'scanline', 'unfilter', 'convert', 'output']);

/** Adam7 七趟的起点与步长（规范 §8.6.1） */
const ADAM7_PASSES = Object.freeze([
  { startX: 0, stepX: 8, startY: 0, stepY: 8 },
  { startX: 4, stepX: 8, startY: 0, stepY: 8 },
  { startX: 0, stepX: 4, startY: 4, stepY: 8 },
  { startX: 2, stepX: 4, startY: 0, stepY: 4 },
  { startX: 0, stepX: 2, startY: 2, stepY: 4 },
  { startX: 1, stepX: 2, startY: 0, stepY: 2 },
  { startX: 0, stepX: 1, startY: 1, stepY: 2 },
]);

const passDimension = (length, start, step) => Math.max(0, Math.ceil((length - start) / step));

/**
 * Adam7 七趟各自的子帧宽高（空趟返回 0 宽或 0 高）。
 */
export function adam7PassDimensions(width, height) {
  return ADAM7_PASSES.map((pass, index) => ({
    pass: index + 1,
    width: passDimension(width, pass.startX, pass.stepX),
    height: passDimension(height, pass.startY, pass.stepY),
    startX: pass.startX,
    startY: pass.startY,
    stepX: pass.stepX,
    stepY: pass.stepY,
  }));
}

function ceilDiv8(value) {
  return Math.ceil(value / 8);
}

/** 过滤后的打包扫描行：1 字节 filter-type + 打包像素 */
function packedRowBytes(width, bitsPerPixel) {
  return 1 + ceilDiv8(width * bitsPerPixel);
}

/** 展开后的扫描行：1 字节 filter-type + 每样本按字节对齐（子字节像素扩成 8 位） */
function expandedRowBytes(width, channels, bitDepth) {
  return 1 + width * channels * Math.max(1, bitDepth / 8);
}

/**
 * 校验本地 JSON 中的已解析元数据。
 * 任何缺失/非法字段都返回 insufficient_metadata —— 信息不足时不得放行分配。
 */
export function validateMetadata(metadata) {
  const problems = [];
  if (metadata === null || typeof metadata !== 'object' || Array.isArray(metadata)) {
    return { ok: false, reason: 'metadata_not_object', problems: ['metadata 必须是 JSON 对象'] };
  }

  const required = ['width', 'height', 'bitDepth', 'colorType', 'interlaceMethod', 'stage'];
  for (const key of required) {
    if (!(key in metadata)) problems.push(`缺少字段 ${key}`);
  }
  if (problems.length > 0) return { ok: false, reason: 'missing_fields', problems };

  const { width, height, bitDepth, colorType, interlaceMethod, stage } = metadata;

  if (!Number.isSafeInteger(width) || width <= 0 || width > 0x7fffffff) {
    problems.push('width 必须是 1..2^31-1 的整数');
  }
  if (!Number.isSafeInteger(height) || height <= 0 || height > 0x7fffffff) {
    problems.push('height 必须是 1..2^31-1 的整数');
  }
  if (!(colorType in COLOR_TYPES)) {
    problems.push(`colorType=${String(colorType)} 不合法（允许 0/2/3/4/6）`);
  }
  if (colorType in COLOR_TYPES && !ALLOWED_BIT_DEPTHS[colorType].includes(bitDepth)) {
    problems.push(`bitDepth=${String(bitDepth)} 与 colorType=${String(colorType)} 不匹配`);
  }
  if (interlaceMethod !== INTERLACE_NONE && interlaceMethod !== INTERLACE_ADAM7) {
    problems.push('interlaceMethod 必须是 0（无交错）或 1（Adam7）');
  }
  if (!STAGES.includes(stage)) {
    problems.push(`stage=${String(stage)} 不合法（允许 ${STAGES.join('/')}）`);
  }
  if (problems.length > 0) return { ok: false, reason: 'invalid_fields', problems };

  // 尺寸合法但乘积超出安全整数范围：无法做可靠的整数预算，同样拒绝。
  const channels = COLOR_TYPES[colorType].channels;
  const bitsPerPixel = bitDepth * channels;
  const worstRow = packedRowBytes(width, bitsPerPixel);
  if (!Number.isSafeInteger(worstRow) || !Number.isSafeInteger(worstRow * height)) {
    return { ok: false, reason: 'dimension_overflow', problems: ['宽高乘积超出安全整数范围，无法可靠记账'] };
  }

  return { ok: true, channels, bitsPerPixel };
}

/**
 * 依据元数据构建各阶段的缓冲计划与峰值预算。
 * 每个阶段列出“同时占用内存的全部缓冲”，峰值取各阶段总和的最大值。
 *
 * @throws 元数据不足时抛出 Error（reason 可直接映射为 insufficient_metadata 判定）
 */
export function buildBufferPlan(metadata, { perAllocationOverheadBytes = 0, expandedOutput = true } = {}) {
  const check = validateMetadata(metadata);
  if (!check.ok) {
    const err = new Error(`元数据不足或非法：${check.problems.join('；')}`);
    err.code = 'INSUFFICIENT_METADATA';
    err.reason = check.reason;
    err.problems = check.problems;
    throw err;
  }

  const { width, height, bitDepth, colorType, interlaceMethod } = metadata;
  const { channels, bitsPerPixel } = check;
  const interlaced = interlaceMethod === INTERLACE_ADAM7;

  // —— 非交错的全尺寸行/图像缓冲 ——
  const rowBytesPacked = packedRowBytes(width, bitsPerPixel);
  const rowBytesExpanded = expandedRowBytes(width, channels, bitDepth);
  const imagePackedBytes = height * rowBytesPacked;

  // —— Adam7：按趟计算子帧尺寸，行缓冲取七趟中的最大值 ——
  let passRowBytesPacked = 0;
  let passRowBytesExpanded = 0;
  let interlacedPackedBytes = 0;
  if (interlaced) {
    for (const pass of ADAM7_PASSES) {
      const passWidth = passDimension(width, pass.startX, pass.stepX);
      const passHeight = passDimension(height, pass.startY, pass.stepY);
      if (passWidth === 0 || passHeight === 0) continue;
      const passPacked = packedRowBytes(passWidth, bitsPerPixel);
      passRowBytesPacked = Math.max(passRowBytesPacked, passPacked);
      passRowBytesExpanded = Math.max(
        passRowBytesExpanded,
        expandedRowBytes(passWidth, channels, bitDepth),
      );
      interlacedPackedBytes += passHeight * passPacked;
    }
  }

  // 交错图像若按趟拼接存放，额外的每趟 filter 字节可能使其略大于整图估算，取保守上界。
  const packedTargetBytes = interlaced
    ? Math.max(imagePackedBytes, interlacedPackedBytes)
    : imagePackedBytes;
  const activeRowPacked = interlaced ? passRowBytesPacked : rowBytesPacked;
  const activeRowExpanded = interlaced ? passRowBytesExpanded : rowBytesExpanded;

  const inflateBytes = packedTargetBytes; // zlib 输出缓冲按打包目标尺寸预留
  const imageExpandedBytes = expandedOutput ? height * rowBytesExpanded : 0;

  const withOverhead = (buffers) => {
    const stageBytes = buffers.reduce((sum, b) => sum + b.bytes, 0);
    const overheadBytes = buffers.length * perAllocationOverheadBytes;
    return { buffers, stageBytes, overheadBytes, totalBytes: stageBytes + overheadBytes };
  };

  const stages = [
    { stage: 'header', ...withOverhead([]) },
    {
      stage: 'inflate',
      ...withOverhead([{ label: 'inflate_output', bytes: inflateBytes }]),
    },
    {
      stage: 'scanline',
      ...withOverhead([
        { label: 'inflate_output', bytes: inflateBytes },
        { label: 'packed_image', bytes: packedTargetBytes },
        { label: 'row_packed', bytes: activeRowPacked },
        { label: 'row_expanded', bytes: activeRowExpanded },
      ]),
    },
    {
      stage: 'unfilter',
      ...withOverhead([
        { label: 'inflate_output', bytes: inflateBytes },
        { label: 'packed_image', bytes: packedTargetBytes },
        { label: 'row_packed', bytes: activeRowPacked },
        { label: 'row_expanded', bytes: activeRowExpanded },
      ]),
    },
    {
      stage: 'convert',
      ...withOverhead([
        { label: 'packed_image', bytes: packedTargetBytes },
        ...(imageExpandedBytes > 0 ? [{ label: 'image_expanded', bytes: imageExpandedBytes }] : []),
      ]),
    },
    {
      stage: 'output',
      ...withOverhead(
        imageExpandedBytes > 0 ? [{ label: 'image_expanded', bytes: imageExpandedBytes }] : [],
      ),
    },
  ];

  const peakBytes = Math.max(...stages.map((s) => s.totalBytes));

  return {
    width,
    height,
    bitDepth,
    colorType,
    colorTypeName: COLOR_TYPES[colorType].name,
    interlaceMethod,
    interlaced,
    channels,
    bitsPerPixel,
    rowBytesPacked,
    rowBytesExpanded,
    passRowBytesPacked,
    passRowBytesExpanded,
    inflateBytes,
    imagePackedBytes: packedTargetBytes,
    imageExpandedBytes,
    stages,
    peakBytes,
  };
}

/**
 * 内存预算管理器：双层约束
 *  1) 单图上限 perImageBudgetBytes —— 按元数据推导出的峰值在准入时校验；
 *  2) 并发总预算 globalBudgetBytes —— 由记录式分配器在每次分配时实时记账。
 */
export class PngMemoryBudget {
  /**
   * @param {object} opts
   * @param {number} opts.globalBudgetBytes 并发总预算（字节）
   * @param {number} opts.perImageBudgetBytes 单图峰值上限（字节）
   * @param {number} [opts.perAllocationOverheadBytes] 每次分配的额外记账开销
   * @param {boolean} [opts.expandedOutput] 是否预留像素展开后的全尺寸输出缓冲
   */
  constructor({
    globalBudgetBytes,
    perImageBudgetBytes,
    perAllocationOverheadBytes = 0,
    expandedOutput = true,
  } = {}) {
    for (const [name, value] of [
      ['globalBudgetBytes', globalBudgetBytes],
      ['perImageBudgetBytes', perImageBudgetBytes],
      ['perAllocationOverheadBytes', perAllocationOverheadBytes],
    ]) {
      if (!Number.isSafeInteger(value) || value < 0) throw new Error(`${name} 必须是非负安全整数`);
    }
    this.globalBudgetBytes = globalBudgetBytes;
    this.perImageBudgetBytes = perImageBudgetBytes;
    this.perAllocationOverheadBytes = perAllocationOverheadBytes;
    this.expandedOutput = expandedOutput;
    this.allocator = new RecordingAllocator({ capacityBytes: globalBudgetBytes });
    /** @type {Map<string, {plan:object, usedBytes:number, admittedAtStage:string}>} */
    this._tasks = new Map();
    /** 预算管理器自身的决策审计（含未到达分配器的单图层拒绝） */
    this._decisions = [];
    this._decisionSeq = 0;
  }

  _record(entry) {
    const record = { seq: this._decisionSeq++, ...entry };
    this._decisions.push(record);
    return record;
  }

  /**
   * 分配前的准入判定（元数据不足 / 单图超限在此拦截，尚未占用任何预算）。
   */
  admit({ taskId, metadata } = {}) {
    if (typeof taskId !== 'string' || taskId.length === 0) {
      return this._record({ event: 'admit', decision: 'deny', reason: 'invalid_task_id', taskId });
    }
    if (this._tasks.has(taskId)) {
      return this._record({ event: 'admit', decision: 'deny', reason: 'duplicate_task_id', taskId });
    }

    let plan;
    try {
      plan = buildBufferPlan(metadata, {
        perAllocationOverheadBytes: this.perAllocationOverheadBytes,
        expandedOutput: this.expandedOutput,
      });
    } catch (err) {
      if (err.code === 'INSUFFICIENT_METADATA') {
        return this._record({
          event: 'admit',
          decision: 'insufficient_metadata',
          reason: err.reason,
          problems: err.problems,
          taskId,
        });
      }
      throw err;
    }

    if (plan.peakBytes > this.perImageBudgetBytes) {
      return this._record({
        event: 'admit',
        decision: 'deny',
        reason: 'per_image_limit_exceeded',
        limit: 'per_image',
        taskId,
        peakBytes: plan.peakBytes,
        budgetBytes: this.perImageBudgetBytes,
        overBytes: plan.peakBytes - this.perImageBudgetBytes,
        plan,
      });
    }

    this._tasks.set(taskId, { plan, usedBytes: 0, admittedAtStage: metadata.stage });
    return this._record({
      event: 'admit',
      decision: 'allow',
      taskId,
      peakBytes: plan.peakBytes,
      plan,
    });
  }

  /**
   * 实际申请一个缓冲。先查单图预留，再查并发总预算。
   */
  allocate({ taskId, stage, label, bytes } = {}) {
    const task = this._tasks.get(taskId);
    if (!task) {
      return this._record({ event: 'allocate', decision: 'deny', reason: 'unknown_task', taskId, stage, label });
    }
    if (!STAGES.includes(stage)) {
      return this._record({ event: 'allocate', decision: 'deny', reason: 'invalid_stage', taskId, stage, label });
    }
    if (!Number.isSafeInteger(bytes) || bytes <= 0) {
      return this._record({ event: 'allocate', decision: 'deny', reason: 'invalid_size', taskId, stage, label, bytes });
    }

    const chargedBytes = bytes + this.perAllocationOverheadBytes;

    // 第一层：单图预留（该任务自身的峰值预算）
    if (task.usedBytes + chargedBytes > task.plan.peakBytes) {
      return this._record({
        event: 'allocate',
        decision: 'deny',
        reason: 'per_image_limit_exceeded',
        limit: 'per_image',
        taskId,
        stage,
        label,
        requestedBytes: bytes,
        chargedBytes,
        taskUsedBytes: task.usedBytes,
        peakBytes: task.plan.peakBytes,
        overBytes: task.usedBytes + chargedBytes - task.plan.peakBytes,
      });
    }

    // 第二层：并发总预算（拒绝也会留在分配器审计记录中，且不改变已用量）
    const result = this.allocator.allocate({ taskId, stage, label, bytes: chargedBytes });
    if (!result.ok) {
      return this._record({
        event: 'allocate',
        decision: 'deny',
        reason: 'global_budget_exceeded',
        limit: 'global',
        taskId,
        stage,
        label,
        requestedBytes: bytes,
        chargedBytes,
        globalUsedBytes: this.allocator.usedBytes,
        globalBudgetBytes: this.globalBudgetBytes,
        overBytes: this.allocator.usedBytes + chargedBytes - this.globalBudgetBytes,
        allocatorRecord: result.record,
      });
    }

    task.usedBytes += chargedBytes;
    return this._record({
      event: 'allocate',
      decision: 'allow',
      taskId,
      stage,
      label,
      requestedBytes: bytes,
      chargedBytes,
      handle: result.handle,
      allocatorRecord: result.record,
    });
  }

  /** 释放单个句柄。 */
  free(handle) {
    const result = this.allocator.free(handle);
    if (result.ok) {
      const task = this._tasks.get(handle.taskId);
      if (task) task.usedBytes = Math.max(0, task.usedBytes - result.freedBytes);
    }
    return result;
  }

  /**
   * 任务取消或提前失败：归还该任务占有的全部预算。
   * @returns {{decision:string, taskId:string, freedBytes:number, handleCount:number}}
   */
  release(taskId, reason = 'completed') {
    if (!this._tasks.has(taskId)) {
      return this._record({
        event: 'release',
        decision: 'deny',
        reason: 'unknown_task',
        taskId,
        freedBytes: 0,
        handleCount: 0,
      });
    }
    const result = this.allocator.releaseTask(taskId, reason);
    this._tasks.delete(taskId);
    return this._record({ event: 'release', decision: 'allow', taskId, reason, ...result });
  }

  stats() {
    return {
      globalBudgetBytes: this.globalBudgetBytes,
      perImageBudgetBytes: this.perImageBudgetBytes,
      globalUsedBytes: this.allocator.usedBytes,
      globalPeakBytes: this.allocator.peakBytes,
      globalRemainingBytes: this.allocator.remainingBytes,
      activeTasks: this._tasks.size,
      taskSummaries: [...this._tasks.entries()].map(([taskId, t]) => ({
        taskId,
        usedBytes: t.usedBytes,
        peakBytes: t.plan.peakBytes,
        remainingBytes: t.plan.peakBytes - t.usedBytes,
      })),
      decisionRecords: this._decisions,
      allocatorRecords: this.allocator.records,
      records: this._decisions,
    };
  }
}
