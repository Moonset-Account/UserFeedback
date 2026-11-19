import { Side, BackPolicy } from './controller.js';

let seq = 0;

/** 生成确定性事件 id：前缀 + 进程内自增序号。测试也可显式传入 id 做重放。 */
export function eventId(prefix) {
  seq += 1;
  return `${prefix}-${String(seq).padStart(4, '0')}`;
}

/** 重置自增序号，仅供测试构造可预测 id。 */
export function resetEventSequence() {
  seq = 0;
}

/**
 * 纸张登记事件。
 * @param {object} p
 * @param {string} p.paperId 实体纸编号（卡纸重印必须换新纸号）
 * @param {string} p.ownerId 归属人
 * @param {string} p.jobId 打印任务
 * @param {string} p.compartmentId 对应取件格
 * @param {('PRINTED'|'BLANK')} p.backPolicy 背面策略；单面任务必须显式 BLANK
 */
export function paperRegistered({ paperId, ownerId, jobId, compartmentId, backPolicy }, id = eventId('reg')) {
  return {
    eventId: id,
    type: 'PAPER_REGISTERED',
    paperId,
    ownerId,
    jobId,
    compartmentId,
    backPolicy,
  };
}

/** 单面成像完成回执（FRONT / BACK 可分次上报）。 */
export function sideImaged({ paperId, ownerId, jobId, side }, id = eventId('img')) {
  if (side !== Side.FRONT && side !== Side.BACK) {
    throw new Error(`非法成像面 ${side}`);
  }
  return { eventId: id, type: 'SIDE_IMAGED', paperId, ownerId, jobId, side };
}

/** 传感器实际出纸记录。 */
export function paperOutput({ paperId, sensorId }, id = eventId('out')) {
  return { eventId: id, type: 'PAPER_OUTPUT', paperId, sensorId };
}

/** 同一人的取件授权。 */
export function pickupAuthorized({ paperId, ownerId }, id = eventId('auth')) {
  return { eventId: id, type: 'PICKUP_AUTHORIZED', paperId, ownerId };
}

/** 卡纸：旧纸进入隔离。 */
export function paperJammed({ paperId }, id = eventId('jam')) {
  return { eventId: id, type: 'PAPER_JAMMED', paperId };
}

/** 回收确认：隔离旧纸完成销毁/回收闭环。 */
export function paperRecycled({ paperId }, id = eventId('rec')) {
  return { eventId: id, type: 'PAPER_RECYCLED', paperId };
}

export { BackPolicy, Side };

/**
 * 事件重放：在一个新控制器上按给定顺序回放完整事件日志。
 * 控制器本身是纯内存、无外部 IO 的状态机，重放结果必须与首次处理完全一致。
 *
 * @param {import('./controller.js').PaperOutputController} controller
 * @param {object[]} events 事件日志（可乱序、可含重复 eventId）
 * @returns {{results: object[], errors: {event: object, code: string, message: string}[]}}
 */
export function replay(controller, events) {
  const results = [];
  const errors = [];
  for (const event of events) {
    try {
      results.push(controller.handle(structuredClone(event)));
    } catch (err) {
      errors.push({
        event: structuredClone(event),
        code: err.code ?? 'UNKNOWN',
        message: err.message,
      });
    }
  }
  return { results, errors };
}
