// 中文操作入口前端逻辑：所有结果均来自后端真实执行的轮询数据，不预填任何成功结果。
const $ = (id) => document.getElementById(id);
let activeTaskId = null;
let pollTimer = null;
let demoStartTs = null;
let demoTimerInt = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options) {
  const resp = await fetch(path, options);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
  return body;
}

function badgeClass(state) {
  return { queued: "badge-idle", running: "badge-running", done: "badge-done", error: "badge-error" }[state] || "badge-idle";
}
function stateText(state) {
  return { queued: "排队中", running: "运行中", done: "已完成", error: "执行出错" }[state] || state;
}

function renderTaskList(tasks) {
  const box = $("task-list");
  if (!tasks.length) {
    box.innerHTML = '<p class="muted">尚无任务。点击“运行示例”或提交配置开始；结果均来自真实执行。</p>';
    return;
  }
  box.innerHTML = tasks.map((t) => {
    const r = t.result;
    const meta = r
      ? `唯一 ${r.observed_unique_count} ｜ 样本 ${r.sampling ? r.sampling.sample.length : 0} ｜ 请求 ${r.requests_used}/${r.budget}`
      : (t.fatal_error ? esc(t.fatal_error) : "等待执行数据…");
    return `<div class="task-item ${t.task_id === activeTaskId ? "active" : ""}" data-id="${t.task_id}">
      <div class="t-title"><span>${esc(t.label)}</span><span class="badge ${badgeClass(t.state)}">${stateText(t.state)}</span></div>
      <div class="t-meta">${esc(meta)}</div>
    </div>`;
  }).join("");
  box.querySelectorAll(".task-item").forEach((el) =>
    el.addEventListener("click", () => { activeTaskId = el.dataset.id; refresh(); }));
}

function renderResult(t) {
  const badge = $("status-badge");
  if (!t) {
    $("result-empty").hidden = false; $("result-body").hidden = true;
    badge.className = "badge badge-idle"; badge.textContent = "未运行";
    return;
  }
  badge.className = `badge ${badgeClass(t.state)}`;
  badge.textContent = stateText(t.state) + (t.result && t.result.stop_reason === "budget_exhausted" ? "（预算耗尽）" : "");
  if (t.state === "running" || t.state === "queued") {
    $("result-empty").hidden = true; $("result-body").hidden = false;
    $("declaration").className = "declaration";
    $("declaration").textContent = "任务运行中… " + (t.progress.map((p) =>
      p.stage === "page" ? `已扫第 ${p.page_index} 页（${p.requests_used} 次请求）` : p.stage).join("；") || "");
    return;
  }
  if (t.fatal_error || !t.result) {
    $("result-empty").hidden = true; $("result-body").hidden = false;
    $("declaration").className = "declaration limited";
    $("declaration").textContent = "任务执行失败：" + (t.fatal_error || "无结果数据");
    $("raw-json").textContent = JSON.stringify(t, null, 2);
    return;
  }

  const r = t.result;
  $("result-empty").hidden = true; $("result-body").hidden = false;
  $("st-raw").textContent = r.raw_entry_count;
  $("st-dup").textContent = r.duplicate_count;
  $("st-unique").textContent = r.observed_unique_count;
  $("st-sample").textContent = r.sampling ? r.sampling.sample.length : 0;
  $("st-requests").textContent = `${r.requests_used}/${r.budget}`;

  const limited = r.stop_reason !== "completed";
  $("declaration").className = "declaration" + (limited ? " limited" : "");
  $("declaration").textContent = "结论声明：" + r.declaration;

  const stop = r.scan.stop;
  $("scan-boundary").innerHTML =
    `扫描边界：停止原因 <b>${esc(stop.reason)}</b> ｜ 已扫页 <b>${stop.last_page_index}</b> ｜ ` +
    (stop.next_url_not_fetched
      ? `未抓取的下一页（边界）：<b>${esc(stop.next_url_not_fetched)}</b>`
      : "无未抓取页（已到末页）") +
    `<br>细节：${esc(stop.detail)}` +
    (r.sampling && r.sampling.full_population_taken ? "<br><b>样本量 ≥ 已观察总体：总体被全量收录，未发生替换。</b>" : "");

  // 最终样本 + 来源页
  const tbody = $("sample-table").querySelector("tbody");
  tbody.innerHTML = r.sample_sources.map((s, i) => `<tr>
    <td>${i + 1}</td><td>${esc(s.specimen_id)}</td><td>${esc(s.title || "")}</td>
    <td>第 ${s.source_page} 页</td>
    <td class="wrap"><a href="${esc(s.source_page_url)}" target="_blank" rel="noopener">${esc(s.source_page_url)}</a></td>
    <td>${s.detail_url ? `<a href="${esc(s.detail_url)}" target="_blank" rel="noopener">详情证据</a>` : "—"}</td>
  </tr>`).join("") || '<tr><td colspan="6" class="muted">无样本</td></tr>';

  // 入样替换过程
  const evBody = $("events-table").querySelector("tbody");
  const events = r.sampling ? r.sampling.events : [];
  evBody.innerHTML = events.map((e) => `<tr>
    <td>${e.step}</td><td>${esc(e.specimen_id)}</td><td>${e.draw.toFixed(6)}</td>
    <td class="action-${e.action}">${ { filled: "填充", replaced: "替换", skipped: "跳过" }[e.action] || e.action }</td>
    <td>${e.slot ?? "—"}</td><td>${esc(e.ejected_id || "—")}</td>
    <td class="wrap">[${e.reservoir_after.map(esc).join(", ")}]</td>
  </tr>`).join("") || '<tr><td colspan="7" class="muted">无事件</td></tr>';

  // 逐页证据
  const pgBody = $("pages-table").querySelector("tbody");
  pgBody.innerHTML = r.scan.pages.map((p) => `<tr>
    <td>${p.page_index}</td><td>${p.http_status}</td><td>${p.raw_count}</td>
    <td>${p.unique_on_page}</td><td>${p.duplicate_on_page}</td><td>${p.requests_used}</td>
    <td class="wrap">${esc(p.url)}</td>
  </tr>`).join("");

  $("raw-json").textContent = JSON.stringify(r, null, 2);
}

async function refresh() {
  try {
    const tasks = await api("/api/tasks");
    renderTaskList(tasks);
    const active = tasks.find((t) => t.task_id === activeTaskId) || tasks[tasks.length - 1];
    if (active && !activeTaskId) activeTaskId = active.task_id;
    renderResult(tasks.find((t) => t.task_id === activeTaskId) || null);
    if (demoStartTs) updateDemoTimer(tasks);
  } catch (err) {
    console.error(err);
  }
}

function updateDemoTimer(tasks) {
  const demoTasks = tasks.filter((t) => t.label && t.label.startsWith("示例"));
  const allDone = demoTasks.length >= 2 && demoTasks.every((t) => t.state === "done" || t.state === "error");
  const secs = ((Date.now() - demoStartTs) / 1000).toFixed(1);
  $("demo-timer").hidden = false;
  $("demo-timer").textContent = allDone ? `示例链完成，用时 ${secs}s（目标 ≤15s）` : `示例运行中 ${secs}s`;
  if (allDone) {
    clearInterval(demoTimerInt); demoTimerInt = null;
    $("run-demo").disabled = false;
  }
}

async function submitTask(payload, label) {
  const t = await api("/api/tasks", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  activeTaskId = t.task_id;
  refresh();
  return t;
}

$("task-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const payload = {
    start_url: $("start_url").value.trim(),
    seed: Number($("seed").value),
    sample_size: Number($("sample_size").value),
    request_budget: Number($("request_budget").value),
    transport: $("transport").value,
    label: $("label").value.trim() || "自定义任务",
  };
  try {
    await submitTask(payload);
  } catch (err) { alert("提交失败：" + err.message); }
});

$("run-demo").addEventListener("click", async () => {
  $("run-demo").disabled = true;
  try {
    await api("/api/demo", { method: "POST" });
    demoStartTs = Date.now();
    if (demoTimerInt) clearInterval(demoTimerInt);
    demoTimerInt = setInterval(refresh, 200);
    refresh();
  } catch (err) {
    alert("示例启动失败：" + err.message);
    $("run-demo").disabled = false;
  }
});

$("run-scenario").addEventListener("click", async () => {
  const name = $("scenario-select").value;
  try {
    const t = await api(`/api/scenarios/${name}`, { method: "POST" });
    activeTaskId = t.task_id;
    refresh();
  } catch (err) { alert("场景启动失败：" + err.message); }
});

$("run-freq").addEventListener("click", async () => {
  const box = $("freq-result");
  box.innerHTML = '<p class="muted">检验运行中（数千次纯内存抽样）…</p>';
  try {
    const r = await api("/api/frequency", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        population_size: Number($("fq-pop").value),
        sample_size: Number($("fq-k").value),
        trials: Number($("fq-trials").value),
      }),
    });
    const minF = Math.min(...r.frequencies.map((f) => f.frequency));
    const maxF = Math.max(...r.frequencies.map((f) => f.frequency));
    box.innerHTML = `
      <p>卡方统计量 <b>${r.chi_square.toFixed(3)}</b>（df=${r.params.df}，α=0.05 临界值 ${r.chi2_critical_0_05_df24 ?? "—"}）：
        <span class="${r.passed_at_0_05 ? "freq-pass" : "freq-fail"}">${r.passed_at_0_05 ? "通过（未检出显著不均匀）" : "未通过"}</span></p>
      <p>理论入样概率 p=k/M=${r.theoretical_probability.toFixed(4)}；实测频率区间 [${minF.toFixed(4)}, ${maxF.toFixed(4)}]；每次期望计数 ${r.expected_count_each.toFixed(1)}。</p>
      <p>固定种子复现核对：${r.pinned_runs.map((p) => `种子 ${p.seed} → [${p.sample.join(", ")}]`).join("　")}</p>
      <div class="freq-disclaimer">${esc(r.disclaimer)}</div>`;
  } catch (err) {
    box.innerHTML = `<p class="freq-fail">检验失败：${esc(err.message)}</p>`;
  }
});

async function init() {
  const meta = await api("/api/meta");
  $("source-hint").textContent = "源站基址：" + meta.source_base_url + " ｜ 支持协议子集：" + meta.protocol_subset.join("；");
  const sel = $("scenario-select");
  Object.entries(meta.scenarios).forEach(([name, sc]) => {
    const opt = document.createElement("option");
    opt.value = name; opt.textContent = sc.label; sel.appendChild(opt);
  });
  pollTimer = setInterval(refresh, 700);
  refresh();
}
init();
