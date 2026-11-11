/* 采集器前端：配置输入、运行任务、轮询结果、展开来源证据。
   结果全部来自后端真实执行，本文件不构造任何成功状态。 */
(function () {
  "use strict";

  const scenarioSelect = document.getElementById("scenario-select");
  const scenarioDesc = document.getElementById("scenario-desc");
  const backendSelect = document.getElementById("backend-select");
  const maxRefreshesInput = document.getElementById("max-refreshes");
  const resetFirstInput = document.getElementById("reset-first");
  const runBtn = document.getElementById("run-btn");
  const demoBtn = document.getElementById("run-demo");
  const clearBtn = document.getElementById("clear-btn");
  const banner = document.getElementById("status-banner");
  const runsList = document.getElementById("runs-list");

  const STATUS_TEXT = {
    success: "成功",
    failed_expired: "失败：链接持续失效，达到刷新上限",
    failed_version_changed: "失败：刷新后业务版本变化，已停止",
    failed_identity: "失败：业务身份校验不通过",
    failed_structure: "失败：内容结构/类型校验不通过",
    failed_acquire: "失败：无法取得下载地址",
    failed_transport: "失败：下载传输异常",
    failed_exception: "失败：采集器内部异常",
    running: "执行中",
    completed: "批次完成",
  };

  let scenarios = [];
  let pollTimer = null;
  let knownRuns = new Set();

  function setBanner(kind, text) {
    banner.className = "status-banner status-" + kind;
    banner.textContent = text;
  }

  function setBusy(busy) {
    runBtn.disabled = busy;
    demoBtn.disabled = busy;
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function statusBadge(status) {
    let cls = "badge-running";
    if (status === "success") cls = "badge-success";
    else if (status && status !== "running" && status !== "completed") cls = "badge-failed";
    return `<span class="badge ${cls}">${esc(STATUS_TEXT[status] || status)}</span>`;
  }

  async function loadScenarios() {
    const resp = await fetch("/api/scenarios");
    const data = await resp.json();
    scenarios = data.scenarios;
    scenarioSelect.innerHTML = scenarios.map((s) =>
      `<option value="${esc(s.id)}">${esc(s.id)} · ${esc(s.title)}${s.js_rendered ? "（JS 渲染）" : ""}</option>`
    ).join("");
    renderDesc();
  }

  function renderDesc() {
    const s = scenarios.find((x) => x.id === scenarioSelect.value);
    if (!s) return;
    scenarioDesc.textContent = s.description;
    // JS 渲染场景默认建议 Playwright
    if (s.js_rendered) backendSelect.value = "playwright";
    else if (backendSelect.value === "playwright" && !s.js_rendered) backendSelect.value = "http";
  }
  scenarioSelect.addEventListener("change", renderDesc);

  // ---------------------------------------------------------------- 运行
  async function startRun() {
    const body = {
      scenario_id: scenarioSelect.value,
      backend: backendSelect.value,
      max_refreshes: Number(maxRefreshesInput.value),
      reset_first: resetFirstInput.checked,
    };
    setBusy(true);
    setBanner("running", "采集任务已提交，正在真实执行…");
    try {
      const resp = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      await resp.json();
      startPolling();
    } catch (err) {
      setBusy(false);
      setBanner("failed", "任务提交失败：" + err.message);
    }
  }

  async function startDemo() {
    setBusy(true);
    setBanner("running", "示例批次执行中：正常场景 → 边界场景，预计 15 秒内完成…");
    try {
      const resp = await fetch("/api/demo", { method: "POST" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      await resp.json();
      startPolling();
    } catch (err) {
      setBusy(false);
      setBanner("failed", "示例启动失败：" + err.message);
    }
  }

  runBtn.addEventListener("click", startRun);
  demoBtn.addEventListener("click", startDemo);
  clearBtn.addEventListener("click", async () => {
    await fetch("/api/runs/clear", { method: "POST" });
    knownRuns = new Set();
    runsList.innerHTML = "";
    setBanner("idle", "记录已清空。尚未运行——结果来自真实执行，页面不预填成功。");
  });

  // ---------------------------------------------------------------- 轮询
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(refreshRuns, 700);
    refreshRuns();
  }

  async function refreshRuns() {
    const resp = await fetch("/api/runs");
    const data = await resp.json();
    const runs = data.runs || [];
    renderRuns(runs);

    const anyRunning = runs.some((r) => r.status === "running");
    if (!anyRunning) {
      clearInterval(pollTimer);
      pollTimer = null;
      setBusy(false);
      const latest = runs[0];
      if (latest) {
        if (latest.status === "success" || latest.status === "completed") {
          setBanner("success", "最近任务已完成：" + latest.message);
        } else {
          setBanner("failed", "最近任务结束（未成功）：" + latest.message);
        }
      }
    }
  }

  function renderRuns(runs) {
    for (const run of runs) {
      const existing = document.getElementById("run-" + run.run_id);
      if (existing) {
        updateRunCard(existing, run);
      } else {
        runsList.insertAdjacentHTML("beforeend", runCardHtml(run));
        knownRuns.add(run.run_id);
        const card = document.getElementById("run-" + run.run_id);
        card.querySelector(".run-summary").addEventListener("click", () => {
          card.classList.toggle("open");
        });
      }
    }
  }

  function updateRunCard(card, run) {
    card.querySelector(".run-title").innerHTML =
      `${esc(run.scenario_id)} <span class="mono">(${esc(run.backend_name)})</span> ${statusBadge(run.status)}`;
    card.querySelector(".run-meta").textContent =
      `尝试 ${run.attempts} 次 · 刷新 ${run.refreshes}/${run.max_refreshes} · ${run.duration_ms} ms · ${run.message || ""}`;
    const detail = card.querySelector(".run-detail");
    detail.innerHTML = detailHtml(run);
  }

  function runCardHtml(run) {
    return `
    <div class="run-card" id="run-${esc(run.run_id)}">
      <div class="run-summary">
        <div class="run-title">${esc(run.scenario_id)} <span class="mono">(${esc(run.backend_name)})</span> ${statusBadge(run.status)}</div>
        <span class="caret">点击展开证据 ▾</span>
        <div class="run-meta">尝试 ${run.attempts} 次 · 刷新 ${run.refreshes}/${run.max_refreshes} · ${run.duration_ms} ms · ${esc(run.message || "")}</div>
      </div>
      <div class="run-detail">${detailHtml(run)}</div>
    </div>`;
  }

  function detailHtml(run) {
    const validation = renderValidation(run.validation);
    const timeline = (run.events || []).map((ev) => `
      <li class="ev-${esc(ev.kind)}">
        <span class="ev-kind">#${ev.seq} ${esc(ev.kind)}</span>
        <span class="ev-msg">${esc(ev.message)}</span>
        ${ev.evidence ? `<div class="ev-evidence">${esc(JSON.stringify(ev.evidence, null, 2))}</div>` : ""}
      </li>`).join("");
    return `
      <div class="detail-section">
        <h4>执行摘要</h4>
        <dl class="kv">
          <dt>运行编号</dt><dd class="mono">${esc(run.run_id)}</dd>
          <dt>最终状态</dt><dd>${esc(STATUS_TEXT[run.status] || run.status)}</dd>
          <dt>下载尝试</dt><dd>${run.attempts} 次</dd>
          <dt>链接刷新</dt><dd>${run.refreshes} / 上限 ${run.max_refreshes}</dd>
          <dt>保存路径</dt><dd class="mono">${esc(run.saved_path || "（未保存）")}</dd>
          <dt>结论说明</dt><dd>${esc(run.message || "")}</dd>
        </dl>
      </div>
      ${validation}
      <div class="detail-section">
        <h4>过程时间线与来源证据</h4>
        <ul class="timeline">${timeline || "<li>暂无事件</li>"}</ul>
      </div>`;
  }

  function renderValidation(v) {
    if (!v || !v.checks || !v.checks.length) {
      return `<div class="detail-section"><h4>三层内容校验</h4><p class="field-hint">本次运行未进入内容校验。</p></div>`;
    }
    const rows = v.checks.map((c) => `
      <div class="check-row">
        <span class="check-icon ${c.passed ? "check-pass" : "check-fail"}">${c.passed ? "✓" : "✗"}</span>
        <span class="check-name">${esc(c.name)}</span>
        <span>${esc(c.detail)}</span>
      </div>`).join("");
    const manifest = v.manifest
      ? `<div class="ev-evidence">${esc(JSON.stringify(v.manifest, null, 2))}</div>` : "";
    return `
      <div class="detail-section">
        <h4>三层内容校验（类型 → 结构 → 业务身份）· 总体：${v.passed ? "通过" : "未通过"}</h4>
        ${rows}
        ${manifest}
      </div>`;
  }

  loadScenarios().catch((err) => setBanner("failed", "场景加载失败：" + err.message));
  startPolling();
})();
