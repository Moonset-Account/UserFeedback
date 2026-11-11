/* 公开实验 ZIP 选择性采集系统 —— 前端逻辑（无框架、无外部依赖） */

const state = {
  meta: null,
  tasks: [],
  activeTaskId: null,
  pollTimer: null,
};

const $ = (id) => document.getElementById(id);

async function fetchJson(url, options) {
  const resp = await fetch(url, options);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

// ----------------------------------------------------------------------
// 初始化
// ----------------------------------------------------------------------
async function init() {
  state.meta = await fetchJson("/api/meta");
  $("protocol-subset").textContent = "支持协议子集：" + state.meta.protocol_subset;
  $("experiment-id").value = state.meta.experiment_id;
  renderScenarioList();
  refreshTaskList();
  state.pollTimer = setInterval(refreshTaskList, 800);

  $("run-task").addEventListener("click", () => createTaskFromForm($("zip-url").value.trim()));
  $("run-demo").addEventListener("click", runDemo);
}

function sourceUrl(urlPath) {
  return state.meta.source_base_url + urlPath;
}

function renderScenarioList() {
  const box = $("scenario-list");
  box.innerHTML = "";
  for (const sc of state.meta.scenarios) {
    const item = document.createElement("div");
    item.className = "scenario-item";
    const expectLabel = { success: "应成功", failed: "应失败", rejected: "含拒绝" }[sc.expect] || sc.expect;
    item.innerHTML = `
      <div class="name">${sc.key} <span class="badge ${sc.expect === "success" ? "ok" : sc.expect === "failed" ? "bad" : "warn"}">${expectLabel}</span></div>
      <div class="desc">${sc.description}</div>
    `;
    const btn = document.createElement("button");
    btn.textContent = "使用此场景";
    btn.addEventListener("click", () => fillScenario(sc));
    item.appendChild(btn);
    box.appendChild(item);
  }
}

function fillScenario(sc) {
  const url = sc.key === "mutation"
    ? sourceUrl(state.meta.mutation_endpoint)
    : sourceUrl("/packages/" + sc.filename);
  $("zip-url").value = url;
  $("wanted-members").value = sc.wanted_members.join("\n");
}

// ----------------------------------------------------------------------
// 任务创建与轮询
// ----------------------------------------------------------------------
async function createTaskFromForm(url, label) {
  const members = $("wanted-members").value
    .split("\n").map((s) => s.trim()).filter(Boolean);
  if (!url) {
    alert("请先填写 ZIP 公开地址，或在下方选择一个场景。");
    return null;
  }
  if (members.length === 0) {
    alert("请至少指定一个要采集的成员名（每行一个）。");
    return null;
  }
  const payload = {
    url,
    wanted_members: members,
    experiment_id: $("experiment-id").value.trim(),
    label: label || "",
  };
  const task = await fetchJson("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await refreshTaskList();
  state.activeTaskId = task.id;
  renderTabs();
  return task.id;
}

async function refreshTaskList() {
  const data = await fetchJson("/api/tasks");
  state.tasks = data.tasks;
  renderTabs();
  if (state.activeTaskId) renderDetail(state.activeTaskId);
}

function renderTabs() {
  const tabs = $("task-tabs");
  tabs.innerHTML = "";
  for (const task of state.tasks.slice(0, 10)) {
    const tab = document.createElement("button");
    tab.className = "task-tab" + (task.id === state.activeTaskId ? " active" : "");
    const label = task.params.label || shortName(task.params.url);
    tab.textContent = `${label} · ${task.status}`;
    tab.addEventListener("click", () => {
      state.activeTaskId = task.id;
      renderTabs();
      renderDetail(task.id);
    });
    tabs.appendChild(tab);
  }
}

function shortName(url) {
  const seg = url.split("/").pop() || url;
  return seg.replace(".zip", "");
}

// ----------------------------------------------------------------------
// 结果渲染
// ----------------------------------------------------------------------
function renderDetail(taskId) {
  const task = state.tasks.find((t) => t.id === taskId);
  const box = $("task-detail");
  if (!task) {
    box.innerHTML = "";
    return;
  }
  const summary = $("summary");
  if (task.status !== "finished") {
    summary.className = "summary";
    summary.innerHTML = `任务 <span class="pill">${task.id}</span> 正在执行（${task.status}）……`;
    box.innerHTML = `<div class="log-box">${escapeHtml(task.logs.join("\n"))}</div>`;
    return;
  }

  const report = task.report;
  if (!report) {
    summary.className = "summary failed";
    summary.textContent = "任务异常：" + (task.error || "未知错误");
    box.innerHTML = `<div class="log-box">${escapeHtml(task.logs.join("\n"))}</div>`;
    return;
  }

  renderSummary(report);
  box.innerHTML = "";

  if (report.error) {
    const err = document.createElement("div");
    err.className = "detail-block";
    err.open = true;
    err.innerHTML = `
      <summary>具体错误（必须可见、可追溯）</summary>
      <div class="block-body">
        <div class="error-box">
          <strong>错误代码：</strong>${escapeHtml(report.error.code)}<br/>
          <strong>说明：</strong>${escapeHtml(report.error.message)}
        </div>
      </div>`;
    box.appendChild(err);
  }

  box.appendChild(renderTraffic(report));
  box.appendChild(renderRangeTable(report));
  box.appendChild(renderRecords(report));
  box.appendChild(renderRejections(report));
  box.appendChild(renderLogs(task));
}

function renderSummary(report) {
  const summary = $("summary");
  summary.className = "summary " + (report.status === "success" ? "success" : report.status === "failed" ? "failed" : "rejected");
  const traffic = report.traffic || {};
  const ratio = traffic.downloaded_ratio != null
    ? (traffic.downloaded_ratio * 100).toFixed(2) + "%" : "—";
  let title;
  if (report.status === "success") title = "✅ 采集成功，全部选中成员通过校验并入册";
  else if (report.status === "failed") title = "❌ 采集失败：没有任何成员进入正式实验记录";
  else title = "⚠️ 采集完成但存在被拒绝成员（拒绝项未入册）";
  summary.innerHTML = `
    <div><strong>${title}</strong></div>
    <div class="stat-line">
      <span>正式记录：${report.records.length} 条成员</span>
      <span>拒绝成员：${report.rejected_members.length} 个</span>
      <span>Range 请求：${traffic.request_count ?? "—"} 次</span>
      <span>实际下载：${fmtBytes(traffic.downloaded_bytes)}</span>
      <span>整包大小：${fmtBytes(traffic.remote_total_bytes)}</span>
      <span>下载占比：${ratio}</span>
      <span>耗时：${report.elapsed_ms} ms</span>
    </div>`;
}

function renderTraffic(report) {
  const block = document.createElement("details");
  block.className = "detail-block";
  block.open = true;
  const t = report.traffic || {};
  block.innerHTML = `
    <summary>流量证据：只取所需字节，未下载整个大包</summary>
    <div class="block-body">
      <table>
        <tr><th>指标</th><th>值</th></tr>
        <tr><td>远程文件总大小</td><td class="num">${fmtBytes(t.remote_total_bytes)}</td></tr>
        <tr><td>实际请求并下载的字节</td><td class="num">${fmtBytes(t.downloaded_bytes)}</td></tr>
        <tr><td>节省的字节</td><td class="num">${fmtBytes(t.saved_bytes)}</td></tr>
        <tr><td>下载占整包比例</td><td class="num">${t.downloaded_ratio != null ? (t.downloaded_ratio * 100).toFixed(3) + " %" : "—"}</td></tr>
        <tr><td>Range 请求次数</td><td class="num">${t.request_count}</td></tr>
        <tr><td>源包 ETag 指纹</td><td class="code">${escapeHtml(report.remote?.etag || "未提供")}</td></tr>
        <tr><td>容器格式</td><td>${report.remote?.is_zip64 ? "ZIP64" : "ZIP"}，目录成员数 ${report.remote?.total_entries ?? "—"}</td></tr>
      </table>
    </div>`;
  return block;
}

function renderRangeTable(report) {
  const block = document.createElement("details");
  block.className = "detail-block";
  const rows = (report.range_requests || [])
    .map((r) => `
      <tr>
        <td class="num">${r.index}</td>
        <td class="num code">${r.start}</td>
        <td class="num code">${r.end_inclusive}</td>
        <td class="num">${r.requested_bytes}</td>
        <td class="num">${r.bytes_returned}</td>
        <td class="num">${r.response_status}</td>
        <td class="code">${escapeHtml(r.content_range || "")}</td>
        <td class="code">${escapeHtml(r.etag || "")}</td>
      </tr>`)
    .join("");
  block.innerHTML = `
    <summary>实际请求的字节区间（${(report.range_requests || []).length} 次 Range 请求，可逐条核对）</summary>
    <div class="block-body">
      <table>
        <thead><tr><th class="num">#</th><th class="num">起始字节</th><th class="num">结束字节</th>
        <th class="num">请求字节</th><th class="num">实收字节</th><th class="num">状态</th>
        <th>Content-Range</th><th>ETag</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="8">无请求记录</td></tr>'}</tbody>
      </table>
    </div>`;
  return block;
}

function renderRecords(report) {
  const block = document.createElement("details");
  block.className = "detail-block";
  const rows = report.records
    .map((rec) => `
      <tr>
        <td class="code">${escapeHtml(rec.member_name)}</td>
        <td class="num">v${rec.schema_version}</td>
        <td class="num">${rec.accepted_observation_count}</td>
        <td><span class="badge ok">${escapeHtml(rec.status)}</span></td>
        <td class="code">${escapeHtml(rec.observations.map((o) => o.observation_id).slice(0, 4).join(", "))}${rec.observations.length > 4 ? " …" : ""}</td>
      </tr>`)
    .join("");
  block.innerHTML = `
    <summary>正式实验记录（${report.records.length} 个成员，仅含通过全部校验的成员）</summary>
    <div class="block-body">
      <table>
        <thead><tr><th>成员名</th><th class="num">对象版本</th><th class="num">观测条数</th><th>状态</th><th>观测 ID 摘录</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5">无正式记录</td></tr>'}</tbody>
      </table>
    </div>`;
  return block;
}

function renderRejections(report) {
  const block = document.createElement("details");
  block.className = "detail-block";
  if (report.rejected_members.length) block.open = true;
  const items = report.rejected_members
    .map((r) => `<div class="reject-item"><strong class="code">${escapeHtml(r.member_name)}</strong><br/>${escapeHtml(r.reason)}</div>`)
    .join("");
  block.innerHTML = `
    <summary>被拒绝的成员（${report.rejected_members.length} 个，不进入正式记录）</summary>
    <div class="block-body">${items || "无拒绝项。"}</div>`;
  return block;
}

function renderLogs(task) {
  const block = document.createElement("details");
  block.className = "detail-block";
  block.innerHTML = `
    <summary>执行日志</summary>
    <div class="block-body"><div class="log-box">${escapeHtml(task.logs.join("\n"))}</div></div>`;
  return block;
}

// ----------------------------------------------------------------------
// 运行示例：正常 + 边界，目标 15 秒内完成
// ----------------------------------------------------------------------
async function runDemo() {
  const btn = $("run-demo");
  const status = $("demo-status");
  btn.disabled = true;
  try {
    const scenarios = state.meta.scenarios;
    const normal = scenarios.find((s) => s.key === "normal_small");
    const edge = scenarios.find((s) => s.key === "crc_bad");

    status.textContent = "示例 1/2：运行正常场景 normal_small …";
    await runScenarioTask(normal, "示例-正常");

    status.textContent = "示例 2/2：运行边界场景 crc_bad（CRC 错误）…";
    await runScenarioTask(edge, "示例-边界(CRC错误)");

    status.textContent = "两个示例均已结束，结果保留在右侧，可切换标签查看。";
  } catch (exc) {
    status.textContent = "示例运行出错：" + exc.message;
  } finally {
    btn.disabled = false;
  }
}

async function runScenarioTask(scenario, label) {
  const url = scenario.key === "mutation"
    ? sourceUrl(state.meta.mutation_endpoint)
    : sourceUrl("/packages/" + scenario.filename);
  const payload = {
    url,
    wanted_members: scenario.wanted_members,
    experiment_id: state.meta.experiment_id,
    label,
  };
  const task = await fetchJson("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.activeTaskId = task.id;
  await waitForTask(task.id);
}

function waitForTask(taskId) {
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      try {
        const task = await fetchJson("/api/tasks/" + taskId);
        if (task.status === "finished") {
          clearInterval(timer);
          await refreshTaskList();
          resolve(task);
        }
      } catch (exc) {
        clearInterval(timer);
        reject(exc);
      }
    }, 300);
  });
}

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB"];
  let value = n;
  let unit = "B";
  for (const u of units) {
    value /= 1024;
    unit = u;
    if (value < 1024) break;
  }
  return value.toFixed(2) + " " + unit;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

init().catch((exc) => {
  document.body.insertAdjacentHTML("afterbegin", `<div class="error-box">页面初始化失败：${escapeHtml(exc.message)}</div>`);
});
