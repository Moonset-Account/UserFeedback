/* SVG 实验图数值采集器前端：所有数据来自后端真实采集，不做任何预填。 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  result: null,
  scenarios: [],
  pendingFile: null,
  demoRunIds: [],
};
// 暴露给调试与端到端检查使用
window.state = state;

// ---------------- 通用请求 ----------------
async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

// ---------------- 初始化 ----------------
async function init() {
  const data = await fetch("/api/scenarios").then((r) => r.json());
  state.scenarios = data.scenarios;
  const sel = $("#scenario-select");
  sel.innerHTML = state.scenarios
    .map((s) => `<option value="${s.key}">${s.title}</option>`)
    .join("");

  $("#btn-run-sample").addEventListener("click", () =>
    runSample(sel.value)
  );
  $("#btn-upload").addEventListener("click", runUpload);
  $("#btn-rerun").addEventListener("click", rerunCurrent);
  $("#run-demo").addEventListener("click", runDemo);
  $("#file-input").addEventListener("change", (e) => {
    state.pendingFile = e.target.files[0] || null;
    $("#btn-upload").disabled = !state.pendingFile;
  });

  $$(".tab").forEach((tab) =>
    tab.addEventListener("click", () => switchTab(tab.dataset.tab))
  );
  $("#modal-close").addEventListener("click", () =>
    $("#modal-mask").classList.add("hidden")
  );
  $("#protocol-close").addEventListener("click", () =>
    $("#protocol-mask").classList.add("hidden")
  );
  $("#btn-protocol").addEventListener("click", showProtocol);
  $("#modal-mask").addEventListener("click", (e) => {
    if (e.target.id === "modal-mask") $("#modal-mask").classList.add("hidden");
  });

  await refreshHistory();
}

function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-body").forEach((b) =>
    b.classList.toggle("active", b.id === `tab-${name}`)
  );
}

// ---------------- 任务执行 ----------------
async function runSample(key) {
  setBusy(true, "正在采集……");
  try {
    const result = await postJSON("/api/run/sample", { scenario: key });
    state.result = result;
    render(result);
  } catch (err) {
    alert(`采集失败：${err.message}`);
  } finally {
    setBusy(false);
    refreshHistory();
  }
}

async function runUpload() {
  if (!state.pendingFile) return;
  setBusy(true, "正在上传并采集……");
  try {
    const fd = new FormData();
    fd.append("svg", state.pendingFile);
    const resp = await fetch("/api/run/upload", { method: "POST", body: fd });
    const result = await resp.json();
    if (!resp.ok) throw new Error(result.error || "上传失败");
    state.result = result;
    render(result);
    $("#btn-rerun").disabled = !result.stored_svg;
  } catch (err) {
    alert(`采集失败：${err.message}`);
  } finally {
    setBusy(false);
    refreshHistory();
  }
}

async function rerunCurrent() {
  const stored = state.result && state.result.stored_svg;
  if (!stored) {
    alert("内置样例请直接重新选择场景；仅上传图支持文件修改后重采。");
    return;
  }
  setBusy(true, "重新采集中（请确认已在 runs/ 目录修改 SVG）……");
  try {
    const result = await postJSON("/api/rerun", { stored_svg: stored });
    state.result = result;
    render(result);
  } catch (err) {
    alert(`重新采集失败：${err.message}`);
  } finally {
    setBusy(false);
    refreshHistory();
  }
}

async function runDemo() {
  const btn = $("#run-demo");
  btn.disabled = true;
  btn.textContent = "示例运行中……";
  state.demoRunIds = [];
  try {
    // 正常场景 + 边界场景，顺序执行，保留两份结果
    const normal = await postJSON("/api/run/sample", {
      scenario: "linear_basic",
    });
    state.demoRunIds.push(normal.run_id);
    state.result = normal;
    render(normal);
    await refreshHistory();

    const edge = await postJSON("/api/run/sample", { scenario: "clipped" });
    state.demoRunIds.push(edge.run_id);
    state.result = edge;
    render(edge);
    await refreshHistory();
    btn.textContent = "✓ 示例完成（两份结果可在历史中切换）";
    setTimeout(() => {
      btn.textContent = "▶ 运行示例（正常 + 边界）";
      btn.disabled = false;
    }, 4000);
  } catch (err) {
    alert(`示例运行失败：${err.message}`);
    btn.textContent = "▶ 运行示例（正常 + 边界）";
    btn.disabled = false;
  }
}

function setBusy(busy, text) {
  const meta = $("#run-meta");
  if (busy) {
    meta.classList.remove("empty");
    meta.textContent = text;
  }
  const actionIds = ["#run-demo", "#btn-run-sample", "#btn-upload",
    "#btn-rerun"];
  actionIds.forEach((sel) => {
    const btn = $(sel);
    if (!btn) return;
    if (busy) {
      btn.disabled = true;
    } else {
      btn.disabled = (sel === "#btn-upload" && !state.pendingFile)
        || (sel === "#btn-rerun" && !(state.result && state.result.stored_svg));
    }
  });
}

// ---------------- 历史 ----------------
async function refreshHistory() {
  const data = await fetch("/api/runs").then((r) => r.json());
  const ul = $("#run-history");
  ul.innerHTML = data.runs
    .map((r) => {
      const label = r.source || "未命名";
      const demo = state.demoRunIds.includes(r.run_id) ? " 📌示例" : "";
      return `<li data-run="${r.run_id}">
        <span>${r.created_at || ""} · ${label}${demo}</span>
        <span class="tag ${r.status}">${statusText(r.status)}</span>
      </li>`;
    })
    .join("");
  ul.querySelectorAll("li").forEach((li) =>
    li.addEventListener("click", async () => {
      const detail = await fetch(
        `/api/runs/${li.dataset.run}`
      ).then((r) => r.json());
      state.result = detail;
      render(detail);
      $("#btn-rerun").disabled = !detail.stored_svg;
    })
  );
}

function statusText(s) {
  return { ok: "成功", partial: "部分", error: "失败" }[s] || s;
}

// ---------------- 渲染 ----------------
async function render(result) {
  renderMeta(result);
  renderBanner(result);
  await renderSvg(result);
  // SVG 插入后再叠加标记
  requestAnimationFrame(() => renderOverlay(result));
  renderPoints(result);
  renderClipped(result);
  renderAxes(result);
  renderErrors(result);
  renderBreaks(result);
}

function renderMeta(r) {
  const meta = $("#run-meta");
  meta.classList.remove("empty");
  const s = r.summary || {};
  meta.innerHTML = `
    <b>任务：</b>${r.run_id} <br/>
    <b>来源：</b>${r.source.name}（${r.source.size_bytes ?? "-"} 字节）<br/>
    <b>耗时：</b>${r.elapsed_ms ?? "-"} ms　<b>状态：</b>${statusText(r.status)}<br/>
    <b>可见点：</b>${s.visible_points ?? 0}　<b>可恢复：</b>${
    s.recoverable_points ?? 0
  }　<b>拒绝：</b>${s.blocked_points ?? 0}　<b>裁剪：</b>${
    s.clipped_points ?? 0
  }`;
}

function renderBanner(r) {
  const banner = $("#status-banner");
  banner.classList.remove("hidden", "ok", "partial", "error");
  banner.classList.add(r.status);
  const parts = [];
  if (r.status === "ok") {
    parts.push("全部可见点均完成坐标换算，并给出像素量化误差范围。");
  }
  if (r.summary && r.summary.blocked_points) {
    parts.push(`有 ${r.summary.blocked_points} 个点因尺度不足/域外/非法对数被拒绝给值。`);
  }
  if (r.summary && r.summary.clipped_points) {
    parts.push(`有 ${r.summary.clipped_points} 个点被裁剪区域移除，仅登记位置不产生观测值。`);
  }
  if (r.status === "error") {
    parts.push("任务未能产生观测结果，请查看“错误与警告”。");
  }
  banner.innerHTML = parts.join("<br/>");
}

async function renderSvg(r) {
  const host = $("#svg-host");
  $("#empty-tip").classList.add("hidden");
  const resp = await fetch(r.svg_url);
  host.innerHTML = await resp.text();
  const svg = host.querySelector("svg");
  if (svg) {
    // 统一显示宽度，保持比例；overlay 依据同一盒模型定位
    svg.setAttribute("width", "860");
    const vb = svg.viewBox.baseVal;
    if (vb && vb.width) {
      svg.setAttribute("height", String(860 * vb.height / vb.width));
    } else if (!svg.getAttribute("height")) {
      svg.setAttribute("height", "540");
    }
    svg.style.position = "absolute";
    svg.style.left = "50%";
    svg.style.transform = "translateX(-50%)";
    svg.style.top = "6px";
  }
}

function svgMetrics() {
  const svg = $("#svg-host svg");
  if (!svg) return null;
  const rect = svg.getBoundingClientRect();
  const vb = svg.viewBox.baseVal;
  const sx = rect.width / (vb && vb.width ? vb.width : rect.width);
  const sy = rect.height / (vb && vb.height ? vb.height : rect.height);
  return { rect, sx, sy };
}

function toScreen(canvasX, canvasY) {
  const m = svgMetrics();
  if (!m) return { left: 0, top: 0 };
  const vb = $("#svg-host svg").viewBox.baseVal;
  const ox = vb && vb.x ? vb.x : 0;
  const oy = vb && vb.y ? vb.y : 0;
  return {
    left: (canvasX - ox) * m.sx + m.rect.left - $("#viewer").getBoundingClientRect().left,
    top: (canvasY - oy) * m.sy + m.rect.top - $("#viewer").getBoundingClientRect().top,
  };
}

function renderOverlay(r) {
  const overlay = $("#overlay");
  overlay.innerHTML = "";
  if (!svgMetrics()) return;

  for (const o of r.observations || []) {
    const pin = document.createElement("div");
    pin.className = `marker-pin ${o.recoverable ? "ok" : "blocked"}`;
    const pos = toScreen(o.canvas.x, o.canvas.y);
    pin.style.left = `${pos.left}px`;
    pin.style.top = `${pos.top}px`;
    pin.title = `点 #${o.index}（点击查看换算过程）`;
    pin.addEventListener("click", () => showTrace(o, r));
    overlay.appendChild(pin);
  }
  for (const c of r.clipped_points || []) {
    const pin = document.createElement("div");
    pin.className = "marker-pin clipped";
    const pos = toScreen(c.canvas.x, c.canvas.y);
    pin.style.left = `${pos.left}px`;
    pin.style.top = `${pos.top}px`;
    pin.title = "被裁剪点（点击查看说明）";
    pin.addEventListener("click", () => showTrace(c, r, true));
    overlay.appendChild(pin);
  }
  for (const brk of r.breaks || []) {
    // 断点标记放在该系列相邻两段的中间（用观测点近似）
    const seg = brk.detail?.match(/第 (\d+) 段/) ;
    const flag = document.createElement("div");
    flag.className = "break-flag";
    flag.title = brk.detail || "折线断点";
    const idx = seg ? Number(seg[1]) : 0;
    const around = (r.observations || []).filter(
      (o) => o.segment_index === idx
    ).pop();
    if (around) {
      const pos = toScreen(around.canvas.x + 14, around.canvas.y);
      flag.style.left = `${pos.left}px`;
      flag.style.top = `${pos.top}px`;
      overlay.appendChild(flag);
    }
  }
}

// ---------------- 证据表格 ----------------
function renderPoints(r) {
  $("#cnt-points").textContent = (r.observations || []).length;
  const rows = (r.observations || [])
    .map((o) => {
      const x = o.values.x;
      const y = o.values.y;
      const fmt = (v) =>
        v
          ? `${v.value} ± ${v.uncertainty} <span class="hint-inline">[${v.interval.join(
              ", "
            )}]</span>`
          : "—";
      return `<tr class="clickable" data-idx="${o.index}">
        <td class="num">#${o.index}</td>
        <td class="num">(${o.canvas.x}, ${o.canvas.y})</td>
        <td class="num">${fmt(x)}</td>
        <td class="num">${fmt(y)}</td>
        <td>${o.recoverable
          ? '<span class="badge ok">可恢复</span>'
          : '<span class="badge bad">拒绝给值</span>'}</td>
      </tr>`;
    })
    .join("");
  $("#tab-points").innerHTML = `
    <table>
      <thead><tr><th>编号</th><th>画布坐标(px)</th><th>X 值（误差范围）</th>
      <th>Y 值（误差范围）</th><th>状态</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5">无观测点</td></tr>'}</tbody>
    </table>
    <p class="hint">点击任意行或图上圆点，查看完整坐标换算过程。</p>`;
  $$("#tab-points tr.clickable").forEach((tr) =>
    tr.addEventListener("click", () => {
      const o = r.observations.find(
        (x) => x.index === Number(tr.dataset.idx)
      );
      if (o) showTrace(o, r);
    })
  );
}

function renderClipped(r) {
  $("#cnt-clipped").textContent = (r.clipped_points || []).length;
  $("#tab-clipped").innerHTML = (r.clipped_points || [])
    .map(
      (c) => `<div class="card clip">
        <h4>裁剪点（画布 ${c.canvas.x}, ${c.canvas.y}）</h4>
        ${c.explanation || c.clip_detail || "位于裁剪区域外"}<br/>
        来源：&lt;${c.source_tag}&gt;${
        c.source_id ? ` id="${c.source_id}"` : ""
      }，变换链：${
        (c.transform_chain || []).join(" → ") || "无"
      }
      </div>`
    )
    .join("") || '<p class="hint">本图没有被裁剪的点。</p>';
}

function renderAxes(r) {
  const blocks = ["x", "y"]
    .map((name) => {
      const a = r.axes[name];
      if (!a.available) {
        return `<div class="card err"><h4>${name.toUpperCase()} 轴：标定失败</h4>
          ${a.error}</div>`;
      }
      const ticks = a.ticks
        .map(
          (t) => `<tr><td class="num">${t.canvas_coord}</td>
          <td>${t.label}</td><td class="num">${t.parsed_value ?? "—"}</td>
          <td>${t.parse_note || ""}</td></tr>`
        )
        .join("");
      return `<div class="card">
        <h4>${name.toUpperCase()} 轴 · ${
        a.scale === "log" ? "对数轴（底 10）" : "线性轴"
      }</h4>
        <div>${a.direction_text}</div>
        <div>标定公式：<code>${a.formula}</code></div>
        <div>刻度覆盖画布范围：<code>[${a.domain_pixels
          .map((v) => v.toFixed(1))
          .join(", ")}] px</code>，拟合残差 RMSE=${a.fit_residual.toExponential(
        2
      )}</div>
        <ul class="plain">${(a.notes || []).map((n) => `<li>${n}</li>`).join("")}</ul>
        <table><thead><tr><th>画布投影(px)</th><th>可见标签</th>
        <th>解析数值</th><th>解析备注</th></tr></thead>
        <tbody>${ticks}</tbody></table>
      </div>`;
    })
    .join("");
  $("#tab-axes").innerHTML = blocks;
}

function renderErrors(r) {
  const errs = (r.errors || []).map(
    (e) => `<div class="card err"><h4>阻塞错误</h4>${e}</div>`
  );
  const warns = (r.warnings || []).map(
    (w) => `<div class="card warn"><h4>警告</h4>${w}</div>`
  );
  $("#cnt-errors").textContent = errs.length + warns.length;
  $("#tab-errors").innerHTML =
    errs.concat(warns).join("") ||
    '<p class="hint">没有错误或警告。</p>';
}

function renderBreaks(r) {
  $("#cnt-breaks").textContent =
    (r.breaks || []).length + (r.skipped_elements || []).length;
  const breaks = (r.breaks || []).map(
    (b) => `<div class="card"><h4>折线断点</h4>
      系列 &lt;${b.series_tag}&gt;${
      b.series_id ? ` id="${b.series_id}"` : ""
    }：${b.detail}</div>`
  );
  const skipped = (r.skipped_elements || []).map(
    (s) => `<div class="card warn"><h4>跳过的元素</h4>
      &lt;${s.element}&gt;${s.id ? ` id="${s.id}"` : ""}：${s.reason}</div>`
  );
  $("#tab-breaks").innerHTML =
    breaks.concat(skipped).join("") ||
    '<p class="hint">没有断点或被跳过的元素。</p>';
}

// ---------------- 换算过程弹窗 ----------------
function showTrace(o, r, isClipped) {
  const body = $("#modal-body");
  if (isClipped) {
    body.innerHTML = `
      <dl class="kv">
        <dt>画布坐标</dt><dd>(${o.canvas.x}, ${o.canvas.y}) px</dd>
        <dt>来源元素</dt><dd>&lt;${o.source_tag}&gt;${
      o.source_id ? ` id="${o.source_id}"` : ""
    }</dd>
        <dt>嵌套变换</dt><dd>${(o.transform_chain || []).join(" → ") || "无"}</dd>
      </dl>
      <div class="card warn">${o.explanation || o.clip_detail}</div>`;
    $("#modal-mask").classList.remove("hidden");
    return;
  }
  const traces = (o.trace || [])
    .map((t) => {
      const blocked = t.blocked
        ? `<div class="card err" style="margin-top:6px">${t.blocked}</div>`
        : "";
      return `<div class="trace-step ${t.blocked ? "blocked" : ""}">
        <b>${t.axis.toUpperCase()} 轴（${
        t.scale === "log" ? "对数" : "线性"
      }）</b><br/>
        原始画布点：(${t.raw_canvas.x}, ${t.raw_canvas.y}) px
        → 轴向投影：<code>${t.pixel_coord} px</code><br/>
        <code>${t.formula}</code>${blocked}
      </div>`;
    })
    .join("");
  const valueRows = ["x", "y"]
    .map((name) => {
      const v = o.values[name];
      if (!v)
        return `<tr><td>${name.toUpperCase()}</td><td colspan="3">阻塞：${
          (o.errors || []).find((e) => e.startsWith(name.toUpperCase())) ||
          "该维度不可用"
        }</td></tr>`;
      return `<tr><td>${name.toUpperCase()}（${v.scale}）</td>
        <td class="num">${v.value}</td>
        <td class="num">± ${v.uncertainty}</td>
        <td class="num">[${v.interval.join(", ")}]</td></tr>`;
    })
    .join("");
  body.innerHTML = `
    <dl class="kv">
      <dt>观测点编号</dt><dd>#${o.index}（${o.kind}）</dd>
      <dt>画布坐标</dt><dd>(${o.canvas.x}, ${o.canvas.y}) px</dd>
      <dt>来源元素</dt><dd>&lt;${o.source_tag}&gt;${
    o.source_id ? ` id="${o.source_id}"` : ""
  }</dd>
      <dt>嵌套变换链</dt><dd>${(o.transform_chain || []).join(" → ") || "无"}</dd>
    </dl>
    <h4 style="margin:8px 0 4px">分步换算</h4>
    ${traces || '<p class="hint">无可用换算。</p>'}
    <h4 style="margin:10px 0 4px">结果与误差范围</h4>
    <table><thead><tr><th>维度</th><th>恢复值</th><th>不确定度</th>
    <th>误差区间</th></tr></thead><tbody>${valueRows}</tbody></table>
    ${
      o.errors.length
        ? `<div class="card err" style="margin-top:8px">${o.errors
            .map((e) => `• ${e}`)
            .join("<br/>")}</div>`
        : ""
    }
    <p class="hint" style="margin-top:8px">
      不确定度来自刻度配对与像素量化先验（σ=1.2px）经标定斜率传播；
      对数轴先在 log 空间传播再映射回数值空间，区间因此非对称。</p>`;
  $("#modal-mask").classList.remove("hidden");
}

// ---------------- 协议子集 ----------------
async function showProtocol() {
  const p = await fetch("/api/protocol").then((r) => r.json());
  $("#protocol-body").innerHTML = `
    <h4>${p.name}</h4>
    <div class="card"><b>支持</b>
      <ul class="plain">${p.supported.map((x) => `<li>${x}</li>`).join("")}</ul>
    </div>
    <div class="card warn"><b>不支持（遇到即显式跳过/报错，不猜测）</b>
      <ul class="plain">${p.unsupported.map((x) => `<li>${x}</li>`).join("")}</ul>
    </div>`;
  $("#protocol-mask").classList.remove("hidden");
}

window.addEventListener("resize", () => {
  if (state.result) renderOverlay(state.result);
});

init().catch((err) => alert(`初始化失败：${err.message}`));
