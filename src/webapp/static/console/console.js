/* RPA 控制台前端：所有结果均来自后端真实执行返回，不做任何预填。 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  let scenarios = [];
  let selectedFile = null;
  let activeRunId = null;

  async function loadScenarios() {
    const resp = await fetch("/api/scenarios");
    const body = await resp.json();
    scenarios = body.scenarios;
    renderScenarios();
  }

  function renderScenarios() {
    const groups = {
      "demo-normal": "快速演示 · 正常场景",
      "demo-boundary": "快速演示 · 边界场景",
      full: "完整验收场景（按需运行）",
    };
    const box = $("#scenario-groups");
    box.innerHTML = "";
    Object.entries(groups).forEach(([key, title]) => {
      const h = document.createElement("div");
      h.className = "scenario-group-title";
      h.textContent = title;
      box.appendChild(h);
      scenarios
        .filter((s) => s.group === key)
        .forEach((s) => {
          const label = document.createElement("label");
          label.className = "scenario-item";
          label.dataset.file = s.file;
          label.innerHTML = `
            <input type="radio" name="scenario" />
            <span>
              <div>${s.description}</div>
              <div class="meta">${s.file}</div>
            </span>`;
          label.addEventListener("click", () => {
            selectedFile = s.file;
            document.querySelectorAll(".scenario-item").forEach((n) => n.classList.remove("selected"));
            label.classList.add("selected");
            label.querySelector("input").checked = true;
          });
          box.appendChild(label);
        });
    });
    // 默认选中正常场景。
    const first = box.querySelector(".scenario-item");
    if (first) first.click();
  }

  async function postRun(payload) {
    const resp = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json();
    if (!resp.ok || !body.ok) {
      const msg = body.error ? JSON.stringify(body.error) : body.error || "启动失败";
      throw new Error(msg);
    }
    return body.run_id;
  }

  function setRunning(text) {
    const btn = $("#run-demo");
    btn.disabled = true;
    btn.textContent = text;
    $("#run-summary .pill").className = "pill running";
    $("#run-summary .pill").textContent = text;
  }

  function resetDemoButton() {
    const btn = $("#run-demo");
    btn.disabled = false;
    btn.textContent = "▶ 运行示例（正常 + 边界，15 秒内）";
  }

  // 运行示例：依次执行一个正常 + 一个边界场景，两者结果都保留。
  async function runDemo() {
    setRunning("示例运行中…");
    const headless = $("#headless-toggle").checked;
    const demoFiles = scenarios
      .filter((s) => s.group.startsWith("demo-"))
      .map((s) => s.file);
    const runIds = [];
    const started = Date.now();
    try {
      for (const file of demoFiles) {
        const id = await postRun({ task_file: file, headless });
        runIds.push(id);
        await waitRun(id);
      }
    } catch (exc) {
      $("#run-timing").textContent = "启动失败：" + exc.message;
      resetDemoButton();
      return;
    }
    const elapsed = ((Date.now() - started) / 1000).toFixed(1);
    await refreshRuns();
    const all = await Promise.all(runIds.map((id) => fetchRun(id)));
    const failed = all.filter((r) => r.status === "failed");
    // 设计上边界场景应当 failed（禁用停止），正常场景应当 success。
    const ok = all.some((r) => r.status === "success") && all.some((r) => r.status === "failed");
    const pill = $("#run-summary .pill");
    pill.className = "pill " + (ok ? "success" : "failed");
    pill.textContent = ok
      ? `示例完成：${all.length - failed.length} 成功 / ${failed.length} 按预期停止（边界）`
      : "示例结果不符合预期，请查看错误";
    $("#run-timing").textContent = `总耗时 ${elapsed} 秒（目标 ≤ 15 秒），两次运行记录均已保留`;
    resetDemoButton();
    if (activeRunId) renderEvidence(activeRunId);
  }

  async function runSelected() {
    if (!selectedFile) return;
    const id = await postRun({ task_file: selectedFile, headless: $("#headless-toggle").checked });
    await waitRun(id);
    await refreshRuns();
  }

  async function runCustom() {
    let document;
    try {
      document = JSON.parse($("#custom-task").value);
    } catch (exc) {
      alert("自定义任务不是合法 JSON：" + exc.message);
      return;
    }
    const id = await postRun({
      task_document: document,
      headless: $("#headless-toggle").checked,
    });
    await waitRun(id);
    await refreshRuns();
  }

  function waitRun(runId) {
    return new Promise((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const record = await fetchRun(runId);
          if (record.status !== "running") {
            clearInterval(timer);
            resolve(record);
          }
        } catch (exc) {
          clearInterval(timer);
          reject(exc);
        }
      }, 700);
    });
  }

  async function fetchRun(runId) {
    const resp = await fetch(`/api/runs/${runId}`);
    return resp.json();
  }

  async function refreshRuns() {
    const resp = await fetch("/api/runs");
    const body = await resp.json();
    const list = $("#run-list");
    list.innerHTML = "";
    if (!body.runs.length) {
      list.innerHTML = '<li class="muted">暂无运行记录</li>';
      return;
    }
    body.runs.forEach((record) => {
      const li = document.createElement("li");
      li.className = "run-item" + (record.run_id === activeRunId ? " active" : "");
      const report = record.report;
      const duration = report
        ? ((report.finished_at - report.started_at)).toFixed(1) + "s"
        : "—";
      li.innerHTML = `
        <span class="status-dot ${record.status}"></span>
        <span>
          <div class="name">${escapeHtml(record.task_name)}</div>
          <div class="sub">${escapeHtml(record.source)} · ${record.run_id}</div>
        </span>
        <span class="sub">${record.status} · ${duration}</span>`;
      li.addEventListener("click", () => {
        activeRunId = record.run_id;
        refreshRuns().then(() => renderEvidence(record.run_id));
      });
      list.appendChild(li);
    });
    if (!activeRunId && body.runs.length) {
      activeRunId = body.runs[0].run_id;
      renderEvidence(activeRunId);
    }
  }

  async function renderEvidence(runId) {
    const record = await fetchRun(runId);
    const box = $("#evidence-box");
    box.innerHTML = "";
    if (record.status === "running") {
      box.innerHTML = '<p class="muted">该任务仍在执行中…</p>';
      return;
    }
    const report = record.report;
    if (!report) {
      box.innerHTML = `<div class="error-box">启动错误：${escapeHtml(JSON.stringify(record.error))}</div>`;
      return;
    }
    if (report.error) {
      const e = report.error;
      const err = document.createElement("div");
      err.className = "error-box";
      err.innerHTML = `<strong>停止原因（${escapeHtml(e.code)}）</strong><div>${escapeHtml(e.message)}</div>`;
      box.appendChild(err);
    }
    report.steps.forEach((step) => {
      const block = document.createElement("div");
      block.className = "step-block";
      const decision = step.decision;
      const stop = decision && decision.status && decision.status !== "unique";
      block.innerHTML = `
        <div class="step-head">
          <span class="step-idx">#${step.index + 1}</span>
          <span>${escapeHtml(step.action)}</span>
          <span class="sub">${escapeHtml(step.summary)}</span>
        </div>`;
      if (decision) {
        const reason = document.createElement("div");
        reason.className = "reason" + (stop ? " stop" : "");
        reason.textContent = "决策理由：" + decision.reason;
        block.appendChild(reason);
        if (decision.candidates && decision.candidates.length) {
          const det = document.createElement("details");
          det.className = "extra";
          det.open = !!stop;
          det.innerHTML = `<summary>候选命令（${decision.candidates.length}，按 id/路径识别，不按序号）</summary>`;
          const ul = document.createElement("ul");
          ul.className = "candidate-list";
          decision.candidates.forEach((c) => {
            const li = document.createElement("li");
            li.innerHTML = `<code>${escapeHtml(c.id)}</code> · ${escapeHtml(c.path.join(" > "))} · scope=${escapeHtml(c.scope || "-")} · enabled=${c.enabled}`;
            ul.appendChild(li);
          });
          det.appendChild(ul);
          block.appendChild(det);
        }
      }
      if (step.observed) appendKv(block, "观测值", JSON.stringify(step.observed));
      if (step.note) appendKv(block, "备注", step.note);
      if (step.screenshot) {
        const img = document.createElement("img");
        img.className = "shot";
        img.src = `/api/runs/${runId}/evidence/${step.screenshot}`;
        img.alt = step.screenshot;
        img.title = "点击新窗口查看大图";
        img.addEventListener("click", () => window.open(img.src, "_blank"));
        block.appendChild(img);
      }
      box.appendChild(block);
    });
    if (report.boundary_violations && report.boundary_violations.length) {
      const b = document.createElement("div");
      b.className = "boundary";
      b.textContent = "采集边界拦截：" + report.boundary_violations.join("；");
      box.appendChild(b);
    } else {
      const b = document.createElement("div");
      b.className = "boundary";
      b.textContent = "采集边界检查：未发现越界请求（仅访问 /lab/ 与 /lab/api/* 白名单）。";
      box.appendChild(b);
    }
  }

  function appendKv(parent, key, value) {
    const div = document.createElement("div");
    div.className = "kv";
    div.innerHTML = `${key}：<code>${escapeHtml(value)}</code>`;
    parent.appendChild(div);
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
    );
  }

  async function loadSelectedIntoEditor() {
    if (!selectedFile) return;
    const resp = await fetch(`/api/scenario-content?file=${encodeURIComponent(selectedFile)}`);
    const body = await resp.json();
    if (!body.ok) {
      alert("载入失败：" + body.error);
      return;
    }
    $("#custom-task").value = JSON.stringify(body.document, null, 2);
  }

  // 事件绑定
  $("#run-demo").addEventListener("click", runDemo);
  $("#run-custom").addEventListener("click", runCustom);
  $("#load-selected").addEventListener("click", loadSelectedIntoEditor);

  loadScenarios().then(refreshRuns);
  setInterval(refreshRuns, 4000);
})();
