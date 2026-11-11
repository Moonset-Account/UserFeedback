/* 研究讲座音频目录采集器前端：所有数据均来自后端真实执行结果，无预填成功状态。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  scenarios: null,
  runs: [],
  currentRunId: null,
  currentRun: null,
  currentSnapshot: 0,
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options) {
  const resp = await fetch(path, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function setAlert(text, kind = "info") {
  const strip = $("#alert-strip");
  if (!text) { strip.hidden = true; strip.textContent = ""; return; }
  strip.hidden = false;
  strip.className = `alert-strip ${kind}`;
  strip.textContent = text;
}

function setProgress(text) {
  const p = $("#progress");
  if (!text) { p.hidden = true; $("#progress-text").textContent = ""; return; }
  p.hidden = false;
  $("#progress-text").textContent = text;
}

function statusBadge(status) {
  const map = {
    success: ["success", "成功"],
    completed_with_errors: ["completed_with_errors", "完成但有错误"],
    failed: ["failed", "失败"],
    running: ["running", "运行中"],
  };
  const [cls, label] = map[status] || ["running", status || "未知"];
  return `<span class="badge ${cls}">${label}</span>`;
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
async function init() {
  try {
    state.scenarios = await api("/api/scenarios");
    $("#server-status").textContent =
      `已连接 · 本地源站 ${state.scenarios.base_url}`;
    $("#server-status").className = "server-status ok";
    fillSnapshotHint();
  } catch (err) {
    $("#server-status").textContent = "服务连接失败：" + err.message;
    $("#server-status").className = "server-status bad";
    setAlert("无法连接后端服务：" + err.message, "error");
  }
  await refreshRuns();
}

function fillSnapshotHint() {
  // 在自定义输入框中给出可复制的源站地址提示
  const base = state.scenarios.base_url;
  $("#custom-sources").placeholder =
    `${base}/feeds/normal-rss-v1.xml RSS快照一\n${base}/feeds/full-rss-v2.xml RSS快照二`;
}

// ---------------------------------------------------------------------------
// 任务提交与轮询
// ---------------------------------------------------------------------------
async function submitRun(sources, scenario, timeout, probeMedia) {
  return api("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sources, scenario, timeout, probe_media: probeMedia }),
  });
}

async function runScenario(key, label) {
  const sc = state.scenarios.scenarios[key];
  if (!sc) { setAlert(`场景 ${key} 不存在`, "error"); return; }
  setProgress(`正在运行：${label} …`);
  setAlert(null);
  try {
    await submitRun(sc.sources, key, sc.timeout ?? 5, true);
    await waitForRun(label, 20000, key);
  } catch (err) {
    setAlert(`${label} 提交失败：${err.message}`, "error");
  } finally {
    setProgress(null);
  }
}

async function waitForRun(label, maxWaitMs = 20000, scenarioKey = null) {
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    await refreshRuns();
    // 精确匹配本次提交的场景（任务列表可能保留历史同场景运行）
    const candidates = scenarioKey
      ? state.runs.filter((r) => r.scenario === scenarioKey)
      : state.runs;
    const newest = candidates[0];
    if (newest) await openRun(newest.run_id);
    if (newest && ["success", "completed_with_errors", "failed"].includes(newest.status)) {
      return newest;
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  setAlert(`${label} 等待超时，请手动刷新任务列表`, "error");
  return null;
}

async function runDemo() {
  const btn = $("#run-demo");
  btn.disabled = true;
  try {
    await runScenario("demo-normal", "示例一 · 正常场景");
    await runScenario("demo-edge", "示例二 · 边界场景");
    const edge = state.runs.find((r) => r.scenario === "demo-edge");
    if (edge) await openRun(edge.run_id);
    setAlert("两个示例已依次执行完毕，可在任务列表切换查看，结果均保留。", "info");
  } finally {
    btn.disabled = false;
  }
}

async function refreshRuns() {
  try {
    const data = await api("/api/runs");
    state.runs = data.runs || [];
    renderRunList();
  } catch (err) {
    setAlert("刷新任务列表失败：" + err.message, "error");
  }
}

function renderRunList() {
  const ul = $("#run-list");
  if (!state.runs.length) {
    ul.innerHTML = '<li class="muted">暂无任务，结果均来自真实执行</li>';
    return;
  }
  ul.innerHTML = state.runs.map((r) => `
    <li data-id="${esc(r.run_id)}" data-scenario="${esc(r.scenario)}"
        class="${r.run_id === state.currentRunId ? "active" : ""}">
      <div class="run-item-title">${statusBadge(r.status)} ${esc(r.scenario)}</div>
      <div class="run-item-meta">
        ${esc((r.started_at || "").replace("T", " ").replace("+00:00", "Z"))}
        · ${r.elapsed_ms ?? "?"}ms<br>
        源 ${r.feed_count} · 条目 ${r.catalog_entry_count}
        · 冲突 ${r.conflict_count} · 错误 ${r.error_count}
      </div>
    </li>`).join("");
  ul.querySelectorAll("li[data-id]").forEach((li) => {
    li.addEventListener("click", () => openRun(li.dataset.id));
  });
}

async function openRun(runId) {
  state.currentRunId = runId;
  state.currentRun = await api(`/api/runs/${encodeURIComponent(runId)}`);
  state.currentSnapshot = 0;
  renderRunList();
  renderAll();
}

// ---------------------------------------------------------------------------
// 渲染：三栏 + 冲突 + 错误
// ---------------------------------------------------------------------------
function renderAll() {
  renderSnapshotPane();
  renderCatalogPane();
  renderLinksPane();
  renderErrors();
  const r = state.currentRun;
  $("#footer-meta").textContent =
    `任务 ${esc(r.run_id)} · 耗时 ${r.elapsed_ms}ms · 状态 ${r.status}`;
}

function parsedFeeds() {
  return (state.currentRun.feeds || []).filter((f) => f.parse_ok && f.feed);
}

function renderSnapshotPane() {
  const select = $("#snapshot-select");
  const feeds = parsedFeeds();
  if (!feeds.length) {
    select.innerHTML = "";
    $("#feed-pane").innerHTML =
      '<p class="muted">没有成功解析的订阅源，请查看底部“具体错误”。</p>';
    return;
  }
  select.innerHTML = feeds.map((f, i) => {
    const feed = f.feed;
    return `<option value="${i}">[${esc(f.version_label)}] ${esc(feed.title || feed.feed_url)} (${esc(feed.format)})</option>`;
  }).join("");
  select.value = String(Math.min(state.currentSnapshot, feeds.length - 1));
  select.onchange = () => {
    state.currentSnapshot = Number(select.value);
    renderSnapshotPane();
  };
  const wrap = feeds[select.value === "" ? 0 : Number(select.value)] || feeds[0];
  const feed = wrap.feed;
  const identityChips = feed.feed_identity_evidence.map((c) =>
    `<li><span class="tier ${c.tier === feed.feed_identity_tier ? "chosen" : ""}">F-P${c.tier}</span>
       <code>${esc(c.key)}</code><br><span class="muted">${esc(c.explanation)}</span></li>`).join("");
  $("#feed-pane").innerHTML = `
    <div class="snapshot-meta">
      <div><b>${esc(feed.title || "（无标题）")}</b> · ${esc(feed.format)}
        · ${feed.entries.length} 个条目 · 抓取地址 <span class="feed-id">${esc(feed.feed_url)}</span></div>
      <div class="muted">根 xml:base：<code>${esc(feed.declared_base || "（未声明）")}</code>
        · self：<code>${esc(feed.self_link || "无")}</code></div>
      <details><summary class="muted">订阅源身份判定依据（F-P 优先级）</summary>
        <ul class="evidence-list">${identityChips}</ul></details>
      <details><summary class="muted">展开完整原始 XML 文档（证据）</summary>
        <pre class="xml-doc">${esc(feed.raw_document)}</pre></details>
    </div>
    <div class="section-title">条目原始片段（与中间栏条目一一对应）</div>
    ${feed.entries.map((e, i) => `
      <details ${i < 2 ? "open" : ""}>
        <summary>#${i + 1} ${esc(e.title || "（无标题）")}
          <span class="tier chosen">P${e.chosen_identity_tier}</span></summary>
        <pre class="xml-doc">${esc(e.raw_fragment)}</pre>
      </details>`).join("")}`;
}

function availText(rec) {
  if (rec.available === null || rec.available === undefined)
    return '<span class="avail unknown">未探测</span>';
  if (rec.available)
    return `<span class="avail yes">可用</span> <span class="muted">${esc(rec.probe_method || "")}
      ${rec.http_status ? "HTTP " + rec.http_status : ""} ${rec.elapsed_ms ?? 0}ms</span>`;
  return `<span class="avail no">不可用</span> <span class="muted">${esc(rec.error || "原因未知")}
    ${rec.http_status ? "（HTTP " + rec.http_status + "）" : ""}</span>`;
}

function renderCatalogPane() {
  const cat = state.currentRun.catalog || {};
  const entries = cat.entries || [];
  const conflicts = cat.conflicts || [];
  if (!entries.length && !conflicts.length) {
    $("#catalog-pane").innerHTML = '<p class="muted">本任务没有产生目录条目。</p>';
    return;
  }
  const conflictHtml = conflicts.length ? `
    <div class="section-title">冲突与警告（${conflicts.length} 项，全部保留、不静默覆盖）</div>
    ${conflicts.map((c, i) => `
      <div class="conflict-card ${c.severity === "warning" ? "warning" : ""}">
        <div class="conflict-head" data-toggle="conf-${i}">
          [${c.severity === "warning" ? "警告" : "冲突"}] ${esc(c.title)}（${esc(c.kind)}）
          ｜涉及版本 ${(c.versions || []).join(", ") || "—"}
        </div>
        <div class="conflict-body" id="conf-${i}" hidden>
          <div>${esc(c.detail)}</div>
          ${(c.entries || []).map((en) => `
            <div style="margin-top:6px">
              <b>${esc(en.title || "（无标题）")}</b>
              <span class="muted">快照#${en.version_index} 条目#${en.entry_index}</span><br>
              <span class="muted">身份键：<code>${esc(en.chosen_identity_key)}</code></span>
              <pre class="conflict-fragment">${esc(en.raw_fragment)}</pre>
            </div>`).join("")}
        </div>
      </div>`).join("")}` : "";

  const entryHtml = entries.map((en, idx) => {
    const raw = en.raw_entry || {};
    // 前 3 个条目或存在媒体更换/多版本修订的条目默认展开，保证关键证据首屏可达
    const expanded = idx < 3
      || (en.media_changes && en.media_changes.length)
      || (en.revisions && en.revisions.length > 1);
    const idCandidates = (raw.identity_candidates || []).map((c) =>
      `<li><span class="tier ${c.tier === en.identity_tier ? "chosen" : ""}">P${c.tier}</span>
        <code>${esc(c.key)}</code><div class="muted">${esc(c.explanation)}</div></li>`).join("");
    const revisions = (en.revisions || []).map((rv) => `
      <li><span class="revision-tag">${esc(rv.version_label)}</span>
        标题「${esc(rv.title || "无标题")}」 ·
        附件：${rv.enclosure_urls.map((u) => `<code>${esc(u)}</code>`).join("，") || "无"}
        ${rv.updated ? " · updated " + esc(rv.updated) : ""}</li>`).join("");
    const changes = (en.media_changes || []).map((mc) => `
      <div class="media-change">
        <b>媒体更换（身份不变）</b>：${esc(mc.version_label)}<br>
        旧地址 <code>${esc(mc.old_url)}</code><br>
        新地址 <code>${esc(mc.new_url)}</code>
        <details><summary class="muted">查看新旧 XML 证据</summary>
          <pre class="conflict-fragment">${esc(mc.old_fragment)}</pre>
          <pre class="conflict-fragment">${esc(mc.new_fragment)}</pre>
        </details>
      </div>`).join("");
    const encRows = (raw.enclosures || []).map((enc) => `
      <div class="enclosure-row">
        <div>${availText(enc)} <span class="muted">来源：${esc(enc.origin)}
          ${enc.media_type ? " · " + esc(enc.media_type) : ""}</span></div>
        <div class="url">${esc(enc.resolved_url)}</div>
        ${enc.raw_url !== enc.resolved_url
          ? `<div class="base-chain">原始相对地址：${esc(enc.raw_url)}<br>
             xml:base 链：${(enc.base_chain || []).map(esc).join(" → ") || "（无）"}</div>`
          : ""}
        ${enc.redirect_chain && enc.redirect_chain.length
          ? `<div class="base-chain">重定向：${enc.redirect_chain.map(esc).join(" → ")}</div>` : ""}
      </div>`).join("");
    const covers = (raw.covers || []).map((cv) => {
      // 仅对与页面同源（内置本地源站）的封面渲染缩略图，外部源站保留 URL 证据即可
      let thumb = "";
      try {
        const u = new URL(cv.resolved_url, location.origin);
        if (u.origin === location.origin) {
          thumb = `<img class="cover-thumb" src="/source${u.pathname}"
                 alt="封面证据" onerror="this.style.display='none'">`;
        }
      } catch (_) { /* 非法 URL 不渲染缩略图 */ }
      return `
      <div class="cover-row">
        ${availText(cv)} <span class="muted">封面归属：${cv.owner === "feed" ? "订阅源级" : "条目级"} · ${esc(cv.origin)}</span>
        <div class="url">${esc(cv.resolved_url)}</div>
        ${thumb}
      </div>`;
    }).join("");
    return `
      <div class="entry-card">
        <div class="entry-head" data-toggle="entry-${idx}">
          <span class="entry-title">${esc(en.title || "（无标题）")}</span>
          <span class="entry-tiers">
            ${[1, 2, 3, 5].map((t) =>
              `<span class="tier ${t === en.identity_tier ? "chosen" : ""}">P${t}</span>`).join("")}
          </span>
        </div>
        <div class="entry-detail" id="entry-${idx}" ${expanded ? "" : "hidden"}>
          <div class="kv">
            <span class="k">身份键</span><span><code>${esc(en.identity_key)}</code></span>
            <span class="k">判定说明</span><span class="muted">${esc(en.identity_evidence_explanation)}</span>
            <span class="k">permalink</span><span>${(en.current_permalinks || []).map((u) => `<code>${esc(u)}</code>`).join("<br>") || "无"}</span>
            <span class="k">修订版本</span><span>首次 v${en.first_seen_version} → 最新 v${en.latest_version}（共 ${en.revisions.length} 个 revision）</span>
          </div>
          <details><summary class="muted">身份线索优先级（P1–P5，标题与媒体 URL 不参与）</summary>
            <ul class="evidence-list">${idCandidates}</ul></details>
          <details><summary class="muted">修订历史（同一条目的更正版本归并于此）</summary>
            <ul class="evidence-list">${revisions}</ul></details>
          ${changes}
          <div class="section-title">enclosure 可用性证据</div>
          ${encRows || '<span class="muted">无附件</span>'}
          ${covers ? `<div class="section-title">封面关联证据</div>${covers}` : ""}
        </div>
      </div>`;
  }).join("");

  $("#catalog-pane").innerHTML = conflictHtml +
    `<div class="section-title">目录实体（${entries.length} 个，按 feed 作用域 + 身份键归并）</div>` +
    entryHtml;
  bindToggles();
}

function renderLinksPane() {
  const feeds = parsedFeeds();
  if (!feeds.length) {
    $("#links-pane").innerHTML = '<p class="muted">无链接数据。</p>';
    return;
  }
  const html = feeds.map((wrap) => {
    const feed = wrap.feed;
    const feedLinks = (feed.links || []).map((l) => linkRow(l)).join("");
    const entryLinks = feed.entries.map((e, i) => `
      <details>
        <summary>#${i + 1} ${esc(e.title || "（无标题）")} 的链接（${e.links.length}）</summary>
        ${e.links.map((l) => linkRow(l)).join("")}
        ${(e.enclosures || []).map((enc) => `
          <div class="link-row">
            <span class="rel-chip">enclosure</span>
            <span class="muted">${esc(enc.origin)}</span>
            <div class="url">${esc(enc.resolved_url)}</div>
            <div class="base-chain">用途：音频附件（可用性见中栏）
              ${enc.raw_url !== enc.resolved_url ? "；相对地址经 xml:base 解析" : ""}</div>
          </div>`).join("")}
      </details>`).join("");
    return `
      <div class="section-title">[${esc(wrap.version_label)}] ${esc(feed.title || feed.feed_url)}</div>
      <details open><summary>订阅源级链接（${feed.links.length}）</summary>${feedLinks || '<span class="muted">无</span>'}</details>
      ${entryLinks}`;
  }).join("");
  $("#links-pane").innerHTML = html + `
    <div class="section-title">链接关系说明（协议子集）</div>
    <ul class="evidence-list">
      <li><b>alternate</b>：规范 HTML 页面，permalink 候选，仅在 feed 作用域内有效</li>
      <li><b>self</b>：订阅源自身地址，用于 feed 身份（F-P2）</li>
      <li><b>enclosure</b>：音频附件，采集器只探测可用性，不下载不播放</li>
      <li><b>related / via / payment / edit / icon / logo</b>：按原 rel 记录，采集器不跟随 edit/payment</li>
      <li>缺失 rel 的链接标记为 unknown，不臆测用途</li>
    </ul>`;
}

function linkRow(l) {
  return `
    <div class="link-row">
      <span class="rel-chip">${esc(l.rel)}</span>
      <span class="muted">${esc(l.purpose_label)}</span>
      <div class="url">${esc(l.resolved_url)}</div>
      <div class="base-chain">
        原始 href：<code>${esc(l.raw_href)}</code>
        ${l.link_type ? " · type=" + esc(l.link_type) : ""}
        ${l.is_permalink ? " · isPermaLink=true" : ""}<br>
        xml:base 链：${(l.base_chain || []).map(esc).join(" → ") || "（无声明，相对地址以订阅源地址兜底）"}
      </div>
      <details><summary class="muted">链接元素 XML 证据</summary>
        <pre class="conflict-fragment">${esc(l.source_fragment)}</pre></details>
    </div>`;
}

function renderErrors() {
  // 错误以卡片形式插到中栏顶部下方（与冲突并列，保证首屏可见）
  const errors = state.currentRun.errors || [];
  const pane = $("#catalog-pane");
  if (!errors.length) return;
  const html = `
    <div class="section-title" style="color:var(--err)">具体错误（${errors.length} 条，均含阶段与来源）</div>
    ${errors.map((e) => `
      <div class="error-card">
        <div><span class="stage">[${esc(e.stage)}]</span> ${esc(e.error_type || "Error")}
          ${e.version_label ? "· " + esc(e.version_label) : ""}</div>
        <div class="msg">${esc(e.message)}</div>
        <div class="src">${esc(e.source || "")} ${e.entry_title ? "· 条目：" + esc(e.entry_title) : ""}</div>
      </div>`).join("")}`;
  pane.insertAdjacentHTML("afterbegin", html);
}

function bindToggles() {
  document.querySelectorAll("[data-toggle]").forEach((el) => {
    el.onclick = () => {
      const target = document.getElementById(el.dataset.toggle);
      if (target) target.hidden = !target.hidden;
    };
  });
}

// ---------------------------------------------------------------------------
// 自定义任务
// ---------------------------------------------------------------------------
$("#run-demo").addEventListener("click", runDemo);
$("#run-full").addEventListener("click", () =>
  runScenario("full-acceptance", "完整验收场景（含 2s 超时源，预计 10 秒内）"));
$("#refresh-runs").addEventListener("click", refreshRuns);
$("#run-custom").addEventListener("click", async () => {
  const text = $("#custom-sources").value.trim();
  if (!text) { setAlert("请先输入至少一个订阅源地址", "error"); return; }
  const sources = text.split("\n").map((line, i) => {
    const parts = line.trim().split(/\s+/);
    return { url: parts[0], version_label: parts[1] || `自定义${i + 1}` };
  }).filter((s) => s.url);
  const timeout = Number($("#custom-timeout").value) || 5;
  const probe = $("#custom-probe").checked;
  setProgress("正在运行自定义任务 …");
  try {
    await submitRun(sources, "custom", timeout, probe);
    await waitForRun("自定义任务", 20000, "custom");
  } catch (err) {
    setAlert("自定义任务失败：" + err.message, "error");
  } finally {
    setProgress(null);
  }
});

init();

// 后台轻量轮询：任务由后端线程异步执行，定期刷新列表让新任务/终态自动出现。
// 正在查看某任务详情时保留选中，不打断证据阅读。
setInterval(async () => {
  const previousIds = state.runs.map((r) => r.run_id);
  try {
    const data = await api("/api/runs");
    const next = data.runs || [];
    const changed = next.length !== previousIds.length
      || next.some((r, i) => r.run_id !== previousIds[i]
        || (state.runs[i] && r.status !== state.runs[i].status));
    if (changed) {
      state.runs = next;
      renderRunList();
    }
  } catch (_) { /* 轮询失败静默，下次再试 */ }
}, 1000);
