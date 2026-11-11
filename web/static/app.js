/* WARC 提取工具前端逻辑：原生 JS，无构建步骤。 */
(function () {
  "use strict";

  const state = { jobs: new Map(), activeJobId: null, pollTimer: null };

  const $ = (sel) => document.querySelector(sel);
  const els = {
    runDemo: $("#run-demo"),
    runAcceptance: $("#run-acceptance"),
    uploadForm: $("#upload-form"),
    fileInput: $("#warc-file"),
    fileLabel: $("#file-label"),
    uploadBtn: $("#upload-btn"),
    jobList: $("#job-list"),
    cards: $("#cards"),
    resultMeta: $("#result-meta"),
    banner: $("#status-banner"),
    drawer: $("#raw-drawer"),
    drawerMask: $("#drawer-mask"),
    drawerClose: $("#drawer-close"),
    drawerBody: $("#drawer-body"),
  };

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function showBanner(text, kind) {
    els.banner.className = "status-banner " + kind;
    els.banner.textContent = text;
  }
  function hideBanner() { els.banner.className = "status-banner hidden"; }

  async function api(url, options) {
    const resp = await fetch(url, options);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }

  // ---- 任务调度 ----
  function schedulePoll() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(refreshJobs, 700);
  }

  async function refreshJobs() {
    let list;
    try {
      list = await api("/api/jobs");
    } catch (err) {
      return;
    }
    let anyActive = false;
    for (const summary of list.jobs) {
      state.jobs.set(summary.job_id, summary);
      if (summary.status === "running" || summary.status === "pending") anyActive = true;
    }
    renderJobList(list.jobs);
    if (state.activeJobId) {
      const active = state.jobs.get(state.activeJobId);
      if (active && (active.status === "running" || active.status === "pending")) {
        await loadJobDetail(state.activeJobId);
      }
    }
    if (!anyActive) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      if (state.activeJobId) await loadJobDetail(state.activeJobId);
    }
  }

  function renderJobList(jobs) {
    if (!jobs.length) {
      els.jobList.innerHTML = '<p class="empty-hint">暂无任务。点击「运行示例」或上传归档开始。</p>';
      return;
    }
    els.jobList.innerHTML = jobs.map((j) => {
      const pill = {
        pending: ["pending", "等待中"], running: ["running", "运行中"],
        done: ["done", "完成"], error: ["error", "失败"],
      }[j.status] || ["pending", j.status];
      const active = j.job_id === state.activeJobId ? " active" : "";
      const meta = j.duration_ms != null ? ` · ${j.duration_ms} ms` : "";
      return `<div class="job-row${active}" data-job="${esc(j.job_id)}">
        <span class="pill pill-${pill[0]}">${j.status === "running" ? '<span class="spinner"></span> ' : ""}${pill[1]}</span>
        <span class="job-label" title="${esc(j.label)}">${esc(j.label)}</span>
        <span class="job-meta">${j.record_count != null ? j.record_count + " 条记录" : ""}${meta}</span>
      </div>`;
    }).join("");
    els.jobList.querySelectorAll(".job-row").forEach((row) => {
      row.addEventListener("click", () => selectJob(row.dataset.job));
    });
  }

  async function selectJob(jobId) {
    state.activeJobId = jobId;
    renderJobList([...state.jobs.values()]);
    await loadJobDetail(jobId);
  }

  async function loadJobDetail(jobId) {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (job.status === "running" || job.status === "pending") {
      hideBanner();
      els.cards.innerHTML = `<p class="empty-hint"><span class="spinner"></span> 任务运行中：${esc(job.label)} ……</p>`;
      els.resultMeta.textContent = "";
      return;
    }
    if (job.status === "error") {
      showBanner(`任务失败：${job.error || "未知错误"}`, "error");
      els.cards.innerHTML = "";
      els.resultMeta.textContent = "";
      return;
    }
    hideBanner();
    renderResult(job);
  }

  // ---- 结果卡片 ----
  function fieldRows(fields) {
    if (!fields) return '<dd class="badge-na badge">无 HTML 字段</dd>';
    const order = ["title", "author", "published", "summary", "keywords", "body_char_count"];
    const labels = {
      title: "标题", author: "作者", published: "发布时间",
      summary: "摘要", keywords: "关键词", body_char_count: "正文字数",
    };
    return order.filter((k) => fields[k]).map((k) => {
      const f = fields[k];
      const value = Array.isArray(f.value) ? f.value.map(esc).join("、") : esc(f.value);
      return `<dt>${labels[k]}</dt>
      <dd>${value}
        <div class="evidence">证据：${esc(f.evidence.locator)} · ${esc(f.evidence.snippet)}</div>
      </dd>`;
    }).join("");
  }

  function digestBadge(entry) {
    if (entry.rec_type === "revisit") {
      return '<span class="badge badge-na">revisit 不校验本块摘要</span>';
    }
    if (entry.digest_ok === true) return '<span class="badge badge-ok">摘要校验通过</span>';
    if (entry.digest_ok === false) return '<span class="badge badge-bad">坏摘要</span>';
    return '<span class="badge badge-na">无摘要标签</span>';
  }

  function renderCard(entry) {
    const isRevisit = entry.rec_type === "revisit";
    const hasError = entry.errors.length > 0;
    const refLine = isRevisit && entry.reused_from_record_id
      ? `<div><span class="badge badge-ref">正文复用自 #${entry.reused_from_index}（${esc(entry.reference_rule)}）</span>
         <span class="id-line">引用记录 ID：${esc(entry.reused_from_record_id)}</span></div>`
      : "";
    const errors = entry.errors.length
      ? `<ul class="card-errors">${entry.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : "";
    const warnings = entry.warnings.length
      ? `<ul class="card-warnings">${entry.warnings.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : "";
    const reqLine = entry.request_record_ids.length
      ? `<div class="kv">关联 request：<code>${esc(entry.request_record_ids.join(", "))}</code></div>` : "";
    return `<article class="card ${isRevisit ? "revisit" : ""} ${hasError ? "has-error" : ""}">
      <div class="card-head">
        <span class="type-tag ${isRevisit ? "revisit-tag" : entry.rec_type === "request" ? "request-tag" : ""}">${esc(entry.rec_type)}</span>
        <span class="uri" title="${esc(entry.target_uri || "")}">${esc(entry.target_uri || "（无目标 URL）")}</span>
        ${digestBadge(entry)}
      </div>
      <div class="date-line">WARC-Date：${esc(entry.warc_date || "—")} · 记录 #${entry.index} · gzip 成员 ${entry.member_index} · 偏移 ${entry.file_offset}</div>
      <div class="id-line">WARC-Record-ID：${esc(entry.record_id || "—")}</div>
      ${refLine}
      <dl class="field-grid">${fieldRows(entry.fields)}</dl>
      ${warnings}${errors}
      ${reqLine}
      <div class="card-actions">
        <button class="btn btn-secondary btn-small" data-raw="${entry.index}" type="button">查看原始响应 / 来源证据</button>
      </div>
    </article>`;
  }

  function renderResult(job) {
    const result = job.result;
    const counts = Object.entries(result.record_type_counts)
      .map(([k, v]) => `${k} × ${v}`).join("，");
    const errCount = result.entries.filter((e) => e.errors.length).length;
    els.resultMeta.innerHTML =
      `文件：<b>${esc(job.label)}</b> · 共 <b>${result.total_records}</b> 条记录（${esc(counts)}）` +
      (result.member_errors.length ? ` · <b style="color:var(--err)">${result.member_errors.length} 个成员级错误</b>` : "") +
      ` · ${errCount} 条记录含错误 · 耗时 ${job.duration_ms} ms`;

    const memberBanner = result.member_errors.length
      ? `<details class="member-details" open><summary>成员/分帧级错误（${result.member_errors.length}）—— 已隔离，不影响后续记录</summary>
         <div class="detail-body"><ul class="card-errors">
         ${result.member_errors.map((m) => `<li>成员 ${m.member_index} @ 文件偏移 ${m.file_offset}：${esc(m.message)}</li>`).join("")}
         </ul></div></details>` : "";

    const visible = result.entries.filter((e) =>
      ["response", "revisit", "request", "resource", "unknown"].includes(e.rec_type));
    els.cards.innerHTML = memberBanner + visible.map(renderCard).join("");
    els.cards.querySelectorAll("button[data-raw]").forEach((btn) => {
      btn.addEventListener("click", () => openRaw(job.job_id, Number(btn.dataset.raw)));
    });
  }

  // ---- 原始响应抽屉 ----
  async function openRaw(jobId, index) {
    els.drawer.classList.add("open");
    els.drawerMask.classList.remove("hidden");
    els.drawerBody.innerHTML = '<p class="empty-hint"><span class="spinner"></span> 正在读取原始记录……</p>';
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(jobId)}/records/${index}`);
      const s = data.summary;
      const warcRows = data.warc_headers.map(([k, v]) =>
        `<tr><td>${esc(k)}</td><td><code>${esc(v)}</code></td></tr>`).join("");
      const httpRows = data.http_headers.map(([k, v]) =>
        `<tr><td>${esc(k)}</td><td><code>${esc(v)}</code></td></tr>`).join("");
      const errs = data.errors.length
        ? `<h4>记录级错误</h4><ul class="card-errors">${data.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : "";
      els.drawerBody.innerHTML = `
        <h4>归档定位</h4>
        <table>
          <tr><td>记录序号 / 类型</td><td>#${s.index} · ${esc(s.rec_type)}</td></tr>
          <tr><td>WARC-Record-ID（归档标识）</td><td><code>${esc(s.record_id || "—")}</code></td></tr>
          <tr><td>WARC-Target-URI（目标 URL）</td><td><code>${esc(s.target_uri || "—")}</code></td></tr>
          <tr><td>WARC-Date</td><td>${esc(s.warc_date || "—")}</td></tr>
          <tr><td>gzip 成员 / 文件偏移</td><td>成员 ${s.member_index} · 字节偏移 ${s.file_offset} · 块长 ${data.block_length}</td></tr>
        </table>
        <h4>WARC 头部</h4><table>${warcRows || '<tr><td colspan="2">无</td></tr>'}</table>
        <h4>HTTP 响应行 / 头部 ${esc(data.http_statusline || "")}</h4>
        <table>${httpRows || '<tr><td colspan="2">无 HTTP 头部</td></tr>'}</table>
        ${errs}
        <h4>原始负载（${data.payload_length} 字节，UTF-8 视图）</h4>
        <pre>${esc(data.payload_text) || "（空负载）"}</pre>`;
    } catch (err) {
      els.drawerBody.innerHTML = `<p class="card-errors">读取失败：${esc(err.message)}</p>`;
    }
  }

  function closeDrawer() {
    els.drawer.classList.remove("open");
    els.drawerMask.classList.add("hidden");
  }

  // ---- 事件绑定 ----
  els.fileInput.addEventListener("change", () => {
    if (els.fileInput.files.length) {
      els.fileLabel.textContent = els.fileInput.files[0].name;
      els.uploadBtn.disabled = false;
    }
  });

  els.uploadForm.addEventListener("submit", async (evt) => {
    evt.preventDefault();
    if (!els.fileInput.files.length) return;
    const form = new FormData();
    form.append("warc", els.fileInput.files[0]);
    els.uploadBtn.disabled = true;
    try {
      const data = await api("/api/upload", { method: "POST", body: form });
      schedulePoll();
      await selectJob(data.job_id);
    } catch (err) {
      showBanner(`上传失败：${err.message}`, "error");
    } finally {
      els.uploadBtn.disabled = false;
    }
  });

  async function runDemo() {
    els.runDemo.disabled = true;
    try {
      const data = await api("/api/demo", { method: "POST" });
      schedulePoll();
      await selectJob(data.jobs[0].job_id);
    } catch (err) {
      showBanner(`示例启动失败：${err.message}`, "error");
    } finally {
      els.runDemo.disabled = false;
    }
  }

  els.runDemo.addEventListener("click", runDemo);
  els.runAcceptance.addEventListener("click", async () => {
    els.runAcceptance.disabled = true;
    try {
      const data = await api("/api/acceptance", { method: "POST" });
      schedulePoll();
      await selectJob(data.job_id);
    } catch (err) {
      showBanner(`验收场景启动失败：${err.message}`, "error");
    } finally {
      els.runAcceptance.disabled = false;
    }
  });
  els.drawerClose.addEventListener("click", closeDrawer);
  els.drawerMask.addEventListener("click", closeDrawer);

  refreshJobs();
})();
