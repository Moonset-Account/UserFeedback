/* 语义契约采集器前端：全部数据来自后端真实执行结果，无任何预填成功数据。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let scenarios = [];
let currentRunId = null;
let currentSuggestions = {};

const VIOLATION_TEXT = {
  LABEL_MISMATCH: "标签含义改变",
  UNIT_MISSING: "单位缺失",
  UNIT_UNSUPPORTED: "单位不在支持子集",
  UNIT_INCOMPATIBLE: "单位量纲不兼容",
  UNIT_SCALE: "单位倍数改变",
  QUALIFIER_MISSING: "缺少必要限定说明",
  STAT_MISMATCH: "统计口径改变",
  TYPE_MISMATCH: "数据类型组成不符",
  HEADER_MISSING: "列头选择器未命中",
};

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function setMessage(text, kind = "") {
  const el = $("#action-message");
  el.textContent = text;
  el.className = `action-message ${kind}`;
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- 场景
async function loadScenarios() {
  scenarios = await api("/api/scenarios");
  const sel = $("#scenario-select");
  sel.innerHTML = scenarios.map((s) => `<option value="${s.key}">${esc(s.title)}</option>`).join("");
  renderScenarioDesc();
}

function renderScenarioDesc() {
  const key = $("#scenario-select").value;
  const s = scenarios.find((x) => x.key === key);
  $("#scenario-desc").textContent = s ? s.description : "";
}

// ---------------------------------------------------------------- 运行列表
async function loadRuns(selectRunId = null) {
  const runs = await api("/api/runs");
  const tbody = $("#runs-tbody");
  if (!runs.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">暂无运行记录</td></tr>';
    return;
  }
  tbody.innerHTML = runs.map((r) => {
    const pills = (r.violation_codes || []).map((c) =>
      `<span class="code-pill" title="${esc(VIOLATION_TEXT[c] || c)}">${esc(c)}</span>`).join("");
    return `<tr class="clickable" data-run="${esc(r.run_id)}">
      <td class="mono">${esc(r.run_id)}</td>
      <td>${esc(r.scenario)}</td>
      <td><span class="badge badge-${r.status}">${r.status === "passed" ? "全部通过" : "已隔离"}</span></td>
      <td class="mono">${esc(r.contract_version)}</td>
      <td><div class="code-pills">${pills || '<span class="hint">无</span>'}</div></td>
      <td>${r.published_count}</td>
      <td>${r.quarantined_count}</td>
      <td><button class="btn btn-small" type="button" data-open="${esc(r.run_id)}">查看详情</button></td>
    </tr>`;
  }).join("");
  $$("#runs-tbody [data-open]").forEach((btn) =>
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openRun(btn.dataset.open);
    }));
  $$("#runs-tbody tr.clickable").forEach((tr) =>
    tr.addEventListener("click", () => openRun(tr.dataset.run)));
  if (selectRunId) await openRun(selectRunId);
}

// ---------------------------------------------------------------- 运行详情
async function openRun(runId) {
  currentRunId = runId;
  $("#detail").classList.remove("hidden");
  $("#detail-run-id").textContent = runId;
  const result = await api(`/api/runs/${runId}`);
  renderDiffs(result);
  renderEvidence(result);
  renderRecords(result);
  await renderSuggestions(result);
  await renderVersions();
}

function renderDiffs(result) {
  const box = $("#diff-list");
  if (!result.violations.length) {
    box.innerHTML = `<div class="diff-card ok"><div class="dc-title">✔ 契约全部通过，无语义差异</div>
      <div class="dc-meta"><span>契约版本：${esc(result.contract_version)}</span>
      <span>采集行数：${result.evidence.row_count}</span></div></div>`;
    return;
  }
  box.innerHTML = result.violations.map((v) => `
    <div class="diff-card">
      <div class="dc-title">${esc(VIOLATION_TEXT[v.code] || v.code)}
        <span class="code-pill">${esc(v.code)}</span>
        <span class="hint">（${v.level === "column" ? "列头级 · 隔离整列记录" : "记录级 · 仅隔离该行"}${v.row_index != null ? ` · 第 ${v.row_index + 1} 行` : ""}）</span>
      </div>
      <div>${esc(v.message)}</div>
      <div class="dc-meta">
        <span>证据选择器：<code>${esc(v.evidence_selector)}</code></span>
        <span>证据原文：“${esc(v.evidence_text || "（空）")}”</span>
      </div>
      <pre>期望：${esc(JSON.stringify(v.expected, null, 2))}\n观察：${esc(JSON.stringify(v.observed, null, 2))}</pre>
    </div>`).join("");
}

function renderEvidence(result) {
  $("#evidence-shot").src = `/api/runs/${result.run_id}/screenshot.png?t=${Date.now()}`;
  $("#evidence-meta").textContent = JSON.stringify({
    url: result.url,
    page_title: result.evidence.page_title,
    table_found: result.evidence.table_found,
    row_count: result.evidence.row_count,
    access: result.evidence.access,
    collected_at: result.started_at,
    observed_fields: result.observed_fields,
  }, null, 2);
  fetch(`/api/runs/${result.run_id}/table.html`).then((r) => r.text()).then((html) => {
    $("#evidence-html").textContent = html;
  });
}

function recordCard(rec, kind) {
  const rows = Object.entries(rec.values || {}).map(([k, val]) =>
    `<tr><td>${esc(k)}</td><td>${esc(val)}</td><td>${esc(rec.raw?.[k] ?? "")}</td></tr>`).join("");
  return `<div class="record-card ${kind}">
    <div class="rc-head">第 ${rec.row_index + 1} 行
      ${kind === "quar" ? `<span class="badge badge-quarantined">已隔离</span>` : `<span class="badge badge-passed">已发布</span>`}
      ${(rec.violation_codes || []).map((c) => `<span class="code-pill">${esc(c)}</span>`).join("")}
    </div>
    ${kind === "quar" ? `<p class="hint">${esc(rec.reason || "")}</p>` : ""}
    <table><tr><td>字段</td><td>解析值</td><td>原始单元格文本</td></tr>${rows}</table>
  </div>`;
}

function renderRecords(result) {
  $("#quarantine-list").innerHTML = result.quarantined.length
    ? result.quarantined.map((r) => recordCard(r, "quar")).join("")
    : '<p class="hint">无隔离记录</p>';
  $("#published-list").innerHTML = result.published.length
    ? result.published.map((r) => recordCard(r, "pub")).join("")
    : '<p class="hint">无正式记录（未通过契约检查的数据不会发布）</p>';
}

// ---------------------------------------------------------------- 批准
async function renderSuggestions(result) {
  currentSuggestions = {};
  let html = "";
  if (result.violations.length) {
    const suggestions = await api(`/api/runs/${result.run_id}/suggestions`);
    currentSuggestions = suggestions;
    const violatedKeys = new Set(result.violations.map((v) => v.field_key));
    html = Object.values(result.contract.fields).filter((f) => violatedKeys.has(f.key)).map((f) => {
      const sug = suggestions[f.key];
      const changes = sug ? diffField(f, sug) : ["无可建议映射"];
      return `<label class="diff-card suggest-row">
        <input type="checkbox" name="approve-field" value="${esc(f.key)}">
        <div>
          <div class="dc-title">${esc(f.name)} <span class="mono">${esc(f.key)}</span></div>
          <div class="field-change">${changes.map(esc).join("<br>")}</div>
        </div>
      </label>`;
    }).join("");
  } else {
    html = '<p class="hint">本次运行没有语义差异，无需批准。</p>';
  }
  $("#suggestion-list").innerHTML = html;
}

function diffField(oldField, newPayload) {
  const lines = [];
  if (JSON.stringify(oldField.allowed_labels) !== JSON.stringify(newPayload.allowed_labels))
    lines.push(`allowed_labels: ${JSON.stringify(oldField.allowed_labels)} → ${JSON.stringify(newPayload.allowed_labels)}`);
  if (oldField.expected_unit !== newPayload.expected_unit)
    lines.push(`expected_unit: ${oldField.expected_unit ?? "无"} → ${newPayload.expected_unit ?? "无"}`);
  if (oldField.required_stat !== newPayload.required_stat)
    lines.push(`required_stat: ${oldField.required_stat} → ${newPayload.required_stat}`);
  if (JSON.stringify(oldField.required_qualifiers) !== JSON.stringify(newPayload.required_qualifiers))
    lines.push(`required_qualifiers: ${JSON.stringify(oldField.required_qualifiers)} → ${JSON.stringify(newPayload.required_qualifiers)}`);
  return lines.length ? lines : ["无语义变化（选择器保持不变）"];
}

async function approveSelected() {
  if (!currentRunId) return;
  const keys = $$('input[name="approve-field"]:checked').map((i) => i.value);
  if (!keys.length) { setMessage("请先勾选要批准的字段映射。", "error"); return; }
  const overrides = {};
  keys.forEach((k) => { if (currentSuggestions[k]) overrides[k] = currentSuggestions[k]; });
  try {
    const out = await api(`/api/runs/${currentRunId}/approve`, {
      method: "POST",
      body: JSON.stringify({
        field_keys: keys,
        approved_by: $("#approver").value.trim(),
        reason: $("#approve-reason").value.trim(),
        overrides,
      }),
    });
    setMessage(`已批准并生成契约新版本 ${out.new_contract_version}，受影响记录 ${out.affected_scope.record_count} 条，可点击“重采”验证。`, "success");
    await loadRuns();
    await openRun(currentRunId);
  } catch (e) {
    setMessage(e.message, "error");
  }
}

async function rerunLatest() {
  if (!currentRunId) return;
  const result = await api(`/api/runs/${currentRunId}`);
  const versions = await api("/api/contracts");
  const latest = versions[versions.length - 1].version;
  let scenario = result.scenario.replace(/^demo-/, "");
  if (!scenarios.some((s) => s.key === scenario)) scenario = "custom";
  setMessage(`正在使用契约 ${latest} 重新采集…`, "busy");
  try {
    const summary = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ scenario, url: result.url, contract_version: latest, note: "批准后重采" }),
    });
    setMessage(`重采完成：${summary.run_id}，状态 ${summary.status}，正式记录 ${summary.published_count} 条。`, "success");
    await loadRuns(summary.run_id);
  } catch (e) {
    setMessage(e.message, "error");
  }
}

// ---------------------------------------------------------------- 版本
async function renderVersions() {
  const [contracts, approvals] = await Promise.all([api("/api/contracts"), api("/api/approvals")]);
  $("#contract-versions").textContent = JSON.stringify(contracts, null, 2);
  $("#approval-records").textContent = approvals.length
    ? JSON.stringify(approvals, null, 2) : "（暂无批准记录）";
}

// ---------------------------------------------------------------- 事件绑定
function bindEvents() {
  $("#scenario-select").addEventListener("change", renderScenarioDesc);
  $("#refresh-runs").addEventListener("click", () => loadRuns());
  $("#run-demo").addEventListener("click", async () => {
    const btn = $("#run-demo");
    btn.disabled = true;
    const t0 = performance.now();
    setMessage("示例运行中：先跑正常基线，再跑标签含义改变的边界场景…", "busy");
    try {
      const out = await api("/api/demo", { method: "POST" });
      const cost = ((performance.now() - t0) / 1000).toFixed(1);
      setMessage(`示例完成（耗时 ${cost} 秒）：${out.demo_runs.map((r) => `${r.scenario}=${r.status}`).join("，")}，两个结果均已保留。`, "success");
      await loadRuns(out.demo_runs[1].run_id);
    } catch (e) {
      setMessage(e.message, "error");
    } finally {
      btn.disabled = false;
    }
  });
  $("#run-scenario").addEventListener("click", async () => {
    const scenario = $("#scenario-select").value;
    setMessage(`正在采集场景 ${scenario} …`, "busy");
    try {
      const summary = await api("/api/runs", { method: "POST", body: JSON.stringify({ scenario }) });
      setMessage(`采集完成：${summary.run_id}，状态 ${summary.status}。`, "success");
      await loadRuns(summary.run_id);
    } catch (e) { setMessage(e.message, "error"); }
  });
  $("#run-custom").addEventListener("click", async () => {
    const url = $("#custom-url").value.trim();
    if (!url) { setMessage("请填写页面 URL，或直接选择场景运行。", "error"); return; }
    setMessage(`正在采集自定义 URL …`, "busy");
    try {
      const summary = await api("/api/runs", { method: "POST", body: JSON.stringify({ scenario: "custom", url }) });
      setMessage(`采集完成：${summary.run_id}，状态 ${summary.status}。`, "success");
      await loadRuns(summary.run_id);
    } catch (e) { setMessage(e.message, "error"); }
  });
  $("#approve-btn").addEventListener("click", approveSelected);
  $("#rerun-btn").addEventListener("click", rerunLatest);
  $("#toggle-detail").addEventListener("click", () => {
    const pane = $("#detail");
    const hidden = pane.classList.toggle("hidden");
    $("#toggle-detail").textContent = hidden ? "展开 ▼" : "收起 ▲";
  });
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".tab-pane").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`#pane-${tab.dataset.tab}`).classList.add("active");
  }));
}

async function checkHealth() {
  try {
    await api("/api/health");
    $("#health-dot").className = "dot dot-ok";
    $("#health-text").textContent = "服务正常";
  } catch {
    $("#health-dot").className = "dot dot-bad";
    $("#health-text").textContent = "服务不可用";
  }
}

(async function init() {
  bindEvents();
  await checkHealth();
  await loadScenarios();
  await loadRuns();
})();
