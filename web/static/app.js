/* 全局状态 */
const state = {
  spec: null,
  scenarios: null,
  currentOp: null,
  formValues: {},   // 结构化参数值（与 JSON 文本对应）
  history: [],
};

const $ = (sel) => document.querySelector(sel);

async function fetchJSON(url, opts) {
  const resp = await fetch(url, opts);
  const body = await resp.json();
  return { ok: resp.ok, status: resp.status, body };
}

/* ---------- 初始化 ---------- */
async function init() {
  const [{ body: spec }, { body: scenarios }] = await Promise.all([
    fetchJSON("/api/spec"),
    fetchJSON("/api/scenarios"),
  ]);
  state.spec = spec;
  state.scenarios = scenarios;

  const select = $("#operation-select");
  select.innerHTML = spec.operations
    .map((op) => `<option value="${op.operationId}">${op.operationId} — ${op.method} ${op.path_template}</option>`)
    .join("");
  select.addEventListener("change", () => switchOperation(select.value));
  switchOperation(select.value);

  $("#btn-preview").addEventListener("click", () => execute(false));
  $("#btn-run").addEventListener("click", () => execute(true));
  $("#btn-clear").addEventListener("click", clearResults);
  $("#sync-from-json").addEventListener("click", applyJsonToForm);
  $("#sync-to-json").addEventListener("click", renderJsonFromForm);
  $("#run-demo").addEventListener("click", runDemo);

  renderScenarios();
}

function switchOperation(operationId) {
  state.currentOp = state.spec.operations.find((o) => o.operationId === operationId);
  state.formValues = {};
  for (const p of state.currentOp.parameters) {
    state.formValues[p.name] = defaultValue(p);
  }
  renderParamForm();
  renderJsonFromForm();
  clearResults();
}

function defaultValue(p) {
  if (p.type === "array") return [];
  if (p.type === "object") return {};
  if (p.type === "integer") return null;
  if (p.type === "boolean") return false;
  return "";
}

/* ---------- 动态参数表单 ---------- */
function renderParamForm() {
  const root = $("#param-form");
  root.innerHTML = "";
  for (const p of state.currentOp.parameters) {
    const row = document.createElement("div");
    row.className = "param-row";
    const reserved = p.allowReserved
      ? '<span class="tag ok">allowReserved</span>' : "";
    row.innerHTML = `
      <div class="meta">
        <span class="tag">${p.in}</span>
        <span class="tag">style=${p.style}</span>
        <span class="tag">explode=${p.explode}</span>
        <span class="tag">type=${p.type}${p.items ? `<${p.items.type}>` : ""}</span>
        ${p.required ? '<span class="tag reject">required</span>' : '<span class="tag">optional</span>'}
        ${reserved}
      </div>
      <div class="editor" data-name="${p.name}"></div>`;
    root.appendChild(row);
    renderEditor(row.querySelector(".editor"), p, state.formValues[p.name]);
  }
}

function renderEditor(container, p, value) {
  container.innerHTML = "";
  const label = document.createElement("div");
  label.style.cssText = "font-weight:600;margin-bottom:4px;";
  label.textContent = p.name;
  container.appendChild(label);

  if (p.type === "string" || p.type === "integer") {
    const input = document.createElement("input");
    input.placeholder = p.type === "integer" ? "整数（留空表示不传）" : "字符串（留空表示不传）";
    input.value = value === null || value === undefined ? "" : String(value);
    input.addEventListener("input", () => {
      const raw = input.value.trim();
      if (raw === "") { state.formValues[p.name] = p.type === "integer" ? null : ""; }
      else if (p.type === "integer") { state.formValues[p.name] = Number(raw); }
      else { state.formValues[p.name] = raw; }
      renderJsonFromForm();
    });
    container.appendChild(input);
    return;
  }

  if (p.type === "boolean") {
    const select = document.createElement("select");
    select.innerHTML = `<option value="false">false</option><option value="true">true</option>`;
    select.value = String(Boolean(value));
    select.addEventListener("change", () => {
      state.formValues[p.name] = select.value === "true";
      renderJsonFromForm();
    });
    container.appendChild(select);
    return;
  }

  if (p.type === "array") {
    renderArrayEditor(container, p, value);
    return;
  }

  if (p.type === "object") {
    renderObjectEditor(container, p, value);
  }
}

function renderArrayEditor(container, p, value) {
  const list = document.createElement("div");
  list.className = "array-list";
  container.appendChild(list);

  const draw = () => {
    list.innerHTML = "";
    value.forEach((item, idx) => {
      const row = document.createElement("div");
      row.className = "array-row";
      const index = document.createElement("span");
      index.className = "idx";
      index.textContent = idx;
      const input = document.createElement("input");
      input.placeholder = `${p.items.type} 元素`;
      input.value = String(item);
      input.addEventListener("input", () => {
        value[idx] = p.items.type === "integer" ? Number(input.value) : input.value;
        renderJsonFromForm();
      });
      const del = document.createElement("button");
      del.className = "btn-mini danger";
      del.textContent = "删除";
      del.addEventListener("click", () => { value.splice(idx, 1); draw(); renderJsonFromForm(); });
      row.append(index, input, del);
      list.appendChild(row);
    });
    if (value.length === 0) {
      const hint = document.createElement("div");
      hint.className = "result-meta";
      hint.textContent = "空数组：query 参数将被省略并记录；path 参数会在发送前拒绝。";
      list.appendChild(hint);
    }
  };
  draw();

  const add = document.createElement("button");
  add.className = "btn-mini";
  add.textContent = "＋ 添加数组元素";
  add.addEventListener("click", () => {
    value.push(p.items.type === "integer" ? 0 : "");
    draw();
    renderJsonFromForm();
  });
  container.appendChild(add);
}

function renderObjectEditor(container, p, value) {
  const list = document.createElement("div");
  container.appendChild(list);
  const propNames = Object.keys(p.properties);

  const draw = () => {
    list.innerHTML = "";
    if (propNames.length === 0) {
      list.innerHTML = '<div class="result-meta">该对象无已声明属性</div>';
      return;
    }
    for (const key of propNames) {
      const schema = p.properties[key];
      const row = document.createElement("div");
      row.className = "object-row";
      const keyLabel = document.createElement("span");
      keyLabel.style.cssText = "width:110px;color:var(--muted);font-family:var(--mono)";
      keyLabel.textContent = key;
      let input;
      if (schema.type === "boolean") {
        input = document.createElement("select");
        input.innerHTML = `<option value="">未设置</option><option value="true">true</option><option value="false">false</option>`;
        input.value = key in value ? String(value[key]) : "";
        input.addEventListener("change", () => {
          if (input.value === "") delete value[key];
          else value[key] = input.value === "true";
          renderJsonFromForm();
        });
      } else {
        input = document.createElement("input");
        input.placeholder = schema.type;
        input.value = key in value ? String(value[key]) : "";
        input.addEventListener("input", () => {
          if (input.value === "") { delete value[key]; return; }
          value[key] = schema.type === "integer" ? Number(input.value) : input.value;
          renderJsonFromForm();
        });
      }
      row.append(keyLabel, input);
      list.appendChild(row);
    }
    if (Object.keys(value).length === 0) {
      const hint = document.createElement("div");
      hint.className = "result-meta";
      hint.textContent = "空对象：query 参数将被省略并记录。";
      list.appendChild(hint);
    }
  };
  draw();
}

/* ---------- JSON 双向同步 ---------- */
function renderJsonFromForm() {
  const cleaned = {};
  for (const [k, v] of Object.entries(state.formValues)) {
    if (v === null) continue;
    if (v === "") continue;
    if (Array.isArray(v) && v.length === 0) cleaned[k] = [];
    else if (typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0) cleaned[k] = {};
    else cleaned[k] = v;
  }
  $("#params-json").value = JSON.stringify(cleaned, null, 2);
}

function applyJsonToForm() {
  let parsed;
  try {
    parsed = JSON.parse($("#params-json").value || "{}");
  } catch (err) {
    alert("JSON 解析失败：" + err.message);
    return;
  }
  if (typeof parsed !== "object" || Array.isArray(parsed)) {
    alert("顶层必须是对象 {参数名: 值}");
    return;
  }
  state.formValues = {};
  for (const p of state.currentOp.parameters) {
    state.formValues[p.name] = parsed[p.name] !== undefined ? parsed[p.name] : defaultValue(p);
  }
  renderParamForm();
}

/* ---------- 预览 / 运行 ---------- */
function collectParams() {
  const params = {};
  for (const [k, v] of Object.entries(state.formValues)) {
    if (v === null || v === "") continue;
    params[k] = v;
  }
  return params;
}

async function execute(sendRequest, scenarioMeta) {
  const payload = { operationId: state.currentOp.operationId, params: collectParams() };
  clearResults();
  try {
    const url = sendRequest ? "/api/run" : "/api/preview";
    const { body } = await fetchJSON(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderOutcome(body, sendRequest, scenarioMeta);
    addHistory(body, sendRequest, scenarioMeta);
    return body;
  } catch (err) {
    $("#reject-box").classList.remove("hidden");
    $("#reject-reason").textContent = "前端调用失败：" + err.message;
  }
}

function renderOutcome(body, sent, scenarioMeta) {
  if (body.rejected_before_request) {
    $("#reject-box").classList.remove("hidden");
    $("#reject-reason").textContent = `[${body.error_type}] ${body.error}`;
    $("#request-line").textContent = "请求未生成 —— 构造器在发送前已拒绝该组合。";
    $("#echo-view").textContent = "无网络请求，因此无源站回显。";
    return;
  }
  const req = body.request;
  $("#request-line").textContent =
    `${req.request_line}\n\n完整相对 URL: ${req.url}\n原始 query string: ${req.query_string || "(空)"}`;
  renderOmitted(req);

  if (!sent || !body.response) {
    $("#echo-view").textContent = "当前为“生成请求（不发送）”，暂无回显。点击“运行采集任务”真实发送。";
    return;
  }
  const resp = body.response;
  $("#echo-view").textContent = JSON.stringify(resp.echo, null, 2);
  renderResponse(resp, req);
}

function renderOmitted(req) {
  const tbody = $("#omitted-table tbody");
  tbody.innerHTML = "";
  const notes = [];
  for (const plan of req.parameter_plan) {
    if (plan.omitted) notes.push([plan.name, "空数组/空对象，按 OpenAPI 约定从 query 中省略"]);
    else if (plan.allowReserved) notes.push([plan.name, "allowReserved=true：值中 ,/?:@ 等保留字符原样，=/%# 仍编码"]);
  }
  for (const name of req.omitted) {
    if (!notes.some((n) => n[0] === name)) notes.push([name, "已省略"]);
  }
  if (notes.length === 0) {
    tbody.innerHTML = '<tr><td colspan="2">无省略项；所有非 ASCII 与结构字符均按 RFC 3986 百分号编码。</td></tr>';
    return;
  }
  for (const [k, v] of notes) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${k}</td><td>${v}</td>`;
    tbody.appendChild(tr);
  }
}

function renderResponse(resp) {
  $("#result-meta").textContent =
    `HTTP ${resp.status_code} · 耗时 ${resp.elapsed_ms} ms · 最终 URL: ${resp.final_url}`;
  const table = $("#result-table");
  const rows = Array.isArray(resp.data) ? resp.data : (resp.data ? [resp.data] : []);
  if (rows.length === 0) {
    table.querySelector("thead").innerHTML = "";
    table.querySelector("tbody").innerHTML =
      '<tr><td>本次真实执行返回 0 条实验数据（可能是筛选无匹配或 404）。</td></tr>';
  } else {
    const cols = ["id", "name", "owner", "tags", "code", "score", "level",
                  "status", "archived", "ref"];
    table.querySelector("thead").innerHTML =
      "<tr>" + cols.map((c) => `<th>${c}</th>`).join("") + "</tr>";
    table.querySelector("tbody").innerHTML = rows.map((r) =>
      "<tr>" + cols.map((c) => {
        const v = Array.isArray(r[c]) ? r[c].join(" | ") : String(r[c] ?? "");
        return `<td class="wrap">${escapeHtml(v)}</td>`;
      }).join("") + "</tr>"
    ).join("");
  }
  $("#raw-response").textContent = JSON.stringify(resp, null, 2);
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function clearResults() {
  $("#reject-box").classList.add("hidden");
  $("#request-line").textContent = "尚未生成请求";
  $("#echo-view").textContent = "等待执行…";
  $("#raw-response").textContent = "";
  $("#result-meta").textContent = "";
  $("#result-table").querySelector("thead").innerHTML = "";
  $("#result-table").querySelector("tbody").innerHTML = "";
  $("#omitted-table tbody").innerHTML = "";
}

/* ---------- 场景 ---------- */
function renderScenarios() {
  const demoRoot = $("#scenario-demo");
  const accRoot = $("#scenario-acc");
  demoRoot.innerHTML = "";
  accRoot.innerHTML = "";
  for (const sc of state.scenarios.run_demo || []) {
    demoRoot.appendChild(scenarioNode(sc));
  }
  for (const sc of state.scenarios.acceptance || []) {
    if (sc.disabled) continue;
    accRoot.appendChild(scenarioNode(sc));
  }
}

function scenarioNode(sc) {
  const node = document.createElement("div");
  node.className = "scenario-item";
  node.innerHTML = `
    <span class="kind-badge kind-${sc.kind}">${sc.kind === "rejection" ? "拒绝" : "成功"}</span>
    <span class="title">${escapeHtml(sc.title)}</span>`;
  const btn = document.createElement("button");
  btn.className = "btn-mini";
  btn.textContent = "载入并运行";
  btn.addEventListener("click", () => loadAndRun(sc));
  node.appendChild(btn);
  return node;
}

async function loadAndRun(sc) {
  $("#operation-select").value = sc.operationId;
  switchOperation(sc.operationId);
  state.formValues = {};
  for (const p of state.currentOp.parameters) {
    state.formValues[p.name] = (sc.params && sc.params[p.name] !== undefined)
      ? sc.params[p.name] : defaultValue(p);
  }
  renderParamForm();
  renderJsonFromForm();
  return execute(true, { id: sc.id, title: sc.title, kind: sc.kind });
}

/* ---------- 运行示例（正常 + 边界，目标 15 秒内） ---------- */
async function runDemo() {
  const btn = $("#run-demo");
  const status = $("#demo-status");
  btn.disabled = true;
  const results = [];
  try {
    for (const sc of state.scenarios.run_demo) {
      status.textContent = `正在运行：${sc.title} …`;
      const body = await loadAndRun(sc);
      results.push({ id: sc.id, ok: body && body.ok, body });
      await new Promise((r) => setTimeout(r, 200));
    }
    const okCount = results.filter((r) => r.ok).length;
    status.textContent = `示例完成：${okCount}/${results.length} 成功，结果已保留在历史记录中。`;
  } catch (err) {
    status.textContent = "示例运行出错：" + err.message;
  } finally {
    btn.disabled = false;
  }
}

/* ---------- 历史 ---------- */
function addHistory(body, sent, scenarioMeta) {
  const label = scenarioMeta
    ? `${scenarioMeta.id} · ${scenarioMeta.title}`
    : `${state.currentOp.operationId}（手动${sent ? "运行" : "预览"}）`;
  state.history.unshift({
    label,
    rejected: !!body.rejected_before_request,
    status: body.response ? body.response.status_code : null,
    body,
    time: new Date().toLocaleTimeString(),
  });
  renderHistory();
}

function renderHistory() {
  const root = $("#history-list");
  root.innerHTML = "";
  for (const item of state.history) {
    const node = document.createElement("div");
    node.className = "history-item";
    const pill = item.rejected
      ? '<span class="pill skip">发送前拒绝</span>'
      : item.body.ok
        ? `<span class="pill ok">HTTP ${item.status}</span>`
        : '<span class="pill err">失败</span>';
    node.innerHTML = `
      <div class="h-title">
        <strong>${escapeHtml(item.label)}</strong>
        ${pill}
        <span class="result-meta">${item.time}</span>
      </div>
      <div class="code" style="max-height:160px;margin-top:6px">${escapeHtml(
        item.rejected
          ? `[拒绝原因] ${item.body.error_type}: ${item.body.error}`
          : item.body.request.request_line
      )}</div>`;
    root.appendChild(node);
  }
}

init().catch((err) => {
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<div class="reject">页面初始化失败：${escapeHtml(err.message)}</div>`
  );
});
