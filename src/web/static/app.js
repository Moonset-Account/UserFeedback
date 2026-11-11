/* RIP/1.0 中文操作页：配置输入、运行任务、逐步回放快照、查看证据与错误。 */
"use strict";

const state = {
  scenarios: [],
  tasks: [],                 // /api/tasks 摘要（含 result）
  selectedTaskId: null,
  snapshotIndex: 0,
  pollTimer: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${resp.status}`);
  }
  return resp.json();
}

// ---------------------------------------------------------------------
// 场景与配置
// ---------------------------------------------------------------------
async function loadScenarios() {
  const data = await api("/api/scenarios");
  state.scenarios = data.scenarios;
  const select = $("scenario-select");
  select.innerHTML = "";
  for (const group of ["demo", "acceptance"]) {
    const groupEl = document.createElement("optgroup");
    groupEl.label = group === "demo" ? "演示小场景（15 秒内）" : "完整验收场景";
    for (const s of state.scenarios.filter((x) => x.group === group)) {
      const opt = document.createElement("option");
      opt.value = s.key;
      opt.textContent = `${s.title}（${s.message_count} 条消息）`;
      groupEl.appendChild(opt);
    }
    select.appendChild(groupEl);
  }
  select.addEventListener("change", renderScenarioDesc);
  renderScenarioDesc();
}

function currentScenario() {
  return state.scenarios.find((s) => s.key === $("scenario-select").value);
}

function renderScenarioDesc() {
  const s = currentScenario();
  $("scenario-desc").textContent = s ? s.description : "";
}

// ---------------------------------------------------------------------
// 任务运行
// ---------------------------------------------------------------------
async function runSingleTask() {
  const s = currentScenario();
  const transport = $("transport-select").value;
  const data = await api("/api/tasks", {
    method: "POST",
    body: JSON.stringify({ scenario_key: s.key, transport }),
  });
  state.selectedTaskId = data.task_id;
  await refreshTasks();
}

async function runDemo() {
  const btn = $("run-demo");
  btn.disabled = true;
  btn.textContent = "示例运行中…";
  try {
    const transport = $("transport-select").value;
    const data = await api("/api/demo", {
      method: "POST",
      body: JSON.stringify({ transport }),
    });
    state.selectedTaskId = data.task_ids[0];
    // 等待两个任务都脱离 running/queued
    await waitForTasks(data.task_ids);
    state.selectedTaskId = data.task_ids[0];
    await refreshTasks();
  } finally {
    btn.disabled = false;
    btn.textContent = "▶ 运行示例（正常 + 边界）";
  }
}

async function waitForTasks(ids) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    await refreshTasks();
    const mine = state.tasks.filter((t) => ids.includes(t.task_id));
    if (mine.length === ids.length &&
        mine.every((t) => !["running", "queued"].includes(t.status))) {
      return;
    }
    await new Promise((r) => setTimeout(r, 300));
  }
}

// ---------------------------------------------------------------------
// 轮询与渲染
// ---------------------------------------------------------------------
async function refreshTasks() {
  const data = await api("/api/tasks");
  state.tasks = data.tasks;
  renderTaskList();
  if (state.selectedTaskId) renderResult();
  const anyRunning = state.tasks.some((t) => ["running", "queued"].includes(t.status));
  if (anyRunning && !state.pollTimer) {
    state.pollTimer = setInterval(async () => {
      await refreshTasks();
      const still = state.tasks.some((t) => ["running", "queued"].includes(t.status));
      if (!still && state.pollTimer) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
      }
    }, 400);
  }
}

function renderTaskList() {
  const box = $("task-list");
  if (!state.tasks.length) {
    box.innerHTML = '<p class="placeholder">暂无任务，点击“运行示例”开始。</p>';
    return;
  }
  box.innerHTML = "";
  for (const task of state.tasks) {
    const sc = state.scenarios.find((s) => s.key === task.scenario_key);
    const el = document.createElement("div");
    el.className = "task-item" + (task.task_id === state.selectedTaskId ? " active" : "");
    el.innerHTML = `
      <div class="line1">
        <span class="scenario-name">${escapeHtml(sc ? sc.title : task.scenario_key)}</span>
        <span class="status-pill status-${task.status}">${statusText(task.status)}</span>
      </div>
      <div class="meta">${task.transport.toUpperCase()} · ${task.created_at} · ${task.task_id}</div>`;
    el.addEventListener("click", () => {
      state.selectedTaskId = task.task_id;
      // 默认定位到最新快照（renderResult 会自动钳制到合法上界）
      state.snapshotIndex = Number.MAX_SAFE_INTEGER;
      renderTaskList();
      renderResult();
    });
    box.appendChild(el);
  }
}

function selectedTask() {
  return state.tasks.find((t) => t.task_id === state.selectedTaskId) || null;
}

function renderResult() {
  const task = selectedTask();
  if (!task || !task.result) {
    $("empty-state").classList.remove("hidden");
    $("result-area").classList.add("hidden");
    return;
  }
  $("empty-state").classList.add("hidden");
  $("result-area").classList.remove("hidden");

  const result = task.result;
  const snapshots = result.snapshots;
  const range = $("snapshot-range");
  range.max = String(snapshots.length - 1);
  if (state.snapshotIndex > snapshots.length - 1) state.snapshotIndex = snapshots.length - 1;
  range.value = String(state.snapshotIndex);

  const snap = snapshots[state.snapshotIndex];
  $("snapshot-label").textContent =
    `#${snap.snapshot_no} / ${snapshots.length - 1} · 触发=${snap.trigger} · 状态=${statusText(snap.status)}`;

  renderSummary(task, snap);
  renderTree(snap.tree);
  renderStatus(snap, result, task);
  renderDecisions(result.decisions);
  renderHistory(task.history);
}

function renderSummary(task, snap) {
  const st = snap.stats;
  $("result-summary").innerHTML =
    `<span class="status-pill status-${snap.status}">${statusText(snap.status)}</span>
     接收 ${st.received} · 接受 ${st.accepted} · 拒绝 ${st.rejected}
     · 挂起 ${snap.unresolved.length} · 插入 ${st.inserts} · 字段错误 ${snap.errors.length}`;
}

// ---------------------------------------------------------------------
// 结果树
// ---------------------------------------------------------------------
function renderTree(tree) {
  const box = $("tree-view");
  if (tree === null) {
    box.innerHTML = '<span class="placeholder">（尚未收到 initial）</span>';
    return;
  }
  box.innerHTML = "";
  box.appendChild(renderNode(tree, true));
}

function renderNode(value, isRoot = false, key = null) {
  const wrap = document.createElement("div");
  wrap.className = "tree-node" + (isRoot ? " root" : "");
  const keyHtml = key !== null ? `<span class="tree-key">${escapeHtml(String(key))}:</span> ` : "";

  if (value !== null && typeof value === "object" && value.__error__ === true) {
    wrap.innerHTML = `${keyHtml}<span class="error-node" title="来源 seq=${value.source_seq}">
      ⚠ 字段错误 [${escapeHtml(value.code)}] seq=${value.source_seq}</span>`;
    return wrap;
  }
  if (Array.isArray(value)) {
    const head = document.createElement("div");
    head.innerHTML = `${keyHtml}<span class="tree-bool">[ ${value.length} 项 ]</span>`;
    wrap.appendChild(head);
    value.forEach((item, i) => wrap.appendChild(renderNode(item, false, i)));
    return wrap;
  }
  if (value !== null && typeof value === "object") {
    const head = document.createElement("div");
    head.innerHTML = `${keyHtml}<span class="tree-bool">{</span>`;
    wrap.appendChild(head);
    for (const [k, v] of Object.entries(value)) {
      wrap.appendChild(renderNode(v, false, k));
    }
    return wrap;
  }
  const cls = typeof value === "string" ? "tree-string"
    : typeof value === "number" ? "tree-number" : "tree-bool";
  wrap.innerHTML = `${keyHtml}<span class="${cls}">${escapeHtml(JSON.stringify(value))}</span>`;
  return wrap;
}

// ---------------------------------------------------------------------
// 状态/错误面板
// ---------------------------------------------------------------------
function renderStatus(snap, result, task) {
  const box = $("status-view");
  const lines = [];
  lines.push(kv("任务状态", statusText(snap.status)));
  lines.push(kv("完整标记", snap.complete ? "是（complete=true）" : "否（暂存树不完整）"));
  if (result.interrupt_reason) lines.push(kv("中断原因", escapeHtml(result.interrupt_reason)));
  lines.push(kv("待解析消息", String(snap.unresolved.length)));
  for (const u of snap.unresolved) {
    lines.push(`<div class="pending-note">↳ seq=${u.seq} ${u.kind} ${escapeHtml(u.path || "")}（${u.reason}）</div>`);
  }
  if (snap.errors.length === 0) {
    lines.push('<p class="ok-item">当前快照无字段错误</p>');
  } else {
    for (const e of snap.errors) {
      lines.push(`<div class="error-item">
        <code>${escapeHtml(e.path)}</code><br/>
        ${escapeHtml(e.code)}：${escapeHtml(e.message)}<br/>
        <span class="pending-note">错误来源 seq=${e.source_seq}</span></div>`);
    }
  }
  box.innerHTML = lines.join("");
}

function kv(k, v) {
  return `<div class="kv"><span class="k">${k}</span><span>${v}</span></div>`;
}

// ---------------------------------------------------------------------
// 决策证据
// ---------------------------------------------------------------------
function renderDecisions(decisions) {
  $("decision-count").textContent = String(decisions.length);
  const box = $("decision-view");
  box.innerHTML = "";
  for (const d of decisions) {
    const row = document.createElement("div");
    const cls = d.accepted ? (d.action === "replay" ? "replay" : d.action) : "rejected";
    row.className = `decision-row ${cls}`;
    row.innerHTML = `
      <span>${d.seq === null ? "—" : d.seq}</span>
      <span>${escapeHtml(d.kind)}</span>
      <span class="tag tag-${d.action}">${actionText(d.action)}</span>
      <span>${escapeHtml(d.path || "—")}${d.reason ? ` <em class="pending-note">(${d.reason})</em>` : ""}</span>
      <span class="placeholder">快照 #${d.snapshot_no} · 点击看原文</span>`;
    row.addEventListener("click", () => showRaw(d.raw));
    box.appendChild(row);
  }
}

function renderHistory(history) {
  $("history-view").innerHTML = history
    .map((h) => `<div>[${escapeHtml(h.at)}] <b>${escapeHtml(h.event)}</b>：${escapeHtml(h.detail)}</div>`)
    .join("");
}

function showRaw(raw) {
  $("raw-content").textContent = typeof raw === "string" ? raw : JSON.stringify(raw, null, 2);
  $("raw-modal").classList.remove("hidden");
}

// ---------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------
function statusText(s) {
  return {
    queued: "排队中", running: "运行中", streaming: "采集中",
    complete: "完整结束", interrupted: "流中断（无 end）",
    ended_unresolved: "结束但有未决项",
  }[s] || s;
}

function actionText(a) {
  return {
    init: "初始", initial: "初始对象", apply: "已应用", replay: "挂起重放",
    pending: "挂起", reject: "拒绝", interrupt: "中断",
  }[a] || a;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// ---------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  await loadScenarios();
  await refreshTasks();
  $("run-task").addEventListener("click", () => runSingleTask().catch(alertErr));
  $("run-demo").addEventListener("click", () => runDemo().catch(alertErr));
  $("snapshot-range").addEventListener("input", (e) => {
    state.snapshotIndex = Number(e.target.value);
    renderResult();
  });
  $("modal-close").addEventListener("click", () => $("raw-modal").classList.add("hidden"));
  $("raw-modal").addEventListener("click", (e) => {
    if (e.target === $("raw-modal")) $("raw-modal").classList.add("hidden");
  });
});

function alertErr(err) {
  window.alert(`操作失败：${err.message}`);
}
