// RPA 控制台前端逻辑（原生 JS，无第三方框架）
const $ = (sel) => document.querySelector(sel);
const scenarioSelect = $('#scenario-select');
let scenarios = [];
let currentSpec = null;
let currentEdges = [];

async function api(path, options) {
  const resp = await fetch(path, options);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

function setStatus(text, cls) {
  const el = $('#status-line');
  el.textContent = text;
  el.className = `status ${cls}`;
}

// ---------------- 场景加载 ----------------
async function loadScenarios() {
  scenarios = await api('/api/scenarios');
  scenarioSelect.innerHTML = scenarios.map(
    (s) => `<option value="${s.scenario_id}">${s.scenario_id} — ${s.title}（${s.n_steps} 步）</option>`
  ).join('');
  await selectScenario();
}

async function selectScenario() {
  const sid = scenarioSelect.value;
  const payload = await api(`/api/scenario/${sid}`);
  currentSpec = payload;
  currentEdges = buildEdges(payload);
  renderMeta(payload);
  renderGraph(payload, currentEdges);
  renderConstraints(payload);
  $('#plan-box').innerHTML = '<span class="hint">点击“运行该场景”生成计划。</span>';
  $('#moves-box').innerHTML = '<p class="hint">运行后在此展示每一步拖拽与回读证据。</p>';
  $('#verify-box').innerHTML = '';
  $('#error-box').innerHTML = '<p class="hint">无错误。</p>';
  setStatus('尚未运行', 'idle');
}

function buildEdges(spec) {
  const edges = [];
  const deps = (spec.constraints && spec.constraints.depends_on) || {};
  Object.entries(deps).forEach(([to, prereqs]) =>
    prereqs.forEach((from) => edges.push({ from, to })));
  const groupLast = {};
  spec.steps.forEach((s) => {
    if (s.group) {
      if (groupLast[s.group]) edges.push({ from: groupLast[s.group], to: s.id, group_edge: true });
      groupLast[s.group] = s.id;
    }
  });
  return edges;
}

function renderMeta(spec) {
  const fixed = (spec.constraints && spec.constraints.fixed) || {};
  $('#scenario-meta').innerHTML = `
    <div><b>${spec.title}</b></div>
    <div>步骤数：${spec.steps.length} ｜ 固定锚点：${Object.keys(fixed).length} 个
      ｜ 依赖边：${currentEdges.length} 条</div>
    <div>来源：${spec.source || '本地样例'}</div>
    ${spec.interference ? `<div style="color:#b91c1c">含执行中手动换位：第 ${spec.interference.after_moves} 次移动后交换 ${spec.interference.swap_a} ⇄ ${spec.interference.swap_b}</div>` : ''}
  `;
}

function renderConstraints(spec) {
  const fixed = (spec.constraints && spec.constraints.fixed) || {};
  const deps = (spec.constraints && spec.constraints.depends_on) || {};
  const lines = [];
  Object.entries(fixed).forEach(([id, slot]) => lines.push(`固定：${id} 必须在槽位 ${slot}`));
  Object.entries(deps).forEach(([to, pre]) =>
    lines.push(`依赖：${pre.join('、')} → ${to}`));
  const groups = {};
  spec.steps.forEach((s) => { if (s.group) (groups[s.group] ??= []).push(s.id); });
  Object.entries(groups).forEach(([g, ids]) =>
    lines.push(`同组 ${g}：${ids.join(' → ')}（保持相对顺序）`));
  $('#constraint-text').innerHTML = lines.length
    ? lines.map((l) => `• ${l}`).join('<br>')
    : '<span class="hint">无额外约束</span>';
}

// ---------------- 依赖图（分层布局）----------------
function renderGraph(spec, edges) {
  const svg = $('#dep-graph');
  svg.innerHTML = '';
  const ids = spec.steps.map((s) => s.id);
  const fixed = (spec.constraints && spec.constraints.fixed) || {};
  const indeg = Object.fromEntries(ids.map((i) => [i, 0]));
  const adj = Object.fromEntries(ids.map((i) => [i, []]));
  edges.forEach((e) => { if (ids.includes(e.from) && ids.includes(e.to)) { adj[e.from].push(e.to); indeg[e.to]++; } });
  const layer = {};
  const q = ids.filter((i) => indeg[i] === 0).map((i) => [i, 0]);
  const seen = new Set();
  const indegWork = { ...indeg };
  while (q.length) {
    const [u, d] = q.shift();
    if (seen.has(u)) continue;
    seen.add(u); layer[u] = Math.max(layer[u] ?? 0, d);
    adj[u].forEach((v) => { indegWork[v]--; if (indegWork[v] <= 0) q.push([v, (layer[u] || 0) + 1]); });
  }
  ids.forEach((i) => { if (!(i in layer)) layer[i] = 0; });
  const byLayer = {};
  ids.forEach((i) => (byLayer[layer[i]] ??= []).push(i));
  const layerCount = Object.keys(byLayer).length;
  const maxRows = Math.max(...Object.values(byLayer).map((v) => v.length));
  const gapX = Math.min(120, Math.floor(410 / Math.max(1, layerCount)));
  const gapY = Math.min(46, Math.floor(210 / Math.max(1, maxRows)));
  const startX = Math.min(46, Math.floor(gapX / 2) + 8);
  const pos = {};
  Object.keys(byLayer).sort((a, b) => a - b).forEach((L, ci) => {
    byLayer[L].forEach((id, ri) => { pos[id] = { x: startX + ci * gapX, y: 30 + ri * gapY }; });
  });
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs) => {
    const el = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
    return el;
  };
  edges.forEach((e) => {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    svg.appendChild(mk('line', {
      x1: a.x + 30, y1: a.y, x2: b.x - 32, y2: b.y,
      stroke: e.group_edge ? '#93c5fd' : '#6b7280', 'stroke-width': 1.3,
      ...(e.group_edge ? { 'stroke-dasharray': '4 3' } : {}),
    }));
  });
  spec.steps.forEach((s) => {
    const p = pos[s.id];
    svg.appendChild(mk('rect', {
      x: p.x - 32, y: p.y - 13, width: 64, height: 24, rx: 6,
      class: fixed[s.id] !== undefined ? 'node-fixed' : 'node-normal',
    }));
    const t = mk('text', { x: p.x, y: p.y + 1, 'text-anchor': 'middle', 'dominant-baseline': 'middle' });
    t.textContent = s.id;
    svg.appendChild(t);
  });
}

// ---------------- 运行任务 ----------------
$('#btn-run').addEventListener('click', () => runScenario(false));
$('#btn-open-source').addEventListener('click', () => {
  window.open(`/source/${scenarioSelect.value}`, '_blank');
});
scenarioSelect.addEventListener('change', selectScenario);

// ---------------- 自定义 JSON 输入 ----------------
const CUSTOM_EXAMPLE = {
  protocol: 'rpa-order/v1',
  scenario_id: 'custom-demo',
  title: '自定义虚构实验',
  steps: [
    { id: 'C1', name: '准备培养基', group: 'g' },
    { id: 'C2', name: '灭菌处理', group: 'g' },
    { id: 'C3', name: '接种观察' },
  ],
  constraints: { fixed: { C1: 0 }, depends_on: { C3: ['C2'] } },
};
$('#custom-json').value = JSON.stringify(CUSTOM_EXAMPLE, null, 2);

async function runCustom(mode) {
  let spec;
  try {
    spec = JSON.parse($('#custom-json').value);
  } catch (err) {
    setStatus(`自定义输入不是合法 JSON：${err.message}`, 'failed');
    $('#error-box').innerHTML = `<div class="conflict-chain">JSON 解析失败：${err.message}</div>`;
    return;
  }
  setStatus('运行自定义输入：采集 → 约束判定 → 规划 → 执行 …', 'running');
  $('#btn-run-custom').disabled = true;
  $('#btn-check-custom').disabled = true;
  try {
    const resp = await fetch('/api/run-custom', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ spec, mode }),
    });
    const record = await resp.json();
    if (!resp.ok) {
      setStatus(record.error || '自定义输入被拒绝', 'failed');
      $('#error-box').innerHTML = `<div class="conflict-chain">${record.error}</div>`;
      return;
    }
    await loadScenarios();
    scenarioSelect.value = spec.scenario_id;
    await selectScenario();
    renderRun(record);
  } catch (err) {
    setStatus(`自定义运行失败：${err.message}`, 'failed');
  } finally {
    $('#btn-run-custom').disabled = false;
    $('#btn-check-custom').disabled = false;
    refreshRuns();
  }
}
$('#btn-run-custom').addEventListener('click', () => runCustom('browser'));
$('#btn-check-custom').addEventListener('click', () => runCustom('check'));

async function runScenario(isDemo) {
  const sid = scenarioSelect.value;
  const mode = $('#mode-check').checked ? 'check' : 'browser';
  setStatus('任务运行中：采集 → 约束判定 → 规划 → 浏览器拖拽 → 独立验证 …', 'running');
  $('#btn-run').disabled = true;
  $('#run-demo').disabled = true;
  try {
    const record = await api(`/api/run/${sid}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    renderRun(record);
  } catch (err) {
    setStatus(`运行失败：${err.message}`, 'failed');
    $('#error-box').innerHTML = `<div class="conflict-chain">${err.message}</div>`;
  } finally {
    $('#btn-run').disabled = false;
    $('#run-demo').disabled = false;
    refreshRuns();
  }
}

// ---------------- 运行示例按钮 ----------------
$('#run-demo').addEventListener('click', async () => {
  setStatus('运行示例：正常场景 + 循环依赖边界场景（目标 15 秒内）…', 'running');
  $('#run-demo').disabled = true;
  $('#btn-run').disabled = true;
  const t0 = performance.now();
  try {
    const demo = await api('/api/demo', { method: 'POST' });
    const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
    const [normal, boundary] = await Promise.all([
      api(`/api/runs/${demo.normal_run_id}`),
      api(`/api/runs/${demo.boundary_run_id}`),
    ]);
    $('#demo-summary').classList.remove('hidden');
    $('#demo-summary').innerHTML = `
      <b>示例完成</b>（前端耗时 ${elapsed}s，后端 ${demo.elapsed_seconds}s）<br>
      正常场景：<span class="tag ${normal.status}">${normal.status}</span><br>
      边界场景（循环依赖）：<span class="tag ${boundary.status}">${boundary.status}</span><br>
      点击下方任务记录可分别查看完整结果。`;
    renderRun(normal);
    await refreshRuns();
    // 把边界场景的冲突链也展示出来
    if (boundary.conflict_chains && boundary.conflict_chains.length) {
      $('#error-box').innerHTML = '<b>边界场景冲突链：</b>' +
        boundary.conflict_chains.map((c) => `<div class="conflict-chain">${c}</div>`).join('');
    }
  } catch (err) {
    setStatus(`示例运行失败：${err.message}`, 'failed');
  } finally {
    $('#run-demo').disabled = false;
    $('#btn-run').disabled = false;
  }
});

// ---------------- 结果渲染 ----------------
function renderRun(record) {
  setStatus(`任务 ${record.run_id}：${statusText(record.status)}`, record.status);
  // 冲突链
  if (record.conflict_chains && record.conflict_chains.length) {
    $('#error-box').innerHTML = '<b>约束不可满足，具体冲突链：</b>' +
      record.conflict_chains.map((c) => `<div class="conflict-chain">${c}</div>`).join('');
    $('#plan-box').innerHTML = '<span class="check-bad">约束不可满足，未生成移动计划。</span>';
    $('#moves-box').innerHTML = '<p class="hint">未执行任何移动。</p>';
    $('#verify-box').innerHTML = '';
    $('#evidence-pre').textContent = JSON.stringify(record, null, 2);
    return;
  }
  if (record.errors && record.errors.length) {
    $('#error-box').innerHTML = record.errors.map((e) => `<div class="conflict-chain">${e}</div>`).join('');
  } else {
    $('#error-box').innerHTML = '<p class="hint">无错误。</p>';
  }
  // 计划
  if (record.plan) {
    const p = record.plan;
    $('#plan-box').innerHTML = `
      <div>目标顺序：<span class="order-line">${p.target.join(' → ')}</span></div>
      <div>理论最少移动下界 n-LCS = <b>${p.lower_bound}</b>，计划移动 ${p.moves.length} 次
        （${p.optimal ? '✅ 达到下界' : '⚠ 未达下界'}）</div>
      <div>${(p.diagnostics || []).join('<br>')}</div>`;
  }
  // 实际移动与回读
  const exec = record.execution;
  if (exec) {
    const html = [];
    if (exec.manual_swaps && exec.manual_swaps.length) {
      exec.manual_swaps.forEach((m) => {
        html.push(`<div class="move-item manual">🔀 <b>用户手动换位</b>（第 ${m.after_moves} 次移动后）：${m.swap.join(' ⇄ ')}
          <div class="order-line">回读：${m.order_after_swap.join(' → ')}</div></div>`);
      });
    }
    exec.history.forEach((h) => {
      html.push(`<div class="move-item ${h.replanned ? 'replan' : ''}">
        <span class="seq">#${h.move_no}</span> 拖动 <b>${h.planned.step_id}</b>
        ${h.planned.anchor_id ? '到 ' + h.planned.anchor_id + ' 之后' : '到首位'}
        ${h.replanned ? ' <span class="tag running">重规划后</span>' : ''}
        <div class="order-line">移动前：${h.order_before.join(' → ')}</div>
        <div class="order-line">回读后：${h.order_after.join(' → ')}</div>
      </div>`);
    });
    $('#moves-box').innerHTML = html.length ? html.join('') : '<p class="hint">顺序已满足，无需移动。</p>';
    // 独立验证
    if (exec.verification) {
      $('#verify-box').innerHTML = exec.verification.checks.map((c) =>
        `<div class="${c.ok ? 'check-ok' : 'check-bad'}">${c.ok ? '✅' : '❌'} ${c.name}：${c.detail}</div>`
      ).join('') + `<div style="margin-top:6px">重规划次数：${exec.replans} ｜ 实际移动：${exec.moves_executed} 次 ｜ 最终顺序：<span class="order-line">${exec.final_order.join(' → ')}</span></div>`;
    }
    $('#evidence-pre').textContent = JSON.stringify(exec.evidence || record, null, 2);
  } else if (record.dry_run_verification) {
    const v = record.dry_run_verification;
    $('#verify-box').innerHTML = v.checks.map((c) =>
      `<div class="${c.ok ? 'check-ok' : 'check-bad'}">${c.ok ? '✅' : '❌'} ${c.name}：${c.detail}</div>`).join('');
    $('#moves-box').innerHTML = '<p class="hint">仅检查模式，未启动浏览器。</p>';
    $('#evidence-pre').textContent = JSON.stringify(record, null, 2);
  }
}

function statusText(s) {
  return ({
    success: '成功（独立验证通过）', failed: '失败', unsatisfiable: '约束不可满足',
    checked: '仅检查完成', running: '运行中',
  })[s] || s;
}

async function refreshRuns() {
  const runs = await api('/api/runs');
  $('#runs-box').innerHTML = runs.length ? runs.map((r) =>
    `<button class="run-pill" data-run="${r.run_id}">
      ${r.started_at} ｜ ${r.scenario_id} ｜ ${r.mode}
      <span class="tag ${r.status}">${r.status}</span></button>`).join('')
    : '<p class="hint">暂无任务记录。</p>';
  document.querySelectorAll('.run-pill').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const record = await api(`/api/runs/${btn.dataset.run}`);
      renderRun(record);
    }));
}

loadScenarios().then(refreshRuns).catch((err) => setStatus(`初始化失败：${err.message}`, 'failed'));
