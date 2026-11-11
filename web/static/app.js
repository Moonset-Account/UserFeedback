/* 响应语义判定器控制台前端逻辑 */

const $ = (id) => document.getElementById(id);
const CATEGORY_LABELS = {
  VALID: "有效资料页", EMPTY: "合法空结果", MAINTENANCE: "维护页面",
  ACCESS_NOTICE: "访问提示", ERROR_PAGE: "错误页（HTTP 200）",
  BROKEN_TEMPLATE: "模板残缺页", FETCH_FAILED: "采集失败", UNKNOWN: "不确定（已隔离）",
};
let scenarios = [];
let selectedJobId = null;

function setStatus(text, kind = "") {
  const el = $("run-status");
  el.textContent = text;
  el.className = "run-status" + (kind ? " " + kind : "");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

// ---------- 场景目录 ----------
async function loadScenarios() {
  const data = await api("/api/scenarios");
  scenarios = data.scenarios;
  const picker = $("scenario-picker");
  picker.innerHTML = '<option value="">— 选择场景自动填入 —</option>' + scenarios.map((s) => (
    `<option value="${esc(s.id)}">${esc(s.id)} · ${esc(s.title)}（HTTP ${s.status ?? "传输故障"}）</option>`
  )).join("");
  picker.addEventListener("change", () => {
    const s = scenarios.find((x) => x.id === picker.value);
    if (!s) return;
    const origin = window.location.origin;
    $("target").value = origin + s.path;
    $("mode").value = "http";
    $("label").value = `手工任务：${s.title}`;
  });
}

// ---------- 规则 ----------
async function loadRules() {
  const data = await api("/api/rules");
  $("rules-editor").value = data.rules_json;
}

$("rules-save").addEventListener("click", async () => {
  const status = $("rules-status");
  try {
    await api("/api/rules", { method: "PUT", body: JSON.stringify({ rules_json: $("rules-editor").value }) });
    status.textContent = "✓ 已保存并通过校验";
    status.style.color = "var(--ok)";
  } catch (err) {
    status.textContent = "✗ " + err.message;
    status.style.color = "var(--danger)";
  }
});

$("rules-reset").addEventListener("click", () => {
  $("rules-editor").value = "[]";
});

// ---------- 运行 ----------
async function runSingle() {
  $("input-error").hidden = true;
  const body = {
    target: $("target").value.trim(),
    mode: $("mode").value,
    referer: $("referer").value.trim(),
    label: $("label").value.trim(),
  };
  if (!body.target) {
    $("input-error").textContent = "请填写采集目标";
    $("input-error").hidden = false;
    return;
  }
  setStatus("任务执行中…", "busy");
  try {
    await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    setStatus("✓ 单次任务完成", "");
  } catch (err) {
    setStatus("✗ " + err.message, "error");
  }
  await refreshJobs();
}

$("run-one").addEventListener("click", runSingle);

$("run-demo").addEventListener("click", async () => {
  const btn = $("run-demo");
  btn.disabled = true;
  setStatus("运行示例中（正常 + 边界两个小场景）…", "busy");
  const started = performance.now();
  try {
    const data = await api("/api/suites/demo", { method: "POST" });
    const cost = ((performance.now() - started) / 1000).toFixed(1);
    setStatus(`✓ 示例完成，耗时 ${cost}s（要求 15s 内）`, "");
    await refreshJobs(data.job_ids);
  } catch (err) {
    setStatus("✗ " + err.message, "error");
  } finally {
    btn.disabled = false;
  }
});

$("run-full").addEventListener("click", async () => {
  const btn = $("run-full");
  btn.disabled = true;
  setStatus("完整验收运行中（22 个场景，含传输故障）…", "busy");
  try {
    const data = await api("/api/suites/full", { method: "POST" });
    renderSuiteSummary(data.jobs);
    setStatus(`✓ 完整验收完成，共 ${data.jobs.length} 个任务`, "");
    await refreshJobs(data.job_ids);
  } catch (err) {
    setStatus("✗ " + err.message, "error");
  } finally {
    btn.disabled = false;
  }
});

function renderSuiteSummary(jobs) {
  const bar = $("suite-bar");
  bar.hidden = false;
  const counts = {};
  for (const j of jobs) counts[j.latest_category] = (counts[j.latest_category] || 0) + 1;
  const isolated = jobs.filter((j) => j.isolated).length;
  const failedFetch = jobs.filter((j) => !j.fetch_ok).length;
  $("suite-summary").innerHTML = [
    `<span class="suite-chip">总计 <b>${jobs.length}</b></span>`,
    ...Object.entries(counts).map(([cat, n]) =>
      `<span class="suite-chip">${esc(CATEGORY_LABELS[cat] || cat)} <b>${n}</b></span>`),
    `<span class="suite-chip">隔离 <b>${isolated}</b></span>`,
    `<span class="suite-chip">采集失败 <b>${failedFetch}</b>（与合法空结果分列）</span>`,
  ].join("");
}

// ---------- 任务列表 ----------
async function refreshJobs(highlightIds = null) {
  const data = await api("/api/jobs");
  const tbody = $("job-tbody");
  if (!data.jobs.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">尚未运行任务。</td></tr>';
    return;
  }
  if (highlightIds && highlightIds.length) selectedJobId = highlightIds[0];
  tbody.innerHTML = data.jobs.map((j) => `
    <tr class="job-row ${j.job_id === selectedJobId ? "selected" : ""}" data-id="${esc(j.job_id)}">
      <td title="${esc(j.label)}">${esc(j.label || j.job_id)}</td>
      <td>${esc(j.mode)}</td>
      <td><span class="cat-badge cat-${esc(j.latest_category)}">${esc(j.latest_category_label)}</span>
          ${j.isolated ? '<span class="isolated-flag">隔离</span>' : ""}</td>
      <td>${(j.confidence * 100).toFixed(0)}%</td>
      <td>${j.revision_count > 1 ? `第 ${j.revision_count} 版` : "初判"}</td>
      <td>${j.fetch_ok ? "采集成功" : '<span style="color:var(--danger)">采集失败</span>'}</td>
      <td><button class="btn" data-detail="${esc(j.job_id)}">证据 / 复判</button></td>
    </tr>`).join("");
  tbody.querySelectorAll("[data-detail]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      showDetail(btn.dataset.detail);
    });
  });
  tbody.querySelectorAll(".job-row").forEach((row) => {
    row.addEventListener("click", () => showDetail(row.dataset.id));
  });
  if (selectedJobId) showDetail(selectedJobId);
}

// ---------- 详情与证据 ----------
async function showDetail(jobId) {
  selectedJobId = jobId;
  document.querySelectorAll(".job-row").forEach((r) =>
    r.classList.toggle("selected", r.dataset.id === jobId));
  const data = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
  const job = data.job;
  $("job-detail").hidden = false;
  $("detail-title").textContent = job.label || `任务 ${job.job_id}`;

  const latest = job.history[job.history.length - 1];
  const fetch = job.fetch;
  $("detail-meta").innerHTML = `
    <div class="kv"><b>目标</b>${esc(job.target)}</div>
    <div class="kv"><b>模式</b>${esc(job.mode)}</div>
    <div class="kv"><b>HTTP 状态</b>${fetch.status_code ?? "—"}（不作单一依据）</div>
    <div class="kv"><b>最新分类</b><span class="cat-badge cat-${esc(latest.category)}">${esc(latest.category_label)}</span></div>
    <div class="kv"><b>置信度</b>${(latest.confidence * 100).toFixed(1)}%</div>
    <div class="kv"><b>置信边界</b>${(latest.confidence_lower_bound * 100).toFixed(1)}% ~ ${(latest.confidence_upper_bound * 100).toFixed(1)}%</div>
    <div class="kv"><b>前名分差</b>${(latest.margin * 100).toFixed(1)}%</div>
    <div class="kv"><b>采集耗时</b>${fetch.elapsed_ms ?? 0} ms</div>
    <div class="kv"><b>导航来源</b>${esc(fetch.nav_source || "—")}</div>`;

  // 历史链：旧判断与新依据并列保留
  $("detail-history").innerHTML = job.history.map((h, i) => {
    const prev = h.previous_category
      ? '<span class="arrow">← 旧判断</span><span class="cat-badge cat-'
        + esc(h.previous_category) + '">'
        + esc(CATEGORY_LABELS[h.previous_category] || h.previous_category) + "</span>"
      : (i === 0 ? '<span class="hint">初次判定</span>' : "");
    const changed = h.previous_category && h.previous_category !== h.category;
    const rulePart = (h.rule_codes && h.rule_codes.length)
      ? "· 命中规则：" + esc(h.rule_codes.join(", ")) : "";
    const isolatedPart = h.isolated
      ? "· 隔离原因：" + esc((h.isolation_reasons || []).join("；")) : "";
    return '<div class="history-item" style="border-left-color:'
      + (changed ? "var(--warn)" : "var(--primary)") + '">'
      + '<span class="rev">v' + h.revision + "</span>" + esc(h.at)
      + " · 依据：" + esc(h.basis)
      + (h.note ? "· " + esc(h.note) : "")
      + '<div style="margin-top:4px">'
      + '<span class="cat-badge cat-' + esc(h.category) + '">'
      + esc(h.category_label) + "</span> "
      + prev + " " + rulePart + " " + isolatedPart
      + "</div></div>";
  }).join("");

  // 证据
  $("detail-evidence").innerHTML = latest.evidences.map((ev) => {
    const wClass = ev.weight > 0 ? "weight-pos" : ev.weight < 0 ? "weight-neg" : "weight-zero";
    const sign = ev.weight > 0 ? "+" : "";
    return `<div class="evidence ${wClass}">
      <div class="ev-head">
        <span class="ev-code">${esc(ev.code)} · ${esc(ev.layer)}</span>
        <span class="ev-weight">权重 ${sign}${ev.weight.toFixed(2)}</span>
      </div>
      <div class="ev-msg">${esc(ev.message)}</div>
      <div class="ev-support">支持类别：${esc(CATEGORY_LABELS[ev.supports] || ev.supports)} · 区域：${esc(ev.region || "—")}</div>
      ${ev.excerpt ? `<div class="ev-excerpt">${esc(ev.excerpt)}</div>` : ""}
    </div>`;
  }).join("");

  // 快照
  const snapshot = {
    url: fetch.url, status_code: fetch.status_code, content_type: fetch.content_type,
    fetch_mode: fetch.fetch_mode, elapsed_ms: fetch.elapsed_ms,
    html_length: fetch.html_length, error: fetch.error || "",
    html_snapshot: fetch.html_snapshot || "",
  };
  $("detail-snapshot").textContent = JSON.stringify(snapshot, null, 2);
  $("btn-reclassify").disabled = !fetch.ok;
  $("btn-reclassify").title = fetch.ok ? "" : "采集失败任务无页面快照，不可复判";
}

$("btn-reclassify").addEventListener("click", async () => {
  if (!selectedJobId) return;
  setStatus("复判中（复用采集快照，不重新采集）…", "busy");
  try {
    await api(`/api/jobs/${encodeURIComponent(selectedJobId)}/reclassify`, {
      method: "POST",
      body: JSON.stringify({ note: "用户在控制台保存补充规则后发起复判" }),
    });
    setStatus("✓ 复判完成，旧判断已保留在历史链", "");
    await refreshJobs();
  } catch (err) {
    setStatus("✗ " + err.message, "error");
  }
});

// ---------- 初始化 ----------
(async function init() {
  try {
    await loadScenarios();
    await loadRules();
    await refreshJobs();
    setStatus("就绪：结果均来自真实执行，未预填任何成功状态");
  } catch (err) {
    setStatus("初始化失败：" + err.message, "error");
  }
})();
