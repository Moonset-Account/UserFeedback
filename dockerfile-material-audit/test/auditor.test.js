import { test } from 'node:test';
import assert from 'node:assert/strict';
import { audit } from '../src/auditor.js';
import { parseImageRef } from '../src/imageRef.js';
import { substitute } from '../src/variable.js';
import { parseDockerfile } from '../src/parser.js';

const D1 = 'sha256:1111111111111111111111111111111111111111111111111111111111111111';
const D2 = 'sha256:2222222222222222222222222222222222222222222222222222222222222222';
const D3 = 'sha256:3333333333333333333333333333333333333333333333333333333333333333';
const D_OTHER = 'sha256:9999999999999999999999999999999999999999999999999999999999999999';

const find = (report, line) => report.findings.find((f) => f.line === line);

// ---------------------------------------------------------------------------
// 场景一：发布目标已固定内容 -> PASS
// ---------------------------------------------------------------------------
test('发布目标直接引用 @digest 且清单同源同摘要 -> PASS', () => {
  const dockerfile = `
FROM registry.example.com/base/app:1.0@${D1} AS runtime
COPY . /srv
`;
  const report = audit({
    dockerfile,
    target: 'runtime',
    approvals: [
      { id: 'APV-1', registry: 'registry.example.com', repository: 'base/app', digest: D1, tag: '1.0' },
    ],
  });
  assert.equal(report.decision, 'PASS');
  const f = find(report, 2);
  assert.equal(f.status, 'PINNED_APPROVED');
  assert.equal(f.gating, true);
  assert.equal(f.applicableApproval.id, 'APV-1');
});

// ---------------------------------------------------------------------------
// 场景二：发布目标间接引用可变标签（经中间阶段传递）-> FAIL
// ---------------------------------------------------------------------------
test('发布目标经中间阶段间接依赖可变标签 -> FAIL，并报告实际触发指令行', () => {
  const dockerfile = `
FROM registry.example.com/team/builder:v3 AS builder
RUN make
FROM builder AS runtime
COPY --from=builder /out /out
`;
  const report = audit({
    dockerfile,
    target: 'runtime',
    approvals: [
      { id: 'APV-2', registry: 'registry.example.com', repository: 'team/builder', digest: D2, tag: 'v3' },
    ],
  });
  assert.equal(report.decision, 'FAIL');
  assert.deepEqual(report.summary.stages.reachable, ['runtime', 'builder']);

  const f = find(report, 2);
  assert.equal(f.status, 'MUTABLE_TAG_APPROVED');
  assert.equal(f.gating, true);
  assert.equal(f.material.source, 'registry.example.com/team/builder');
  assert.equal(f.material.contentPinned, false);
  assert.equal(f.applicableApproval.id, 'APV-2');
  assert.deepEqual(f.dependencyPath, ['runtime', 'builder']);
  // 触发问题的指令行必须是 FROM 可变标签那一行
  assert.match(f.instruction, /^FROM registry\.example\.com\/team\/builder:v3$/);
});

// ---------------------------------------------------------------------------
// 场景三：审批摘要对应另一来源（同名材料不同来源）-> FAIL
// ---------------------------------------------------------------------------
test('同名材料审批记录属于另一 registry -> MUTABLE_TAG_OTHER_SOURCE', () => {
  const dockerfile = `
FROM registry.example.com/infra/alpine:3.20 AS runtime
`;
  const report = audit({
    dockerfile,
    target: 'runtime',
    approvals: [
      // 仓库路径同为 infra/alpine，但来源是另一个 registry
      { id: 'APV-3', registry: 'mirror.corp.internal', repository: 'infra/alpine', digest: D3, tag: '3.20' },
    ],
  });
  assert.equal(report.decision, 'FAIL');
  const f = find(report, 2);
  assert.equal(f.status, 'MUTABLE_TAG_OTHER_SOURCE');
  assert.equal(f.material.source, 'registry.example.com/infra/alpine');
  assert.equal(f.applicableApproval.registry, 'mirror.corp.internal');
  assert.match(f.message, /另一来源/);
});

test('digest 固定但审批清单里的同摘要属于另一来源 -> PINNED_UNAPPROVED', () => {
  const dockerfile = `
FROM registry.example.com/infra/alpine@${D3} AS runtime
`;
  const report = audit({
    dockerfile,
    target: 'runtime',
    approvals: [
      { id: 'APV-3X', registry: 'mirror.corp.internal', repository: 'infra/alpine', digest: D3 },
    ],
  });
  const f = find(report, 2);
  assert.equal(f.status, 'PINNED_UNAPPROVED');
  assert.equal(report.decision, 'FAIL');
});

// ---------------------------------------------------------------------------
// 场景四：未使用阶段不影响门禁
// ---------------------------------------------------------------------------
test('未使用阶段中的违规材料不影响发布目标门禁', () => {
  const dockerfile = `
FROM registry.example.com/ok/base@${D1} AS runtime
COPY . /srv
FROM untrusted/volatile:latest AS legacy
RUN echo unused
`;
  const report = audit({
    dockerfile,
    target: 'runtime',
    approvals: [
      { id: 'APV-1', registry: 'registry.example.com', repository: 'ok/base', digest: D1 },
    ],
  });
  assert.equal(report.decision, 'PASS');
  assert.deepEqual(report.summary.stages.unused, ['legacy']);
  const legacy = find(report, 4);
  assert.equal(legacy.gating, false);
  assert.equal(legacy.reachable, false);
  assert.equal(legacy.status, 'MUTABLE_TAG_UNAPPROVED');
  assert.equal(report.summary.violations, 0, '未使用阶段的违规不计入门禁');
});

// ---------------------------------------------------------------------------
// 场景五：仅供测试阶段使用的外部材料不参与门禁
// ---------------------------------------------------------------------------
test('测试阶段（名称含 test）的外部材料不参与门禁', () => {
  const dockerfile = `
FROM registry.example.com/ok/base@${D1} AS runtime
COPY . /srv
FROM flaky/testrunner:nightly AS unit-test
RUN npm test
FROM runtime AS release
COPY --from=unit-test /reports /reports
`;
  const report = audit({
    dockerfile,
    target: 'release',
    approvals: [
      { id: 'APV-1', registry: 'registry.example.com', repository: 'ok/base', digest: D1 },
    ],
  });
  // unit-test 可达（被 release COPY --from 引用），但是测试阶段
  assert.equal(report.decision, 'PASS');
  const testRef = find(report, 4);
  assert.equal(testRef.testStage, true);
  assert.equal(testRef.reachable, true);
  assert.equal(testRef.gating, false);
  assert.equal(testRef.status, 'MUTABLE_TAG_UNAPPROVED');
  assert.deepEqual(testRef.dependencyPath, ['release', 'unit-test']);
});

// ---------------------------------------------------------------------------
// 场景六：构建参数无法解析 -> 待核实（INCONCLUSIVE），其余违规仍判 FAIL
// ---------------------------------------------------------------------------
test('ARG 无默认值且构建参数缺失 -> PENDING / INCONCLUSIVE', () => {
  const dockerfile = `
ARG BASE_REGISTRY
ARG BASE_TAG=1.0
FROM ${'$'}{BASE_REGISTRY}/app:${'$'}{BASE_TAG} AS runtime
`;
  const report = audit({
    dockerfile,
    target: 'runtime',
    buildParams: { BASE_TAG: '2.0' },
    approvals: [],
  });
  assert.equal(report.decision, 'INCONCLUSIVE');
  const f = find(report, 4);
  assert.equal(f.status, 'PENDING');
  assert.deepEqual(f.unresolvedArgs, ['BASE_REGISTRY']);
  assert.equal(f.gating, true);
});

test('构建参数可解析且解析结果为已审批 digest -> PASS', () => {
  const dockerfile = `
ARG BASE_REGISTRY=registry.example.com
ARG BASE_IMAGE=team/app
ARG BASE_DIGEST
FROM ${'$'}{BASE_REGISTRY}/${'$'}{BASE_IMAGE}@${'$'}{BASE_DIGEST} AS runtime
`;
  const report = audit({
    dockerfile,
    target: 'runtime',
    buildParams: { BASE_DIGEST: D2 },
    approvals: [
      { id: 'APV-2', registry: 'registry.example.com', repository: 'team/app', digest: D2 },
    ],
  });
  assert.equal(report.decision, 'PASS');
  const f = find(report, 5);
  assert.equal(f.status, 'PINNED_APPROVED');
  assert.equal(f.material.rawReference, `registry.example.com/team/app@${D2}`);
});

test('待核实与真实违规同时存在 -> FAIL（违规优先于待核实）', () => {
  const dockerfile = `
ARG MISSING
FROM ${'$'}{MISSING}/x:1 AS base
FROM base AS runtime
COPY --from=mutable/img:latest /tool /tool
`;
  const report = audit({ dockerfile, target: 'runtime', approvals: [] });
  assert.equal(report.decision, 'FAIL');
  assert.equal(report.summary.violations, 1);
  assert.equal(report.summary.pending, 1);
});

// ---------------------------------------------------------------------------
// 场景七：COPY --from 直接引用外部镜像（非阶段名）
// ---------------------------------------------------------------------------
test('COPY --from 外部镜像同样纳入审计，且可变标签触发违规', () => {
  const dockerfile = `
FROM registry.example.com/ok/base@${D1} AS runtime
COPY --from=registry.example.com/tools/jq:1.7 /bin/jq /bin/jq
`;
  const report = audit({
    dockerfile,
    target: 'runtime',
    approvals: [
      { id: 'APV-1', registry: 'registry.example.com', repository: 'ok/base', digest: D1 },
      { id: 'APV-JQ', registry: 'registry.example.com', repository: 'tools/jq', digest: D_OTHER, tag: '1.7' },
    ],
  });
  assert.equal(report.decision, 'FAIL');
  const jq = find(report, 3);
  assert.equal(jq.via, 'COPY --from');
  assert.equal(jq.status, 'MUTABLE_TAG_APPROVED');
  assert.equal(jq.applicableApproval.id, 'APV-JQ');
});

test('COPY --from 外部镜像带 digest 且同源同摘要 -> 通过', () => {
  const dockerfile = `
FROM registry.example.com/ok/base@${D1} AS runtime
COPY --from=registry.example.com/tools/jq@${D_OTHER} /bin/jq /bin/jq
`;
  const report = audit({
    dockerfile,
    target: 'runtime',
    approvals: [
      { id: 'APV-1', registry: 'registry.example.com', repository: 'ok/base', digest: D1 },
      { id: 'APV-JQ', registry: 'registry.example.com', repository: 'tools/jq', digest: D_OTHER },
    ],
  });
  assert.equal(report.decision, 'PASS');
});

// ---------------------------------------------------------------------------
// 单元：镜像引用归一化（同名不同源 / Docker Hub 官方仓库）
// ---------------------------------------------------------------------------
test('parseImageRef 区分来源身份并归一化 Docker Hub 官方镜像', () => {
  const a = parseImageRef('ubuntu:22.04');
  assert.equal(a.registry, 'docker.io');
  assert.equal(a.repository, 'library/ubuntu');
  assert.equal(a.tag, '22.04');
  assert.equal(a.pinned, false);
  assert.equal(a.sourceKey, 'docker.io/library/ubuntu');

  const b = parseImageRef(`ghcr.io/library/ubuntu@${D1}`);
  assert.equal(b.sourceKey, 'ghcr.io/library/ubuntu');
  assert.equal(b.pinned, true);
  assert.notEqual(a.sourceKey, b.sourceKey, '同名仓库不同 registry 必须身份不同');

  const c = parseImageRef('localhost:5000/ns/img:tag@' + D2);
  assert.equal(c.registry, 'localhost:5000');
  assert.equal(c.tag, 'tag');
  assert.equal(c.digest, D2);
  assert.equal(c.pinned, true);

  const d = parseImageRef('org/img');
  assert.equal(d.registry, 'docker.io');
  assert.equal(d.repository, 'org/img');
});

// ---------------------------------------------------------------------------
// 单元：变量替换
// ---------------------------------------------------------------------------
test('substitute 支持 $VAR / ${VAR} / ${VAR:-default} 并报告未解析变量', () => {
  assert.deepEqual(substitute('$A/${B}/x', { A: 'r', B: 'b' }), {
    value: 'r/b/x',
    unresolved: [],
  });
  const r = substitute('${MISSING:-df}/$ALSO_MISSING', {});
  assert.equal(r.value, 'df/$ALSO_MISSING');
  assert.deepEqual(r.unresolved, ['ALSO_MISSING']);
  assert.deepEqual(substitute('${EMPTY:-fallback}', { EMPTY: '' }).value, 'fallback');
});

// ---------------------------------------------------------------------------
// 单元：解析器保留行号、处理折行与注释
// ---------------------------------------------------------------------------
test('parseDockerfile 保留行号并处理折行/注释/未知指令', () => {
  const src = [
    '# comment',
    'ARG GLOBAL=v1',
    '',
    'FROM ubuntu:22.04 \\',
    '  AS builder',
    'RUN echo hi',
    'COPY --from=builder \\',
    '  /a /b',
  ].join('\n');
  const instr = parseDockerfile(src);
  assert.deepEqual(instr.map((i) => i.kind), ['ARG', 'FROM', 'COPY_FROM']);
  assert.equal(instr[1].line, 4);
  assert.equal(instr[1].alias, 'builder');
  assert.equal(instr[2].line, 7);
  assert.equal(instr[2].from, 'builder');
});

// ---------------------------------------------------------------------------
// 目标不存在时报错
// ---------------------------------------------------------------------------
test('发布目标阶段不存在时抛出明确错误', () => {
  assert.throws(
    () => audit({ dockerfile: 'FROM ubuntu:22.04 AS a\n', target: 'nope' }),
    /发布目标阶段不存在/,
  );
});
