// 实验通知只读采集器前端逻辑（原生 JS，无构建步骤）
const $ = (id) => document.getElementById(id);
const scenarioSelect = $("scenario-select");
const runStatus = $("run-status");
let currentRunId = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function init() {
  const res = await fetch("/api/scenarios");
  const scenarios = await res.json();
  scenarioSelect.innerHTML = scenarios.map((s) =>
    `<option value="${esc(s.key)}">${esc(s.key)} — ${esc(s.title)}</option>`).join("");
  await refreshRuns();
}

function setBusy(busy, text) {
  for (const id of ["run-scene", "run-demo", "run-all"]) $(id).disabled = busy;
  runStatus.className = "run-status";
  runStatus.textContent = text || "";
}

async function postJson(url) {
  const res = await fetch(url, { method: "POST" });
  return res.json();
}

$("run-scene").addEventListener("click", async () => {
  const scenario = scenarioSelect.value;
  setBusy(true, `正在运行场景 ${scenario} …`);
  try {
    const res = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario }),
    });
    const out = await res.json();
    if (!out.ok) throw new Error(out.error || "运行失败");
    setBusy(false, `完成：${out.run_id}（${out.duration_ms} ms，状态 ${out.status}）`);
    runStatus.classList.add("ok");
    await refreshRuns();
    await openRun(out.run_id);
  } catch (err) {
    setBusy(false, `运行出错：${err.message}`);
    runStatus.classList.add("error");
  }
});

$("run-demo").addEventListener("click", async () => {
  setBusy(true, "运行示例：normal_basic + bad_attachment …");
  try {
    const out = await postJson("/api/run-demo");
    if (!out.ok) throw new Error(out.error || "运行失败");
    const summary = out.results.map((r) =>
      `${r.scenario}=${r.status}(${r.duration_ms}ms)`).join("，");
    setBusy(false, `示例完成，共 ${out.total_ms} ms：${summary}`);
    runStatus.classList.add("ok");
    await refreshRuns();
    await openRun(out.results[out.results.length - 1].run_id);
  } catch (err) {
    setBusy(false, `示例出错：${err.message}`);
    runStatus.classList.add("error");
  }
});

$("run-all").addEventListener("click", async () => {
  setBusy(true, "运行全部 5 个验收场景 …");
  try {
    const out = await postJson("/api/run-all");
    if (!out.ok) throw new Error(out.error || "运行失败");
    const summary = out.results.map((r) => `${r.scenario}=${r.status}`).join("，");
    setBusy(false, `全部完成，共 ${out.total_ms} ms：${summary}`);
    runStatus.classList.add("ok");
    await refreshRuns();
    await openRun(out.results[out.results.length - 1].run_id);
  } catch (err) {
    setBusy(false, `运行出错：${err.message}`);
    runStatus.classList.add("error");
  }
});

async function refreshRuns() {
  const data = await (await fetch("/api/runs")).json();
  const tbody = document.querySelector("#runs-table tbody");
  if (!data.runs.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">暂无运行记录，请点击上方按钮</td></tr>`;
    return;
  }
  tbody.innerHTML = data.runs.map((r) => `
    <tr data-run="${esc(r.run_id)}" class="${r.run_id === currentRunId ? "active" : ""}">
      <td>${esc(r.run_id)}</td>
      <td>${esc(r.scenario)}</td>
      <td><span class="status-badge status-${esc(r.status)}">${esc(r.status)}</span></td>
      <td>${r.message_count}</td>
      <td>${r.duration_ms}</td>
      <td class="${r.error_count ? "num-bad" : ""}">${r.error_count}</td>
      <td class="${r.warning_count ? "num-warn" : ""}">${r.warning_count}</td>
      <td>${new Date(r.started_at * 1000).toLocaleTimeString("zh-CN")}</td>
    </tr>`).join("");
  tbody.querySelectorAll("tr[data-run]").forEach((tr) => {
    tr.addEventListener("click", () => openRun(tr.dataset.run));
  });
}

// ---------------- 详情渲染 ----------------
async function openRun(runId) {
  currentRunId = runId;
  const data = await (await fetch(`/api/runs/${encodeURIComponent(runId)}`)).json();
  $("detail-empty").hidden = true;
  $("detail-body").hidden = false;
  $("detail-title").textContent = `运行 ${data.run_id} · 场景 ${data.scenario}`;
  $("detail-body").innerHTML = renderDetail(data);
  await refreshRuns();
}

function renderDetail(data) {
  return `
    ${renderIdentity(data)}
    ${renderErrors(data)}
    ${data.generations.map(renderGeneration).join("")}
    ${renderProtocolLog(data)}`;
}

function renderIdentity(data) {
  const idn = data.identity;
  const dup = idn.duplicate_message_ids.length
    ? idn.duplicate_message_ids.map((d) =>
        `<li>Message-ID <code>${esc(d.message_id)}</code> 出现在身份键：
         ${d.identity_keys.map(esc).join("、")}（未合并）</li>`).join("")
    : "<li class='muted'>无重复 Message-ID</li>";
  const reused = idn.reused_uids.length
    ? idn.reused_uids.map((r) =>
        `<li>UID ${r.uid} 在 UIDVALIDITY ${r.uidvalidity_values.join(" → ")} 中复用</li>`).join("")
    : "<li class='muted'>无 UID 复用</li>";
  return `
  <details class="detail-section" open>
    <summary>身份标识分析（UIDVALIDITY/UID 主键，代际切换 ${idn.generation_switches} 次）</summary>
    <div class="sec-body">
      <div class="pill-row">
        ${idn.uidvalidity_sequence.map((u) => `<span class="pill">UIDVALIDITY ${u}</span>`).join("")}
      </div>
      <ul class="tight"><li class="muted">代际切换：${idn.generation_switches} 次</li>${reused}</ul>
      <ul class="tight"><li class="muted">重复 Message-ID：</li>${dup}</ul>
      <div class="table-wrap">
        <table><thead><tr><th>身份键</th><th>UIDVALIDITY</th><th>UID</th>
        <th>Message-ID</th><th>解码主题</th></tr></thead><tbody>
        ${idn.records.map((r) => `<tr>
          <td><code>${esc(r.identity_key)}</code></td><td>${r.uidvalidity}</td><td>${r.uid}</td>
          <td>${esc(r.message_id ?? "（缺失）")}</td><td>${esc(r.subject ?? "")}</td>
        </tr>`).join("")}
        </tbody></table>
      </div>
    </div>
  </details>`;
}

function renderErrors(data) {
  const errs = data.errors.length
    ? data.errors.map((e) => `<li class="err-text">${esc(e)}</li>`).join("")
    : "<li class='muted'>无错误</li>";
  const warns = data.warnings.length
    ? data.warnings.map((w) => `<li class="warn-text">${esc(w)}</li>`).join("")
    : "<li class='muted'>无警告</li>";
  return `
  <details class="detail-section" ${data.errors.length ? "open" : ""}>
    <summary>错误与警告（${data.errors.length} 个错误 / ${data.warnings.length} 个警告）</summary>
    <div class="sec-body"><ul class="tight">${errs}</ul><ul class="tight">${warns}</ul></div>
  </details>`;
}

function renderGeneration(gen) {
  return `
  <details class="detail-section" open>
    <summary>代际 ${gen.index}：UIDVALIDITY ${gen.uidvalidity} · 模拟服务 ${esc(gen.server_host)}:${gen.server_port} · ${gen.messages.length} 封邮件</summary>
    <div class="sec-body">
      ${gen.messages.map(renderMessage).join("")}
    </div>
  </details>`;
}

function renderMessage(msg) {
  const b = msg.extraction.batch;
  const batchRow = b.value
    ? `<dt>批号（来源 part）</dt>
       <dd><b>${esc(b.value)}</b> ← part <code>${esc(b.source_part_id)}</code>
       <span class="tag ${esc(b.source_content_type.replace("/", "-"))}">${esc(b.source_content_type)}</span>
       ${b.conflict ? `<span class="tag err">替代版本冲突</span>` : ""}</dd>`
    : `<dt>批号</dt><dd class="err-text">未提取到</dd>`;
  const versions = b.versions.map((v) =>
    `<li>part <code>${esc(v.part_id)}</code> (${esc(v.content_type)})：
     ${v.batch ? esc(v.batch) : "<span class='muted'>无批号</span>"}
     ${v.selected ? `<span class="tag sel">已选用</span>` : ""}</li>`).join("");
  const atts = msg.extraction.attachments.map((a) => `
    <li>part <code>${esc(a.part_id)}</code> · ${esc(a.filename ?? "（无文件名）")} · ${esc(a.content_type)}
      ${a.decode_error
        ? `<div class="err-text">附件解码错误：${esc(a.decode_error)}</div>`
        : (a.skipped_reason
          ? `<div class="muted">${esc(a.skipped_reason)}</div>`
          : `<div>数值：[${a.numbers.join(", ")}]</div>`)}
      ${a.source_preview ? `<pre class="code">${esc(a.source_preview)}</pre>` : ""}
    </li>`).join("") || "<li class='muted'>无附件</li>";
  return `
  <div class="msg-card">
    <div class="msg-head">
      <div><b>${esc(msg.decoded_subject ?? "（无主题）")}</b>
        <div class="mid">身份键 ${esc(msg.identity_key)} · Message-ID ${esc(msg.message_id ?? "（缺失）")}</div>
      </div>
      <div class="mid">原始主题头：${esc(msg.raw_subject ?? "")}</div>
    </div>
    <div class="msg-body">
      <dl class="kv">
        ${batchRow}
      </dl>
      <div><b>替代版本证据</b><ul class="tight">${versions}</ul></div>
      <div><b>附件证据（数值可追溯到 part）</b><ul class="tight">${atts}</ul></div>
      <details>
        <summary class="muted">原邮件 MIME 结构树（part_id 即证据锚点）</summary>
        <div class="tree">${renderTree(msg.parsed.tree, true)}</div>
      </details>
      ${msg.errors.length ? `<div class="err-text">本邮件错误：<ul class="tight">
        ${msg.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div>` : ""}
    </div>
  </div>`;
}

function renderTree(node, isRoot) {
  const tags = [];
  if (node.content_type === "text/plain") tags.push(`<span class="tag plain">text/plain</span>`);
  if (node.content_type === "text/html") tags.push(`<span class="tag html">text/html</span>`);
  if (node.is_attachment) tags.push(`<span class="tag att">附件</span>`);
  if (node.error) tags.push(`<span class="tag err">解码错误</span>`);
  const name = node.filename ? ` · ${esc(node.filename)}` : "";
  const cte = node.cte ? ` · CTE=${esc(node.cte)}` : "";
  const errLine = node.error ? `<div class="err-text">错误：${esc(node.error)}</div>` : "";
  const preview = node.preview && !node.is_attachment
    ? `<div class="muted">预览：${esc(node.preview.slice(0, 80))}${node.preview.length > 80 ? "…" : ""}</div>`
    : (node.is_attachment && !node.error ? `<div class="muted">大小 ${node.size} 字节</div>` : "");
  return `
    <div class="node ${isRoot ? "root" : ""}">
      <b>${esc(node.part_id)}</b> ${esc(node.content_type)}${name}${cte} ${tags.join("")}
      ${errLine}${preview}
      ${node.children.map((c) => renderTree(c, false)).join("")}
    </div>`;
}

function renderProtocolLog(data) {
  const logs = data.generations.map((g) => `
    <div><b>代际 ${g.index}（端口 ${g.server_port}）协议交互：</b>
      ${g.protocol_log.map((l) => `<div class="log-line">${esc(l)}</div>`).join("")}
      <div class="muted">服务端命令日志：</div>
      ${g.command_log.map((l) => `<div class="log-line">${esc(l)}</div>`).join("")}
    </div>`).join("");
  return `
  <details class="detail-section">
    <summary>协议交互日志（采集只经 IMAP 公开接口）</summary>
    <div class="sec-body">${logs}
      <div class="muted">协议子集：${esc(data.protocol_subset)}</div>
    </div>
  </details>`;
}

init().catch((err) => {
  runStatus.className = "run-status error";
  runStatus.textContent = "初始化失败：" + err.message;
});
