/* 科研测量数值采集器操作台前端逻辑 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const STATUS_LABELS = {
  ok: "正常", error: "错误", missing: "缺失",
  below_LOD: "低于检测限", above_LOD: "高于检测限", range: "区间",
};
const QUALIFIER_LABELS = {
  missing: "缺失值（空值，非 0）",
  below_detection_limit: "低于检测限（界值，非精确等值）",
  above_detection_limit: "高于检测限（界值，非精确等值）",
  range: "区间记录",
};

let LOCALES = [];

async function getJSON(url, opts) {
  const resp = await fetch(url, opts);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function setStatus(text, kind) {
  const bar = $("#status-bar");
  bar.textContent = text;
  bar.className = "status-bar" + (kind ? " " + kind : "");
}

function setBusy(on) {
  document.querySelectorAll("button").forEach((b) => { b.disabled = on; });
  if (on) setStatus("任务执行中，请稍候…", "busy");
}

function renderSummary(result) {
  if (result.collection_error) {
    $("#summary").innerHTML =
      `<span class="chip err">采集失败：${esc(result.collection_error.message)}</span>`;
    $("#summary").classList.remove("hidden");
    return;
  }
  $("#summary").innerHTML = `
    <span class="chip total">共 ${result.total} 条</span>
    <span class="chip ok">成功 ${result.ok_count}</span>
    <span class="chip err">错误 ${result.error_count}</span>
    <span class="chip miss">缺失 ${result.missing_count}</span>
    <span class="chip total">地区：${esc(result.locale_code)}（${esc(result.locale_origin)}）</span>`;
  $("#summary").classList.remove("hidden");
}

function recordRow(rec) {
  const tag = `<span class="tag ${esc(rec.status)}">${STATUS_LABELS[rec.status] || rec.status}</span>`;
  const value = rec.status === "missing"
    ? "<em>null（保留空值）</em>"
    : (rec.value === null ? "—" : esc(rec.value));
  const valueEnd = rec.value_end !== null && rec.value_end !== undefined
    ? ` ~ ${esc(rec.value_end)}` : "";
  const unit = rec.unit ? esc(rec.unit)
    : (rec.unit_raw ? `<span class="err-text">${esc(rec.unit_raw)}（非法单位）</span>` : "—");
  const qualifier = rec.qualifier
    ? (QUALIFIER_LABELS[rec.qualifier] || esc(rec.qualifier)) : "—";
  const prec = rec.precision
    ? `小数位 ${rec.precision.decimal_places} / 有效数字 ${rec.precision.significant_digits}`
    : "—";
  const unc = rec.uncertainty
    ? `${rec.uncertainty.kind}: +${rec.uncertainty.plus} / -${rec.uncertainty.minus}`
    : "—";
  const err = rec.error
    ? `<div class="err-text">[${esc(rec.error.code)}] ${esc(rec.error.message)}</div>` : "";
  const steps = rec.steps.map((s) => `<li>${esc(s)}</li>`).join("");
  const src = rec.source
    ? `<div class="src">来源：${esc(rec.source.source_type)} ·
       行ID ${esc(rec.source.row_id)} · <code>${esc(JSON.stringify(rec.source.location))}</code></div>`
    : "";
  return `<tr>
    <td class="raw">${esc(rec.raw_text)}</td>
    <td>${tag}${err}</td>
    <td>${value}${valueEnd}</td>
    <td>${unit}</td>
    <td>${qualifier}</td>
    <td>${prec}<div class="muted">不确定度：${esc(unc)}</div></td>
    <td><details class="evidence"><summary>逐步解析与来源证据</summary>
      <ol class="steps">${steps}</ol>${src}</details></td>
  </tr>`;
}

function renderResultTable(result) {
  if (result.collection_error) {
    return `<div class="err-text">采集阶段错误：[${esc(result.collection_error.code)}]
      ${esc(result.collection_error.message)}</div>`;
  }
  const head = `<table class="grid"><thead><tr>
    <th>原文（保留）</th><th>状态</th><th>解析数值</th><th>单位</th>
    <th>限定符</th><th>精度 / 不确定度</th><th>证据</th>
  </tr></thead><tbody>
  ${result.records.map(recordRow).join("")}</tbody></table>`;
  return head;
}

function renderPayload(payload) {
  const root = $("#results");
  let html = "";
  if (payload.mode === "demo" || payload.mode === "acceptance") {
    const blocks = payload.scenarios || payload.runs || [];
    blocks.forEach((b) => {
      html += `<div class="scenario-title">${esc(b.scenario || b.name)}</div>`;
      renderSummaryInto = b.result;
      html += miniSummary(b.result);
      html += renderResultTable(b.result);
    });
  } else {
    renderSummary(payload.result);
    html += renderResultTable(payload.result);
  }
  root.innerHTML = html;
}

let renderSummaryInto = null;
function miniSummary(result) {
  if (result.collection_error) {
    return `<div class="summary"><span class="chip err">采集失败：${esc(result.collection_error.message)}</span></div>`;
  }
  return `<div class="summary">
    <span class="chip total">共 ${result.total} 条</span>
    <span class="chip ok">成功 ${result.ok_count}</span>
    <span class="chip err">错误 ${result.error_count}</span>
    <span class="chip miss">缺失 ${result.missing_count}</span>
    <span class="chip total">地区：${esc(result.locale_code)}（${esc(result.locale_origin)}）</span>
  </div>`;
}

async function runTask() {
  const mode = $("#mode").value;
  const body = { mode, locale: $("#locale").value };
  if (mode === "text") body.text = $("#text-input").value;
  if (mode === "file") body.path = $("#path-input").value.trim();
  if (mode === "http" || mode === "browser") body.url = $("#url-input").value.trim();
  if (mode === "browser") body.headless = true;
  setBusy(true);
  try {
    const payload = await getJSON("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    setStatus(`任务完成，运行编号 ${payload.run_id}（结果已保存到 runs/）`);
    renderPayload(payload);
  } catch (e) {
    setStatus(`任务失败：${e.message}`, "error");
  } finally {
    setBusy(false);
  }
}

async function runDemo() {
  setBusy(true);
  const t0 = performance.now();
  try {
    const payload = await getJSON("/api/demo", { method: "POST" });
    const ms = Math.round(performance.now() - t0);
    setStatus(`示例运行完成（耗时 ${ms} ms，运行编号 ${payload.run_id}），两个场景结果均已保留`);
    renderPayload(payload);
  } catch (e) {
    setStatus(`示例运行失败：${e.message}`, "error");
  } finally {
    setBusy(false);
  }
}

async function runAcceptance() {
  setBusy(true);
  try {
    const payload = await getJSON("/api/acceptance", { method: "POST" });
    setStatus(`完整验收场景完成，运行编号 ${payload.run_id}`);
    renderPayload(payload);
  } catch (e) {
    setStatus(`验收场景失败：${e.message}`, "error");
  } finally {
    setBusy(false);
  }
}

function switchMode() {
  const mode = $("#mode").value;
  $("#text-field").classList.toggle("hidden", mode !== "text");
  $("#path-field").classList.toggle("hidden", mode !== "file");
  $("#url-field").classList.toggle("hidden", !(mode === "http" || mode === "browser"));
}

const PRESETS = {
  ambiguous: "1,234\n1.234\n1,234.5",
  acceptance: [
    "1,234.56 mg/L", "7.8E-3 mg/L", "0.042(7) mg/L", "18.6 ± 0.4 mg/L",
    "9.81 +0.12 -0.08 mg/L", "-", "N/A", "< 0.01 mg/L", "> 50.0 ppb",
    "6.8 – 7.2 pH", "0.10 ~ 0.15 mg/L", "25.3 zz",
  ].join("\n"),
  locales: "1.234,56 mg/L\n1 234,56 mg/L\n12 345,67 mg/L\n12.5 mg/L\n7.8e-3 mg/L",
};

async function init() {
  const locData = await getJSON("/api/locales");
  LOCALES = locData.choices;
  $("#locale").innerHTML = LOCALES
    .map((c) => `<option value="${c.code}">${esc(c.label_zh)}（${c.code}）</option>`).join("");
  const updateDesc = () => {
    $("#locale-desc").textContent = locData.descriptions[$("#locale").value];
  };
  $("#locale").addEventListener("change", updateDesc);
  updateDesc();

  const samples = await getJSON("/api/samples");
  $("#dataset-id").textContent = samples.dataset;
  $("#samples-content").innerHTML = [
    ...samples.files.map((f) => `<div>文件：<code>${esc(f.path)}</code></div>`),
    "<div style='margin-top:6px'>源站公开接口（虚构）：</div>",
    `<div>HTML 页面：<code>${esc(samples.source_urls.html)}</code></div>`,
    `<div>JSON：<code>${esc(samples.source_urls.json)}</code></div>`,
    `<div>CSV：<code>${esc(samples.source_urls.csv)}</code></div>`,
    `<div>德语 JSON：<code>${esc(samples.source_urls.de_json)}</code></div>`,
  ].join("");

  $("#mode").addEventListener("change", switchMode);
  $("#run-task").addEventListener("click", runTask);
  $("#run-demo").addEventListener("click", runDemo);
  $("#run-acceptance").addEventListener("click", runAcceptance);
  document.querySelectorAll("[data-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("#mode").value = "text";
      switchMode();
      $("#text-input").value = PRESETS[btn.dataset.preset];
    });
  });
}

init().catch((e) => setStatus(`页面初始化失败：${e.message}`, "error"));
