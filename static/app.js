/* 实验数据对象分段采集 —— 前端交互逻辑
 * 所有结果均来自后端真实执行的任务状态与事件，前端不预填任何成功数据。
 */
"use strict";

const state = {
  scenarios: [],
  tasks: [],
  selectedId: null,
  pollTimer: null,
  eventTimer: null,
  demoRunning: false,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`HTTP ${resp.status} ${text}`);
  }
  return resp.json();
}

function fmtTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

// ------------------------------------------------------------ 场景目录
async function loadScenarios() {
  const data = await api("/api/scenarios");
  state.scenarios = data.scenarios;
  const select = $("scenario-select");
  select.innerHTML = "";
  for (const item of state.scenarios) {
    const opt = document.createElement("option");
    opt.value = item.key;
    opt.textContent = item.name;
    select.appendChild(opt);
  }
  select.addEventListener("change", fillScenarioForm);
  fillScenarioForm();
}

function fillScenarioForm() {
  const key = $("scenario-select").value;
  const item = state.scenarios.find((s) => s.key === key);
  if (!item) return;
  $("scenario-desc").textContent = item.desc;
  $("object-input").value = item.object;
  $("replacement-input").value = item.replacement || "";
}

// ------------------------------------------------------------ 任务列表
async function loadTasks(preferId) {
  const data = await api("/api/tasks");
  state.tasks = data.tasks || [];
  if (preferId) state.selectedId = preferId;
  if (!state.selectedId && state.tasks.length) {
    state.selectedId = state.tasks[0].task_id || state.tasks[0].id;
  }
  renderTaskList();
  if (state.selectedId) await renderDetail(state.selectedId);
}

function renderTaskList() {
  const box = $("task-list");
  if (!state.tasks.length) {
    box.innerHTML = '<p class="hint">暂无任务，点击“运行示例”或创建任务。</p>';
    return;
  }
  box.innerHTML = "";
  for (const task of state.tasks) {
    const id = task.task_id || task.id;
    const length = task.expected_length || 0;
    const pct = length ? Math.round((task.covered / length) * 100) : 0;
    const card = document.createElement("div");
    card.className = "task-card" + (id === state.selectedId ? " active" : "");
    card.innerHTML = `
      <div class="tc-top">
        <span class="tc-name">${esc(task.label || task.scenario || id)}</span>
        <span class="badge ${esc(task.status)}">${esc(statusText(task.status))}</span>
      </div>
      <div class="tc-meta mono">${esc(id)}${task.recovered ? " · 重启恢复" : ""}</div>
      <div class="mini-progress"><i style="width:${pct}%"></i></div>
      <div class="tc-meta">覆盖 ${task.covered}/${length ?? "?"} 字节 · 拒绝 ${task.rejected_count ?? 0}</div>`;
    card.addEventListener("click", () => {
      state.selectedId = id;
      renderTaskList();
      renderDetail(id);
    });
    box.appendChild(card);
  }
}

function statusText(status) {
  return {
    running: "运行中", paused: "已暂停", complete: "已完成",
    failed: "失败", stopped: "已停止",
  }[status] || status;
}

// ------------------------------------------------------------ 任务详情
async function renderDetail(id) {
  const task = await api(`/api/tasks/${encodeURIComponent(id)}`);
  if (task.error && !task.status) {
    $("detail-empty").textContent = task.error;
    $("detail-body").hidden = true;
    $("detail-empty").hidden = false;
    return;
  }
  $("detail-empty").hidden = true;
  $("detail-body").hidden = false;
  $("detail-title").textContent = "任务详情";
  $("detail-label").textContent = task.label || task.scenario || "";
  $("detail-id").textContent = id;

  const badge = $("badge-status");
  badge.className = `badge ${task.status}`;
  badge.textContent = statusText(task.status);

  $("stat-length").textContent = task.expected_length ?? "—";
  $("stat-covered").textContent = `${task.covered ?? 0} / ${task.expected_length ?? "—"}`;
  $("stat-gaps").textContent = (task.gaps || []).length;
  $("stat-rejected").textContent = task.rejected_count ?? 0;
  $("stat-requests").textContent = task.request_count ?? 0;
  $("stat-spans").textContent = (task.spans || []).length;

  renderCoverage(task);
  renderIntervalLists(task);
  renderVersions(task);
  renderError(task);
  renderResult(task);
  renderActions(task);
  await renderEvents(id);
}

function renderCoverage(task) {
  const bar = $("coverage-bar");
  bar.innerHTML = "";
  const length = task.expected_length || 0;
  if (!length) return;
  const marks = [];
  for (const [s, e] of task.spans || []) {
    marks.push({ pos: s, covered: true }, { pos: e, covered: true });
  }
  for (const [s, e] of task.gaps || []) {
    marks.push({ pos: s, covered: false }, { pos: e, covered: false });
  }
  marks.push({ pos: 0, covered: (task.spans || []).some(([s]) => s === 0) });
  marks.push({ pos: length, covered: true });
  marks.sort((a, b) => a.pos - b.pos || Number(b.covered) - Number(a.covered));
  let prev = 0;
  let covered = (task.spans || []).some(([s, e]) => s === 0 && e > 0);
  const dedup = [];
  for (const mark of marks) {
    if (mark.pos > prev) {
      dedup.push({ from: prev, to: mark.pos, covered });
    }
    prev = mark.pos;
    covered = mark.covered;
  }
  for (const seg of dedup) {
    const width = ((seg.to - seg.from) / length) * 100;
    if (width <= 0) continue;
    const div = document.createElement("div");
    div.className = seg.covered ? "seg-covered" : "seg-gap";
    div.style.width = `${width}%`;
    div.title = `${seg.covered ? "已覆盖" : "缺口"} [${seg.from}, ${seg.to})`;
    bar.appendChild(div);
  }
}

function renderIntervalLists(task) {
  const spans = $("spans-list");
  const gaps = $("gaps-list");
  spans.innerHTML = (task.spans || [])
    .map(([s, e]) => `<li>[${s}, ${e}) · ${e - s} 字节</li>`).join("")
    || '<li class="dim">无</li>';
  gaps.innerHTML = (task.gaps || [])
    .map(([s, e]) => `<li class="gap-item">[${s}, ${e}) · ${e - s} 字节</li>`).join("")
    || '<li class="dim">无缺口，覆盖完整</li>';
}

function renderVersions(task) {
  const list = $("version-list");
  const chain = task.version_chain || [];
  if (!chain.length) {
    list.innerHTML = '<li class="dim">尚未锁定版本</li>';
    return;
  }
  list.innerHTML = chain.map((v) => `
    <li>
      <span class="etag">${esc(v.etag)}</span>
      <span class="dim">（长度 ${v.length}）</span>
      <span class="reason">${esc(v.reason)} · ${fmtTime(v.seen_at)}</span>
    </li>`).join("");
}

function renderError(task) {
  const box = $("error-box");
  if (task.status === "failed" || task.last_error) {
    box.hidden = false;
    $("error-text").textContent = task.error || task.last_error;
  } else {
    box.hidden = true;
  }
}

function renderResult(task) {
  const box = $("result-box");
  if (!task.result) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const r = task.result;
  const rows = [
    ["对象名", r.object_name],
    ["记录条数", r.record_count],
    ["分组计数", JSON.stringify(r.groups)],
    ["温度最小值", r.temperature_min],
    ["温度最大值", r.temperature_max],
    ["温度平均值", r.temperature_avg],
    ["measured 求和", r.measured_sum],
    ["已复核条数", r.checked_count],
    ["对象大小（字节）", r.size],
    ["body SHA-256", r.sha256],
  ];
  $("result-grid").innerHTML = rows.map(
    ([k, v]) => `<div class="result-cell"><span class="rk">${esc(k)}</span>`
      + `<span class="rv">${esc(v)}</span></div>`,
  ).join("");
}

function renderActions(task) {
  const terminal = ["complete", "failed", "stopped"].includes(task.status);
  $("btn-pause").disabled = task.status !== "running";
  $("btn-resume").disabled = task.status !== "paused";
  $("btn-stop").disabled = terminal;
}

async function renderEvents(id) {
  const data = await api(`/api/tasks/${encodeURIComponent(id)}/events`);
  const log = $("event-log");
  const events = (data.events || []).slice(-150);
  if (!events.length) {
    log.innerHTML = '<p class="hint">暂无事件。</p>';
    return;
  }
  log.innerHTML = events.map((e) => `
    <div class="event-item ${esc(e.level)}">
      <div class="event-head">
        <span class="event-level">${esc(e.level.toUpperCase())}</span>
        <span class="event-type">${esc(e.type)}</span>
        <span class="event-time">${fmtTime(e.ts)}</span>
      </div>
      <p class="event-msg">${esc(e.message)}</p>
      ${Object.keys(e.evidence || {}).length
        ? `<pre class="event-ev">${esc(JSON.stringify(e.evidence, null, 2))}</pre>` : ""}
    </div>`).join("");
  log.scrollTop = log.scrollHeight;
}

// ------------------------------------------------------------ 操作
async function createTask() {
  const key = $("scenario-select").value;
  const body = {
    scenario: key,
    object: $("object-input").value.trim(),
    replacement: $("replacement-input").value.trim() || null,
    chunk_size: Number($("chunk-input").value) || 2048,
  };
  const info = await api("/api/tasks", {
    method: "POST",
    body: JSON.stringify(body),
  });
  state.selectedId = info.task_id;
  await loadTasks(info.task_id);
}

async function taskAction(action) {
  if (!state.selectedId) return;
  await api(`/api/tasks/${encodeURIComponent(state.selectedId)}/${action}`, {
    method: "POST",
  });
  await loadTasks(state.selectedId);
}

async function runDemo() {
  if (state.demoRunning) return;
  state.demoRunning = true;
  const btn = $("run-demo");
  const status = $("demo-status");
  btn.disabled = true;
  status.textContent = "示例运行中：先正常续取，后边界场景…";
  try {
    const info = await api("/api/demo", { method: "POST" });
    const ids = info.tasks.map((t) => t.task_id);
    state.selectedId = ids[0];
    const deadline = Date.now() + 15000;
    // 轮询直到两个任务都到达终态（或 15 秒超时）
    await new Promise((resolve) => {
      const timer = setInterval(async () => {
        await loadTasks().catch(() => {});
        const terminal = ids
          .map((id) => state.tasks.find((t) => (t.task_id || t.id) === id))
          .every((t) => t && ["complete", "failed", "stopped"].includes(t.status));
        if (terminal || Date.now() > deadline) {
          clearInterval(timer);
          status.textContent = "示例结束，两个任务结果已保留，可在左侧切换查看。";
          resolve();
        }
      }, 400);
    });
  } catch (err) {
    status.textContent = `示例运行失败：${err.message}`;
  } finally {
    state.demoRunning = false;
    btn.disabled = false;
  }
}

// ------------------------------------------------------------ 自动轮询
function startPolling() {
  state.pollTimer = setInterval(async () => {
    if (state.tasks.length) await loadTasks().catch(() => {});
  }, 1500);
  state.eventTimer = setInterval(async () => {
    if (state.selectedId && $("auto-refresh").checked) {
      await renderEvents(state.selectedId).catch(() => {});
    }
  }, 1200);
}

window.addEventListener("DOMContentLoaded", async () => {
  $("create-task").addEventListener("click", createTask);
  $("refresh-tasks").addEventListener("click", () => loadTasks());
  $("run-demo").addEventListener("click", runDemo);
  $("btn-pause").addEventListener("click", () => taskAction("pause"));
  $("btn-resume").addEventListener("click", () => taskAction("start"));
  $("btn-stop").addEventListener("click", () => taskAction("stop"));
  $("btn-reload-events").addEventListener("click", () =>
    state.selectedId && renderEvents(state.selectedId));
  await loadScenarios();
  await loadTasks();
  startPolling();
});
