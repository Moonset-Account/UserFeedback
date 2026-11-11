/* 控制台前端：配置输入、运行任务、轮询状态、展示证据与逐格错误。 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let scenarios = [];
  let currentTaskId = null;
  let pollTimer = null;

  const KIND_LABEL = {
    id_text: "标识/前导零", decimal: "十进制数", date_text: "日期文本",
    text: "文本", empty: "空字符串", missing: "缺失",
  };
  const STRATEGY_LABEL = {
    skip_missing: "跳过(缺失)", clear_empty: "聚焦清空",
    type_cell: "逐格键盘输入", paste_multiline: "模态真实粘贴",
  };

  async function loadScenarios() {
    const resp = await fetch("/api/scenarios");
    scenarios = await resp.json();
    const sel = $("scenario-select");
    sel.innerHTML = "";
    scenarios.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.name;
      opt.textContent = `${s.title}（${s.name}）`;
      sel.appendChild(opt);
    });
    // 默认选中完整验收场景
    sel.value = "acceptance-full";
    renderScenarioTraits();
  }

  function renderScenarioTraits() {
    const s = scenarios.find((x) => x.name === $("scenario-select").value);
    if (!s) return;
    $("scenario-desc").textContent = s.description;
    const ul = $("scenario-traits");
    ul.innerHTML = "";
    const traits = [
      `隐藏列：${s.hidden_columns.join("、") || "无"}`,
      `只读格：${s.readonly_cells.length
        ? s.readonly_cells.map((c) => `第${c.row + 1}行·${c.column}`).join("；")
        : "无"}`,
      "冻结列：实验编号、样本编码（横向滚动保持可见）",
      "合并标题：标识信息 / 测量结果 / 管理字段",
    ];
    traits.forEach((t) => {
      const li = document.createElement("li");
      li.textContent = t;
      ul.appendChild(li);
    });
  }

  async function refreshTasks() {
    const resp = await fetch("/api/tasks");
    const tasks = await resp.json();
    const box = $("task-list");
    if (!tasks.length) {
      box.innerHTML = '<p class="empty-tip">暂无任务，点击左侧「运行示例」开始。</p>';
      return;
    }
    box.innerHTML = "";
    tasks.forEach((t) => {
      const card = document.createElement("div");
      card.className = "task-card" + (t.task_id === currentTaskId ? " active" : "");
      const running = ["queued", "collecting", "planning", "automating"].includes(t.status);
      const okBadge = t.status === "done"
        ? `<span class="badge ok-${t.ok}">${t.ok ? "整体通过" : "存在失败格"}</span>`
        : t.status === "failed" ? '<span class="badge failed">任务失败</span>' : "";
      card.innerHTML = `
        <span class="t-name">${t.scenario}</span>
        <span class="badge ${t.status}">${statusLabel(t.status)}</span>
        ${okBadge}
        <span class="t-time">${t.created_at}</span>`;
      card.addEventListener("click", () => selectTask(t.task_id));
      box.appendChild(card);
    });
  }

  function statusLabel(s) {
    return { queued: "排队", collecting: "采集中", planning: "规划中",
      automating: "浏览器录入中", done: "完成", failed: "失败" }[s] || s;
  }

  async function selectTask(taskId) {
    currentTaskId = taskId;
    await refreshTasks();
    const resp = await fetch(`/api/tasks/${taskId}`);
    const task = await resp.json();
    renderDetail(task);
  }

  function renderDetail(task) {
    $("task-detail").hidden = false;
    $("detail-title").textContent = `任务 ${task.task_id} · 场景 ${task.scenario}`;
    const r = task.result;
    if (r) {
      $("detail-summary").innerHTML = `
        <div class="stat s-total"><div class="num">${r.total}</div><div class="lbl">总格数</div></div>
        <div class="stat s-success"><div class="num">${r.succeeded}</div><div class="lbl">成功</div></div>
        <div class="stat s-rejected"><div class="num">${r.rejected}</div><div class="lbl">拒绝(保真)</div></div>
        <div class="stat s-failed"><div class="num">${r.failed}</div><div class="lbl">失败</div></div>
        <div class="stat s-skipped"><div class="num">${r.skipped}</div><div class="lbl">跳过(缺失)</div></div>
        <div class="stat s-time"><div class="num">${r.elapsed_ms}<small>ms</small></div><div class="lbl">浏览器耗时</div></div>`;
      $("detail-evidence").textContent = JSON.stringify(r.evidence, null, 2);
      renderOutcomes(r.outcomes);
      $("detail-screenshot").src = `/api/evidence/${task.task_id}/screenshot?t=${Date.now()}`;
    } else {
      $("detail-summary").innerHTML = "";
      $("detail-evidence").textContent = task.error
        ? `任务失败：${task.error}` : "任务尚未产出结果…";
      document.querySelector("#outcome-table tbody").innerHTML = "";
      $("detail-screenshot").removeAttribute("src");
    }
    $("detail-messages").innerHTML = (task.messages || [])
      .map((m) => `<div>[${m.at}] ${escapeHtml(m.text)}</div>`).join("");
  }

  function renderOutcomes(outcomes) {
    const filter = $("outcome-filter").value;
    const tbody = document.querySelector("#outcome-table tbody");
    tbody.innerHTML = "";
    outcomes
      .filter((o) => filter === "all" || o.status === filter)
      .forEach((o) => {
        const tr = document.createElement("tr");
        tr.className = "st-" + o.status;
        tr.innerHTML = `
          <td>${o.row_index + 1}</td>
          <td>${escapeHtml(o.column_key)}</td>
          <td>${KIND_LABEL[o.expected_kind] || o.expected_kind}</td>
          <td class="mono">${preview(o.expected_text)}</td>
          <td>${STRATEGY_LABEL[o.strategy] || "—"}${o.dangerous ? " <b style='color:#b91c1c'>⚠公式样式</b>" : ""}</td>
          <td><span class="status-pill ${o.status}">${statusLabel2(o.status)}</span></td>
          <td>${escapeHtml(o.detail || "")}</td>
          <td class="mono">${preview(o.actual_text)}</td>`;
        tbody.appendChild(tr);
      });
  }

  function statusLabel2(s) {
    return { success: "成功", rejected: "拒绝", failed: "失败", skipped: "跳过" }[s] || s;
  }
  function preview(v) {
    if (v === null || v === undefined) return "∅ 缺失";
    if (v === "") return "「」空串";
    return escapeHtml(String(v).replace(/\t/g, "⇥").replace(/\r/g, "␍").replace(/\n/g, "⏎"));
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---------- 运行控制 ----------
  function setBusy(on) {
    $("run-demo").disabled = on;
    $("run-scenario").disabled = on;
  }

  async function runDemo() {
    setBusy(true);
    const prog = $("demo-progress");
    prog.hidden = false;
    prog.innerHTML = '<div class="step">◌ 已提交：依次运行 demo-normal → demo-edge …</div>';
    await fetch("/api/demo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ headless: $("headless-toggle").checked }),
    });
    pollUntilDone(["demo-normal", "demo-edge"], prog);
  }

  async function runScenario() {
    setBusy(true);
    const scenario = $("scenario-select").value;
    const resp = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario, headless: $("headless-toggle").checked }),
    });
    const data = await resp.json();
    if (data.error) { alert(data.error); setBusy(false); return; }
    pollUntilDone([scenario], null, data.task_id);
  }

  function pollUntilDone(scenarioNames, progEl, specificTask) {
    let ticks = 0;
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      ticks += 1;
      await refreshTasks();
      const resp = await fetch("/api/tasks");
      const tasks = await resp.json();
      const mine = tasks.filter((t) => scenarioNames.includes(t.scenario));
      if (progEl) {
        progEl.innerHTML = scenarioNames.map((name) => {
          const t = mine.find((x) => x.scenario === name);
          if (!t) return `<div class="step">◌ ${name} 排队中…</div>`;
          const cls = t.status === "failed" || (t.status === "done" && t.ok === false)
            ? "fail" : t.status === "done" ? "done" : "";
          const mark = t.status === "done" ? "✓" : t.status === "failed" ? "✗" : "◌";
          return `<div class="step ${cls}">${mark} ${name}：${statusLabel(t.status)}</div>`;
        }).join("");
      }
      const allDone = mine.length >= scenarioNames.length &&
        mine.every((t) => ["done", "failed"].includes(t.status));
      if (allDone || ticks > 120) {
        clearInterval(pollTimer);
        setBusy(false);
        const latest = mine.sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
        if (latest) await selectTask(latest.task_id);
      }
    }, 700);
  }

  // ---------- 绑定 ----------
  document.addEventListener("DOMContentLoaded", async () => {
    await loadScenarios();
    await refreshTasks();
    $("scenario-select").addEventListener("change", renderScenarioTraits);
    $("run-demo").addEventListener("click", runDemo);
    $("run-scenario").addEventListener("click", runScenario);
    $("refresh-tasks").addEventListener("click", refreshTasks);
    $("close-detail").addEventListener("click", () => { $("task-detail").hidden = true; });
    $("outcome-filter").addEventListener("change", async () => {
      if (currentTaskId) await selectTask(currentTaskId);
    });
  });
})();
