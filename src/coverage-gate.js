// coverage-gate.js
//
// 覆盖准入模块：只考查新版本中“增加或替换出来”的可执行行及其全部分支出口，
// 依据必需测试分片（shard）的覆盖记录决定放行、暂缓或缺测定位。
//
// 设计要点（与题面逐条对应）：
//   1. 待测行 = 逐行差异中新版本侧的新增/替换行 ∩ 编译器标出的可执行行。
//      纯重命名（没有任何 hunk / 没有新行）不产生待测行。
//   2. 每个分片只取“完成成功”的最高尝试号，整份替换早期结果；不做尝试间合并。
//   3. 只合并文件摘要匹配的命中；摘要不匹配的命中忽略。
//   4. 必需分片尚未出现成功结果时暂缓（pending），不输出缺测定论。
//   5. 分支出口按出口维度统计：重跑撤掉某分支唯一命中后，即使该行仍被其他
//      分片执行过，该分支仍会出现在缺测位置中。

/**
 * 未被编译器显式列出出口的可执行行，按单一隐式出口处理时使用的出口标识。
 * @type {string}
 */
export const IMPLICIT_EXIT_ID = 'line';

// ---------------------------------------------------------------------------
// 内部工具
// ---------------------------------------------------------------------------

function fail(message) {
  throw new TypeError(`coverage-gate: ${message}`);
}

function isPositiveInteger(value) {
  return Number.isInteger(value) && value >= 1;
}

function assertFileRef(file, where) {
  if (file === null || typeof file !== 'object') {
    fail(`${where} 必须是文件对象`);
  }
  if (typeof file.path !== 'string' || file.path.length === 0) {
    fail(`${where} 缺少非空字符串 path`);
  }
  if (typeof file.summary !== 'string') {
    fail(`文件 ${file.path} 的 summary 必须是字符串`);
  }
}

// ---------------------------------------------------------------------------
// 1. 从逐行差异中收集新版本侧“增加或替换出来”的行号
// ---------------------------------------------------------------------------

/**
 * 解析单个 hunk 的逐行条目，返回新版本上新增/替换行的行号集合。
 *
 * hunk 结构：
 *   {
 *     oldStart, oldCount, newStart, newCount,
 *     lines: [{ type: 'context' | 'add' | 'delete', text }]
 *   }
 * - context/add 行推进新版本行号；delete 行不推进。
 * - 仅 add 行属于“新版本增加或替换出来”的行；context 是未改动行，delete 属于旧版本。
 *
 * @param {object} hunk 单个差异块
 * @returns {number[]} 新版本行号（升序、去重）
 */
export function collectAddedLines(hunk) {
  if (hunk === null || typeof hunk !== 'object') {
    fail('hunk 必须是对象');
  }
  if (!isPositiveInteger(hunk.newStart)) {
    fail('hunk.newStart 必须是正整数');
  }
  if (!Array.isArray(hunk.lines)) {
    fail('hunk.lines 必须是数组');
  }

  const added = new Set();
  let newLine = hunk.newStart;

  for (const entry of hunk.lines) {
    if (entry === null || typeof entry !== 'object') {
      fail('hunk 条目必须是对象');
    }
    switch (entry.type) {
      case 'context':
        newLine += 1;
        break;
      case 'add':
        added.add(newLine);
        newLine += 1;
        break;
      case 'delete':
        // 仅存在于旧版本，不推进新版本游标。
        break;
      default:
        fail(`未知 hunk 条目类型: ${String(entry.type)}`);
    }
  }

  return [...added].sort((a, b) => a - b);
}

// ---------------------------------------------------------------------------
// 2. 待测目标：新增/替换行 ∩ 可执行行，并归一化分支出口
// ---------------------------------------------------------------------------

function normalizeExits(declared) {
  if (declared === undefined || declared === null) {
    // 普通可执行语句：编译器未列出分支出口，按单一隐式出口处理。
    return [{ id: IMPLICIT_EXIT_ID, kind: 'implicit' }];
  }
  if (!Array.isArray(declared) || declared.length === 0) {
    fail('可执行行的 exits 必须是非空数组，或省略以使用隐式出口');
  }
  const seen = new Set();
  const exits = declared.map((exit) => {
    if (exit === null || typeof exit !== 'object' || typeof exit.id !== 'string') {
      fail('分支出口必须是包含字符串 id 的对象');
    }
    if (seen.has(exit.id)) {
      fail(`分支出口 id 重复: ${exit.id}`);
    }
    seen.add(exit.id);
    return { id: exit.id, kind: 'branch' };
  });
  return exits;
}

/**
 * 构建待测目标列表。
 *
 * @param {Array<{file: {path, summary}, hunks: object[]}>} changes 逐文件差异
 * @param {Map<string, {summary: string, executableLines: Array<{line, exits?}>}>} executableByPath
 *        以路径为键的编译器可执行行索引
 * @returns {Array<{path, summary, line, isBranch, exits: Array<{id, kind}>}>}
 */
export function buildTargets(changes, executableByPath) {
  if (!Array.isArray(changes)) {
    fail('changes 必须是数组');
  }
  if (!(executableByPath instanceof Map)) {
    fail('executableByPath 必须是 Map');
  }

  const targets = [];

  for (const change of changes) {
    if (change === null || typeof change !== 'object') {
      fail('change 必须是对象');
    }
    assertFileRef(change.file, 'change.file');
    if (!Array.isArray(change.hunks)) {
      fail(`文件 ${change.file.path} 的 hunks 必须是数组`);
    }

    const executable = executableByPath.get(change.file.path);
    if (!executable) {
      // 该文件没有可执行行（例如纯文档/纯头文件），无待测行。
      continue;
    }
    if (executable.summary !== change.file.summary) {
      fail(
        `文件 ${change.file.path} 的编译器数据摘要过期：` +
          `差异摘要=${change.file.summary}，编译器摘要=${executable.summary}`,
      );
    }

    const added = new Set(change.hunks.flatMap(collectAddedLines));
    const lineIndex = new Map(
      executable.executableLines.map((entry) => [entry.line, entry]),
    );

    for (const line of [...added].sort((a, b) => a - b)) {
      const entry = lineIndex.get(line);
      if (!entry) {
        // 新增行不可执行（空行、注释、纯括号等），不属于考查范围。
        continue;
      }
      const exits = normalizeExits(entry.exits);
      targets.push({
        path: change.file.path,
        summary: change.file.summary,
        line,
        isBranch: exits.some((exit) => exit.kind === 'branch'),
        exits,
      });
    }
  }

  return targets;
}

// ---------------------------------------------------------------------------
// 3. 分片选择：每个必需分片只保留完成成功的最高尝试号
// ---------------------------------------------------------------------------

/**
 * 从分片记录中选出每个必需分片的有效结果。
 *
 * 选择规则：
 *   - 仅考虑 requiredShardIds 中的分片；其他分片记录列入 ignored。
 *   - 同一分片按 attempt 取最高值；仅当该最高尝试 success 为真时整份采用，
 *     该分片更早的尝试一律不参与合并（整份替换，不跨尝试累积）。
 *   - 最高尝试未成功 / 分片完全没有记录 → 进入 pending。
 *
 * @param {string[]} requiredShardIds 必需分片 id
 * @param {Array<{shardId, attempt, success, coverage}>} shardRecords
 * @returns {{selected: object[], pending: string[], ignored: Array<{shardId, reason}>}}
 */
export function selectShardResults(requiredShardIds, shardRecords) {
  if (!Array.isArray(requiredShardIds) || requiredShardIds.some((id) => typeof id !== 'string')) {
    fail('requiredShardIds 必须是字符串数组');
  }
  if (!Array.isArray(shardRecords)) {
    fail('shardRecords 必须是数组');
  }

  const required = new Set(requiredShardIds);
  const latest = new Map(); // shardId -> 最高 attempt 的记录

  for (const record of shardRecords) {
    if (record === null || typeof record !== 'object') {
      fail('分片记录必须是对象');
    }
    if (typeof record.shardId !== 'string' || record.shardId.length === 0) {
      fail('分片记录缺少非空字符串 shardId');
    }
    if (!isPositiveInteger(record.attempt)) {
      fail(`分片 ${record.shardId} 的 attempt 必须是正整数`);
    }
    if (typeof record.success !== 'boolean') {
      fail(`分片 ${record.shardId} 的 success 必须是布尔值`);
    }
    if (!required.has(record.shardId)) {
      continue;
    }
    const previous = latest.get(record.shardId);
    if (previous && previous.attempt === record.attempt) {
      fail(`分片 ${record.shardId} 的尝试号 ${record.attempt} 出现重复记录`);
    }
    if (!previous || record.attempt > previous.attempt) {
      latest.set(record.shardId, record);
    }
  }

  const selected = [];
  const pending = [];
  const ignored = [];

  for (const record of shardRecords) {
    if (!required.has(record.shardId)) {
      ignored.push({ shardId: record.shardId, reason: 'not-required' });
    }
  }

  for (const shardId of requiredShardIds) {
    const record = latest.get(shardId);
    if (!record || !record.success) {
      // 尚无成功结果（完全缺失，或最高尝试失败）→ 暂缓。
      pending.push(shardId);
      continue;
    }
    selected.push(record);
  }

  return { selected, pending, ignored };
}

// ---------------------------------------------------------------------------
// 4. 合并有效分片的覆盖命中（仅文件摘要匹配的命中）
// ---------------------------------------------------------------------------

/**
 * 合并已选分片的覆盖记录。
 *
 * @param {object[]} selected selectShardResults 选出的成功分片
 * @param {Map<string, {summary: string}>} filesByPath 候选文件路径 -> 摘要
 * @returns {{hits: Map<string, Set<string>>, ignored: Array<{shardId, path, reason}>}}
 *   hits 的键为 `${path}\0${line}`，值为命中的出口 id 集合
 */
export function mergeCoverage(selected, filesByPath) {
  if (!(filesByPath instanceof Map)) {
    fail('filesByPath 必须是 Map');
  }

  const hits = new Map();
  const ignored = [];

  const addHit = (path, line, exitId) => {
    const key = `${path}\0${line}`;
    let set = hits.get(key);
    if (!set) {
      set = new Set();
      hits.set(key, set);
    }
    set.add(exitId);
  };

  for (const record of selected) {
    const coverage = record.coverage;
    if (coverage === undefined || coverage === null) {
      continue; // 成功但零覆盖的分片：贡献空集合。
    }
    if (!Array.isArray(coverage.files)) {
      fail(`分片 ${record.shardId} 的 coverage.files 必须是数组`);
    }

    for (const fileCoverage of coverage.files) {
      if (fileCoverage === null || typeof fileCoverage !== 'object') {
        fail('分片覆盖中的文件记录必须是对象');
      }
      const { path, summary, lines } = fileCoverage;
      if (typeof path !== 'string' || path.length === 0) {
        fail('分片覆盖中的文件记录缺少非空字符串 path');
      }
      if (typeof summary !== 'string') {
        fail(`分片覆盖中文件 ${path} 的 summary 必须是字符串`);
      }
      if (!Array.isArray(lines)) {
        fail(`分片覆盖中文件 ${path} 的 lines 必须是数组`);
      }

      const candidate = filesByPath.get(path);
      if (!candidate) {
        ignored.push({ shardId: record.shardId, path, reason: 'unknown-path' });
        continue;
      }
      if (candidate.summary !== summary) {
        // 摘要不匹配 → 该分片基于过期内容产生，整份文件命中丢弃。
        ignored.push({ shardId: record.shardId, path, reason: 'summary-mismatch' });
        continue;
      }

      for (const lineCoverage of lines) {
        if (
          lineCoverage === null ||
          typeof lineCoverage !== 'object' ||
          !isPositiveInteger(lineCoverage.line)
        ) {
          fail(`分片 ${record.shardId} 覆盖中文件 ${path} 的行记录非法`);
        }
        // 显式 exits：按分支出口记录；未提供 exits 时，executed !== false
        // 视为行被执行，归一到单一隐式出口（兼容只记录行执行的分片）。
        let resolved;
        if (Array.isArray(lineCoverage.exits)) {
          resolved = lineCoverage.exits;
        } else if (lineCoverage.executed !== false) {
          resolved = [IMPLICIT_EXIT_ID];
        } else {
          resolved = [];
        }
        if (resolved.some((id) => typeof id !== 'string')) {
          fail(`分片 ${record.shardId} 中文件 ${path} 第 ${lineCoverage.line} 行的出口 id 非法`);
        }
        for (const exitId of resolved) {
          addHit(path, lineCoverage.line, exitId);
        }
      }
    }
  }

  return { hits, ignored };
}

// ---------------------------------------------------------------------------
// 5. 对照待测目标逐出口找缺测
// ---------------------------------------------------------------------------

/**
 * @param {Array} targets buildTargets 产出的待测目标
 * @param {Map<string, Set<string>>} hits mergeCoverage 产出的命中表
 * @returns {Array<{path, line, kind: 'line'|'branch', missingExits: string[],
 *                  coveredExits: string[]}>}
 */
export function findMissingTargets(targets, hits) {
  if (!Array.isArray(targets)) {
    fail('targets 必须是数组');
  }
  if (!(hits instanceof Map)) {
    fail('hits 必须是 Map');
  }

  const missing = [];

  for (const target of targets) {
    const covered = hits.get(`${target.path}\0${target.line}`) ?? new Set();
    const missingExits = target.exits
      .map((exit) => exit.id)
      .filter((id) => !covered.has(id));

    if (missingExits.length > 0) {
      missing.push({
        path: target.path,
        line: target.line,
        kind: target.isBranch ? 'branch' : 'line',
        missingExits,
        coveredExits: target.exits.map((exit) => exit.id).filter((id) => covered.has(id)),
      });
    }
  }

  missing.sort((a, b) => {
    if (a.path !== b.path) return a.path < b.path ? -1 : 1;
    return a.line - b.line;
  });
  return missing;
}

// ---------------------------------------------------------------------------
// 6. 顶层入口
// ---------------------------------------------------------------------------

/**
 * 评估覆盖准入。
 *
 * @param {object} input
 * @param {Array<{path: string, summary: string}>} input.files
 *        候选文件名及内容摘要。
 * @param {Array<{file: {path, summary}, hunks: object[]}>} input.changes
 *        按文件给出的零或多处逐行差异（context/add/delete + 新旧起始行号）。
 * @param {Array<{file: {path, summary}, executableLines: Array<{line, exits?}>}>} input.executable
 *        编译器标出的可执行行及分支出口。
 * @param {string[]} input.requiredShardIds 必需测试分片 id。
 * @param {Array} input.shardRecords 各分片各尝试的覆盖记录。
 *
 * @returns {{
 *   status: 'pass' | 'pending' | 'missing',
 *   pendingShards: string[],
 *   missing: Array,
 *   targets: Array,
 *   ignored: Array,
 *   mergedHits: Array<{path, line, exits: string[]}>
 * }}
 */
export function evaluateCoverageGate(input) {
  if (input === null || typeof input !== 'object') {
    fail('input 必须是对象');
  }
  const { files, changes, executable, requiredShardIds, shardRecords } = input;

  if (!Array.isArray(files)) {
    fail('files 必须是数组');
  }
  if (!Array.isArray(changes)) {
    fail('changes 必须是数组');
  }
  if (!Array.isArray(executable)) {
    fail('executable 必须是数组');
  }
  if (!Array.isArray(requiredShardIds)) {
    fail('requiredShardIds 必须是数组');
  }
  if (!Array.isArray(shardRecords)) {
    fail('shardRecords 必须是数组');
  }

  // 候选文件索引（路径 -> 摘要）。
  const filesByPath = new Map();
  for (const file of files) {
    assertFileRef(file, 'files[]');
    if (filesByPath.has(file.path)) {
      fail(`候选文件路径重复: ${file.path}`);
    }
    filesByPath.set(file.path, { summary: file.summary });
  }

  // 差异引用的文件必须在候选清单内。
  for (const change of changes) {
    if (change === null || typeof change !== 'object' || !change.file) {
      fail('change 缺少 file');
    }
    if (!filesByPath.has(change.file.path)) {
      fail(`差异引用了不在候选清单中的文件: ${change.file.path}`);
    }
  }

  // 编译器可执行行索引，并校验摘要。
  const executableByPath = new Map();
  for (const entry of executable) {
    if (entry === null || typeof entry !== 'object' || !entry.file) {
      fail('executable 条目缺少 file');
    }
    assertFileRef(entry.file, 'executable[].file');
    if (!filesByPath.has(entry.file.path)) {
      fail(`编译器数据引用了不在候选清单中的文件: ${entry.file.path}`);
    }
    if (filesByPath.get(entry.file.path).summary !== entry.file.summary) {
      fail(`编译器数据中文件 ${entry.file.path} 的摘要与候选文件不一致`);
    }
    if (!Array.isArray(entry.executableLines)) {
      fail(`文件 ${entry.file.path} 的 executableLines 必须是数组`);
    }
    executableByPath.set(entry.file.path, {
      summary: entry.file.summary,
      executableLines: entry.executableLines,
    });
  }

  const targets = buildTargets(changes, executableByPath);

  const { selected, pending, ignored: ignoredShards } = selectShardResults(
    requiredShardIds,
    shardRecords,
  );

  const { hits, ignored: ignoredHits } = mergeCoverage(selected, filesByPath);

  const mergedHits = [...hits.entries()]
    .map(([key, exits]) => {
      const separator = key.indexOf('\0');
      return {
        path: key.slice(0, separator),
        line: Number(key.slice(separator + 1)),
        exits: [...exits].sort(),
      };
    })
    .sort((a, b) => {
      if (a.path !== b.path) return a.path < b.path ? -1 : 1;
      return a.line - b.line;
    });

  // 分片尚未收齐：暂缓，不给出缺测定论。
  if (pending.length > 0) {
    return {
      status: 'pending',
      pendingShards: pending,
      missing: [],
      targets,
      ignored: [...ignoredShards, ...ignoredHits],
      mergedHits,
    };
  }

  const missing = findMissingTargets(targets, hits);

  return {
    status: missing.length === 0 ? 'pass' : 'missing',
    pendingShards: [],
    missing,
    targets,
    ignored: [...ignoredShards, ...ignoredHits],
    mergedHits,
  };
}

export default {
  IMPLICIT_EXIT_ID,
  collectAddedLines,
  buildTargets,
  selectShardResults,
  mergeCoverage,
  findMissingTargets,
  evaluateCoverageGate,
};
