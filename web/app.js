/* 公开天文观测窗口采集器前端：原生 JS，无外部依赖 */
"use strict";

const $ = (id) => document.getElementById(id);
let presets = [];
let currentResult = null;

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({ error: "响应不是合法 JSON" }));
  if (!resp.ok && data.error) throw new Error(data.error);
  return data;
}

function originLabel(origin) {
  return {
    master: "主规则实例",
    rdate: "RDATE 额外日期",
    override_move: "RECURRENCE-ID 例外（移动）",
    override_cancel: "RECURRENCE-ID 例外（取消）",
  }[origin] || origin;
}

function formatTime(start, end, allDay) {
  if (allDay) return `${start} 全日 → ${end}（不含）`;
  return `${start.replace("T", " ")} → ${end.replace("T", " ")}`;
}

// --------------------------------------------------------------------------- //
// 初始化
// --------------------------------------------------------------------------- //
async function init() {
  presets = (await api("/api/presets")).presets;
  const select = $("preset-select");
  select.innerHTML = presets.map((p) =>
    `<option value="${p.key}">${p.title}</option>`).join("");
  select.addEventListener("change", () => fillPreset(select.value));
  fillPreset(presets[0].key);

  $("run-collect").addEventListener("click", runCollect);
  $("run-demo").addEventListener("click", runDemo);
  $("reset-form").addEventListener("click", () => fillPreset(select.value));
  await refreshRuns();
}

function fillPreset(key) {
  const p = presets.find((x) => x.key === key);
  if (!p) return;
  $("source-input").value = p.source_url;
  $("range-start").value = p.range_start;
  $("range-end").value = p.range_end;
  $("target-name").value = key;
}

// --------------------------------------------------------------------------- //
// 运行采集
// --------------------------------------------------------------------------- //
async function runCollect() {
  const body = {
    source: $("source-input").value.trim(),
    range_start: $("range-start").value.trim(),
    range_end: $("range-end").value.trim(),
    max_instances: Number($("max-instances").value) || 5000,
    target_name: $("target-name").value.trim() || null,
  };
  if (!body.source || !body.range_start || !body.range_end) {
    alert("请填写来源与查询范围");
    return;
  }
  setBusy(true, "采集中…");
  try {
    const result = await api("/api/collect", {
      method: "POST",
      body: JSON.stringify(body),
    });
    currentResult = result;
    renderResult(result);
  } catch (exc) {
    renderFatal(exc.message);
  } finally {
    setBusy(false);
    await refreshRuns();
  }
}

async function runDemo() {
  setBusy(true, "示例运行中（正常 → 边界）…");
  try {
    const payload = await api("/api/run-demo", {
      method: "POST",
      body: JSON.stringify({}),
    });
    $("demo-status").textContent =
      `已完成 ${payload.runs.length} 个示例，保留结果可切换查看`;
    // 默认展示边界场景（含取消/移动/夏令时），同时两个结果都保留在历史里
    currentResult = payload.runs[payload.runs.length - 1];
    renderResult(currentResult);
    renderDemoSwitcher(payload.runs);
  } catch (exc) {
    renderFatal(exc.message);
  } finally {
    setBusy(false);
    await refreshRuns();
  }
}

function renderDemoSwitcher(runs) {
  const banner = $("issue-banner");
  banner.classList.remove("hidden", "error-level");
  banner.innerHTML =
    `<b>示例两个场景均已真实执行：</b>` +
    runs.map((r, i) =>
      `<a href="#" data-run="${i}" class="demo-link">` +
      `${i === 0 ? "① 正常场景" : "② 边界场景"}（${r.stats.in_range} 个实例，状态：${r.status}）` +
      `</a>`).join("　");
  banner.querySelectorAll(".demo-link").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      currentResult = runs[Number(a.dataset.run)];
      renderResult(currentResult);
    });
  });
}

function setBusy(busy, text) {
  $("run-collect").disabled = busy;
  $("run-demo").disabled = busy;
  if (busy) $("demo-status").textContent = text || "处理中…";
  else if (!$("demo-status").textContent.startsWith("已完成"))
    $("demo-status").textContent = "";
}

function renderFatal(message) {
  const summary = $("run-summary");
  summary.className = "run-summary fail";
  summary.textContent = `任务失败：${message}`;
  $("timeline").innerHTML = `<div class="empty-hint">未取得任何实例</div>`;
  $("evidence-content").textContent = message;
}

// --------------------------------------------------------------------------- //
// 结果渲染
// --------------------------------------------------------------------------- //
function renderResult(result) {
  renderSubset(result.supported_subset);

  const summary = $("run-summary");
  const s = result.stats;
  summary.className =
    `run-summary ${result.status === "failed" ? "fail" : "ok"}`;
  summary.textContent =
    `运行 ${result.run_id} ｜ 状态：${result.status}` +
    (result.error_message ? ` ｜ ${result.error_message}` : "") +
    ` ｜ 范围内实例 ${s.in_range}（取消 ${s.cancelled}）` +
    ` ｜ 问题 ${result.issues.length} 条`;

  renderIssues(result.issues);
  renderTimeline(result.instances);
  renderEvidence(result);
}

function renderSubset(subset) {
  if (!subset) return;
  $("subset-content").innerHTML =
    `<b>支持：</b>${subset.rrule_freq.join(" / ")} 频率；` +
    `${subset.rrule_parts.join("、")}<br>` +
    `<b>能力：</b>${subset.supported.join("；")}<br>` +
    `<span class="bad"><b>不支持（遇即上报）：</b>${subset.unsupported.join("；")}</span>`;
}

function renderIssues(issues) {
  const banner = $("issue-banner");
  if (!issues.length) {
    banner.classList.add("hidden");
    banner.innerHTML = "";
    return;
  }
  const hasError = issues.some((i) => i.severity === "error");
  banner.classList.remove("hidden");
  banner.classList.toggle("error-level", hasError);
  banner.innerHTML =
    `<b>${hasError ? "发生错误" : "未支持/告警"}（${issues.length}）：</b><br>` +
    issues.map((i) =>
      `[${i.severity}] <code>${i.code}</code> ${escapeHtml(i.message)}` +
      `${i.uid ? `（UID: ${escapeHtml(i.uid)}` : ""}` +
      `${i.line ? `，行 ${i.line}` : ""}）`).join("<br>");
}

function renderTimeline(instances) {
  const box = $("timeline");
  if (!instances.length) {
    box.innerHTML = `<div class="empty-hint">查询范围内没有实例</div>`;
    return;
  }
  const groups = new Map();
  for (const inst of instances) {
    const day = inst.all_day ? inst.start : inst.start.slice(0, 10);
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day).push(inst);
  }
  box.innerHTML = [...groups.entries()].map(([day, list]) => `
    <div class="tl-row">
      <div class="tl-date">${day}</div>
      <div class="tl-track">
        ${list.map((inst) => `
          <div class="tl-item ${inst.origin} ${inst.floating ? "floating" : ""} ${inst.all_day ? "all-day" : ""}"
               data-id="${escapeHtml(inst.instance_id)}"
               title="${escapeHtml(inst.origin_explanation)}">
            <span class="tl-time">${escapeHtml(formatTime(inst.start, inst.end, inst.all_day))}</span>
            <span class="tl-origin">${originLabel(inst.origin)}${inst.status === "CANCELLED" ? " · 已取消" : ""}</span>
          </div>`).join("")}
      </div>
    </div>`).join("");

  box.querySelectorAll(".tl-item").forEach((el) => {
    el.addEventListener("click", () => showInstanceDetail(el.dataset.id, instances, el));
  });
}

function showInstanceDetail(instanceId, instances, el) {
  document.querySelectorAll(".tl-item.selected")
    .forEach((n) => n.classList.remove("selected"));
  el.classList.add("selected");
  const inst = instances.find((x) => x.instance_id === instanceId);
  const detail = $("detail-box");
  detail.open = true;
  $("detail-content").textContent =
    `实例身份 instance_id：\n  ${inst.instance_id}\n\n` +
    `UID：${inst.uid}\nRECURRENCE-ID 键：${inst.recurrence_id}\n` +
    `标题：${inst.summary}\n状态：${inst.status}\n` +
    `开始：${inst.start}${inst.start_utc ? `（UTC: ${inst.start_utc}）` : inst.floating ? "（浮动，无 UTC 换算）" : ""}\n` +
    `结束：${inst.end}${inst.end_utc ? `（UTC: ${inst.end_utc}）` : ""}\n` +
    `时间类型：${inst.all_day ? "全日事件" : inst.floating ? "浮动时间" : `显式时区 ${inst.tzid || "UTC"}`}\n` +
    `时长：${JSON.stringify(inst.duration)}\n\n` +
    `来源解释（${originLabel(inst.origin)}）：\n  ${inst.origin_explanation}\n\n` +
    `ICS 行号证据：${JSON.stringify(inst.source_lines, null, 2)}\n` +
    `组件行范围：${inst.evidence.event_begin_line}–${inst.evidence.event_end_line}` +
    `${inst.evidence.override_sequence != null ? `\n例外 SEQUENCE：${inst.evidence.override_sequence}` : ""}`;
}

function renderEvidence(result) {
  const ev = result.source_evidence || {};
  const text =
    `来源：${ev.source || "（无）"}\n方式：${ev.kind || "-"} ｜ 字节数：${ev.byte_count ?? "-"}` +
    ` ｜ Content-Type：${ev.content_type || "-"} ｜ 获取成功：${ev.fetched_ok}\n\n` +
    `EXDATE 排除记录：\n${(result.excluded || []).map((e) =>
      `  - ${e.uid} 排除 ${e.recurrence_id}（行 ${e.line ?? "?"}）`).join("\n") || "  （无）"}\n\n` +
    `问题清单：\n${result.issues.map((i) =>
      `  - [${i.severity}] ${i.code}: ${i.message}${i.line ? `（行 ${i.line}）` : ""}`).join("\n") || "  （无）"}`;
  $("evidence-content").textContent = text;
}

async function refreshRuns() {
  try {
    const { runs } = await api("/api/runs");
    $("runs-content").innerHTML = runs.length
      ? runs.map((r) => `
          <div class="run-line" data-run-id="${r.run_id}">
            <span class="${r.status === "failed" ? "tag-fail" : "tag-ok"}">●</span>
            ${r.finished_at} ｜ ${r.run_id} ｜ ${r.status} ｜ 实例 ${r.instance_count}
            ｜ 来源：${r.source || "-"}
          </div>`).join("")
      : "暂无历史运行。";
    $("runs-content").querySelectorAll(".run-line").forEach((el) => {
      el.addEventListener("click", async () => {
        const run = await api(`/api/runs/${el.dataset.runId}`);
        currentResult = run;
        renderResult(run);
      });
    });
  } catch (exc) {
    $("runs-content").textContent = `历史加载失败：${exc.message}`;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

init().catch((exc) => {
  $("run-summary").className = "run-summary fail";
  $("run-summary").textContent = `页面初始化失败：${exc.message}`;
});
