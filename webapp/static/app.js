/* IIIF 数字标本清单采集器前端逻辑：所有数据来自后端真实采集接口。 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let sources = [];
  let selectedSource = null;
  let currentRunId = null;
  let imageIdOwnerCount = {};

  // ---------------- 通用工具 ---------------- //
  function toast(msg, ms) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.add("hidden"), ms || 3000);
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function setBusy(busy, text) {
    ["run-demo", "run-custom", "run-full"].forEach((id) => { $(id).disabled = busy; });
    if (busy) toast(text || "正在采集…", 12000);
  }

  async function api(url, options) {
    const resp = await fetch(url, options);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `请求失败 ${resp.status}`);
    return data;
  }

  // ---------------- 样例源列表 ---------------- //
  async function loadSources() {
    try {
      const data = await api("/api/sources");
      sources = data.scenarios;
      const list = $("source-list");
      list.innerHTML = "";
      sources.forEach((s) => {
        const div = document.createElement("div");
        div.className = "source-item";
        div.dataset.url = s.manifest_url;
        div.innerHTML = `
          <div class="title">${esc(s.title)}</div>
          <div class="meta">虚构样例 · 图像 ${s.image_count} 张 · 点击选用</div>
          <div class="url">${esc(s.manifest_url)}</div>`;
        div.addEventListener("click", () => {
          selectedSource = s.manifest_url;
          $("source-input").value = s.manifest_url;
          document.querySelectorAll(".source-item").forEach((n) => n.classList.remove("active"));
          div.classList.add("active");
        });
        list.appendChild(div);
      });
      $("subset-list").innerHTML = data.supported_subset
        .map((t) => `<li>${esc(t)}</li>`).join("");
    } catch (err) {
      $("source-list").innerHTML = `<p class="sev-error">源站加载失败：${esc(err.message)}</p>`;
    }
  }

  // ---------------- 运行 ---------------- //
  async function runOnce(source, scenario) {
    return api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, scenario: scenario || null }),
    });
  }

  async function runDemo() {
    setBusy(true, "正在依次运行正常与边界场景（目标 15 秒内）…");
    const t0 = performance.now();
    try {
      const data = await api("/api/demo", { method: "POST" });
      const elapsed = ((performance.now() - t0) / 1000).toFixed(2);
      await refreshRuns();
      const first = data.runs[0];
      if (first) selectRun(first.run_id);
      toast(`示例完成，用时 ${elapsed} 秒，两次结果均已保留`);
    } catch (err) {
      toast("运行示例失败：" + err.message, 6000);
    } finally {
      setBusy(false);
    }
  }

  async function runCustom() {
    const source = $("source-input").value.trim();
    if (!source) { toast("请先输入清单 URL 或本地文件路径"); return; }
    setBusy(true, "正在采集…");
    try {
      const run = await runOnce(source, "custom");
      await refreshRuns();
      selectRun(run.run_id);
      toast(run.result.status === "failed"
        ? "任务已记录：清单获取失败，详见错误信息"
        : "采集完成");
    } catch (err) {
      toast("采集请求失败：" + err.message, 6000);
    } finally {
      setBusy(false);
    }
  }

  async function runFull() {
    const full = sources.find((s) => s.slug === "full-acceptance");
    if (!full) { toast("完整验收场景尚未加载"); return; }
    setBusy(true, "正在运行完整验收场景…");
    try {
      const run = await runOnce(full.manifest_url, "full-acceptance");
      await refreshRuns();
      selectRun(run.run_id);
      toast("完整验收场景采集完成");
    } catch (err) {
      toast("运行失败：" + err.message, 6000);
    } finally {
      setBusy(false);
    }
  }

  // ---------------- 运行记录 ---------------- //
  async function refreshRuns() {
    const data = await api("/api/runs");
    const ul = $("run-list");
    ul.innerHTML = "";
    if (!data.runs.length) {
      ul.innerHTML = `<li class="muted">暂无记录</li>`;
      return;
    }
    data.runs.forEach((run) => {
      const li = document.createElement("li");
      li.dataset.runId = run.run_id;
      const st = run.result.status;
      const name = run.scenario === "custom" ? "自定义输入" :
        (run.scenario || "采集任务");
      li.innerHTML = `
        <div class="run-line1">
          <span class="run-scenario">${esc(name)} <span class="badge badge-${esc(st)}">${esc(st)}</span></span>
          <span class="run-time">${esc(run.created_at)}</span>
        </div>
        <div class="run-source">${esc(run.source)}</div>`;
      li.addEventListener("click", () => selectRun(run.run_id));
      ul.appendChild(li);
    });
  }

  async function selectRun(runId) {
    currentRunId = runId;
    document.querySelectorAll("#run-list li").forEach((li) =>
      li.classList.toggle("active", li.dataset.runId === runId));
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    renderResult(run);
  }

  // ---------------- 结果渲染 ---------------- //
  function thumbUrl(img) {
    if (img.thumbnail_url) return img.thumbnail_url;
    if (img.image_id) return img.image_id;
    return "";
  }

  function renderResult(run) {
    const r = run.result;
    $("result-empty").classList.add("hidden");
    $("result-view").classList.remove("hidden");

    const banner = $("status-banner");
    banner.className = `banner banner-${r.status}`;
    const bannerText = {
      ok: "✅ 采集成功：全部结构均可解析",
      partial: "⚠️ 部分成功：存在无法解析的内容（见下方具体路径），其余页面已正常采集",
      failed: "❌ 采集失败：清单无法获取或解析",
    }[r.status] || r.status;
    banner.textContent = bannerText;

    $("manifest-title").textContent = r.manifest_label || r.manifest_id || "(无标题清单)";
    $("manifest-source").textContent = `来源：${r.source}（${r.source_kind}） · Manifest ID: ${r.manifest_id || "缺失"}`;

    const s = r.stats;
    $("stats-grid").innerHTML = [
      ["Canvas 页面", s.canvas_count],
      ["图像资源", s.image_count],
      ["章节 Range", s.range_count],
      ["缺图像描述", s.missing_image_label_count],
      ["Choice 候选", s.choice_alternative_count],
      ["循环章节", s.cyclic_range_count],
      ["错误", s.error_count],
      ["警告", s.warning_count],
      ["共享图像", s.shared_image_id_count],
    ].map(([lbl, num]) => `<div class="stat"><div class="num">${num}</div><div class="lbl">${lbl}</div></div>`).join("");

    // 统计同一图像 ID 被多少 Canvas 使用
    imageIdOwnerCount = {};
    r.canvases.forEach((c) => c.images.forEach((img) => {
      if (img.image_id) imageIdOwnerCount[img.image_id] = (imageIdOwnerCount[img.image_id] || 0) + 1;
    }));

    renderCanvases(r);
    renderRanges(r);
    renderIssues(r);
  }

  function renderCanvases(r) {
    const host = new URL(r.source, location.origin).origin;
    const wrap = $("canvas-list");
    if (!r.canvases.length) {
      wrap.innerHTML = `<p class="muted">未采集到任何 Canvas 页面。</p>`;
      return;
    }
    wrap.innerHTML = r.canvases.map((canvas) => {
      const imgs = canvas.images.length ? canvas.images.map((img) => {
        const tags = [];
        if (img.is_choice_alternative) tags.push(`<span class="tag tag-choice">Choice 候选</span>`);
        const noLabel = img.issues.some((i) => i.code === "MISSING_IMAGE_LABEL");
        if (noLabel) tags.push(`<span class="tag tag-nolabel">缺描述</span>`);
        if (img.image_id && imageIdOwnerCount[img.image_id] > 1) {
          tags.push(`<span class="tag tag-shared">跨 ${imageIdOwnerCount[img.image_id]} 页共用</span>`);
        }
        const src = thumbUrl(img);
        // 同源相对地址补全；外部地址直接使用
        const fullSrc = src.startsWith("http") ? src : host + src;
        return `
          <div class="thumb ${img.is_choice_alternative ? "choice" : ""} ${noLabel ? "no-desc" : ""}">
            <img src="${esc(fullSrc)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'muted',textContent:'缩略图不可用',style:'height:112px;display:flex;align-items:center;justify-content:center;font-size:11px'}))" />
            <div class="img-label">${tags.join("")}${esc(img.label || "（无图像描述）")}</div>
            <div class="img-meta">${esc(img.format || "")} ${img.width ? img.width + "×" + img.height : ""}</div>
            <div class="img-meta path-text" title="${esc(img.image_id || "")}">图像ID: ${esc((img.image_id || "缺失").slice(-46))}</div>
            <div class="img-meta path-text">证据路径: ${esc(img.source_path)}</div>
          </div>`;
      }).join("") : `<p class="muted">该 Canvas 未解析到图像（见问题表）</p>`;
      return `
        <div class="canvas-card">
          <div class="canvas-head">
            <span class="canvas-label">${esc(canvas.label || "（无页面标题）")}</span>
          </div>
          <div class="canvas-id path-text">Canvas ID: ${esc(canvas.canvas_id)} · 证据路径 ${esc(canvas.source_path)}</div>
          <div class="thumb-row">${imgs}</div>
        </div>`;
    }).join("");
  }

  function renderRanges(r) {
    const wrap = $("range-tree");
    if (!r.ranges.length) {
      wrap.innerHTML = `<p class="muted">清单没有 structures（章节）。</p>`;
      return;
    }
    const byId = Object.fromEntries(r.ranges.map((x) => [x.range_id, x]));
    const childOf = {};
    r.ranges.forEach((x) => childOf[x.range_id] = []);
    r.ranges.forEach((x) => x.child_range_ids.forEach((cid) => {
      if (byId[cid] && childOf[x.range_id]) childOf[x.range_id].push(cid);
    }));
    // 没有父 Range 的节点作为顶层：含正常根，也含循环/悬空导致挂不进树的 Range
    const roots = r.ranges
      .filter((x) => x.parent_range_ids.length === 0)
      .map((x) => x.range_id);
    // 兜底：任何未被树根覆盖的节点（理论上不应发生）也补到顶层，保证全部可见
    const reachable = new Set();
    const mark = (id, seen) => {
      if (seen.has(id) || !byId[id]) return;
      seen.add(id); reachable.add(id);
      childOf[id].forEach((cid) => mark(cid, seen));
    };
    roots.forEach((id) => mark(id, new Set()));
    r.ranges.forEach((x) => {
      if (!reachable.has(x.range_id)) roots.push(x.range_id);
    });

    function nodeHtml(id, seen) {
      const node = byId[id];
      if (!node) return "";
      const cycle = seen.has(id);
      const nextSeen = new Set(seen); nextSeen.add(id);
      const refs = node.referenced_canvas_ids.map((cid) => {
        const c = r.canvases.find((x) => x.canvas_id === cid);
        return `<div class="range-ref">└ <span class="canvas-link">▦ ${esc(c ? c.label || cid : cid)}</span></div>`;
      }).join("");
      const children = cycle ? "" : childOf[id].map((cid) => `<li>${nodeHtml(cid, nextSeen)}</li>`).join("");
      const cyc = node.cyclic ? `<span class="cyclic-mark">⟳ 循环引用（回边已跳过）</span>` : "";
      return `
        <div class="range-node">
          <span class="range-title">📖 ${esc(node.label || node.range_id)}</span>${cyc}
          <div class="path-text muted">Range ID: ${esc(node.range_id)} · 路径 ${esc(node.source_path)}</div>
          ${refs}
        </div>
        ${children ? `<ul>${children}</ul>` : ""}`;
    }
    wrap.innerHTML = `<ul>${roots.map((id) => `<li>${nodeHtml(id, new Set())}</li>`).join("")}</ul>`;
  }

  function renderIssues(r) {
    const tbody = $("issues-table").querySelector("tbody");
    if (!r.issues.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">没有发现问题。</td></tr>`;
      return;
    }
    tbody.innerHTML = r.issues.map((i) => `
      <tr>
        <td class="sev-${esc(i.severity)}">${i.severity === "error" ? "错误" : "警告"}</td>
        <td><code>${esc(i.code)}</code></td>
        <td>${esc(i.message)}</td>
        <td class="path-text">${esc(i.path || "（清单级）")}</td>
        <td class="path-text">${esc(i.ref || "")}</td>
      </tr>`).join("");
  }

  // ---------------- 事件绑定 ---------------- //
  $("run-demo").addEventListener("click", runDemo);
  $("run-custom").addEventListener("click", runCustom);
  $("run-full").addEventListener("click", runFull);
  $("clear-runs").addEventListener("click", async () => {
    await api("/api/runs", { method: "DELETE" });
    currentRunId = null;
    $("result-empty").classList.remove("hidden");
    $("result-view").classList.add("hidden");
    await refreshRuns();
    toast("运行记录已清空");
  });

  loadSources();
  refreshRuns();
})();
