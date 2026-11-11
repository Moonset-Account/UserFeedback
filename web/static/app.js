/* 实验仪器 SOAP/MTOM 采集台前端逻辑（原生 JS，结果均来自真实接口执行） */
(function () {
  "use strict";

  const STATUS_CN = {
    complete: "完整结果（已入正式结果）",
    fault: "业务 Fault",
    rejected: "校验拒绝",
    fetch_error: "来源获取失败",
  };

  const els = {
    modeGroup: document.getElementById("mode-group"),
    scenario: document.getElementById("scenario-select"),
    runSingle: document.getElementById("run-single"),
    runDemo: document.getElementById("run-demo"),
    runAll: document.getElementById("run-all"),
    results: document.getElementById("results"),
    summary: document.getElementById("batch-summary"),
    hint: document.getElementById("progress-hint"),
    sourcePill: document.getElementById("source-pill"),
  };

  function currentMode() {
    return els.modeGroup.querySelector("input[name=mode]:checked").value;
  }

  function setBusy(busy, text) {
    [els.runSingle, els.runDemo, els.runAll].forEach((b) => (b.disabled = busy));
    els.hint.innerHTML = busy ? `<span class="spinner"></span>${text || "执行中…"}` : "结果来自真实执行，页面不预填任何成功数据。";
  }

  async function postJson(url, body) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return resp.json();
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderError(result) {
    const err = result.error;
    if (!err) return "";
    const isFault = result.status === "fault";
    const detailItems = [];
    const ev = err.evidence || {};
    if (ev.missing_references) {
      ev.missing_references.forEach((m) =>
        detailItems.push(`缺失引用：测量组 ${escapeHtml(m.measurement_id)} 通道 ${escapeHtml(m.channel_code)} → cid:${escapeHtml(m.missing_cid)}`));
    }
    if (ev.duplicates) {
      Object.entries(ev.duplicates).forEach(([cid, idxs]) =>
        detailItems.push(`重复 Content-ID：${escapeHtml(cid)}，冲突 part 序号 [${idxs.join(", ")}]，拒绝按顺序猜测`));
    }
    if (ev.dangling_cids) {
      detailItems.push(`孤立附件：${ev.dangling_cids.map(escapeHtml).join(", ")}`);
    }
    if (ev.rejected_groups) {
      ev.rejected_groups.forEach((g) => g.errors.forEach((e) =>
        detailItems.push(`测量组 ${escapeHtml(g.measurement_id)} 通道 ${escapeHtml(e.channel_code)}：[${escapeHtml(e.code)}] ${escapeHtml(e.detail)}`)));
    }
    if (ev.fault_detail && Object.keys(ev.fault_detail).length) {
      Object.entries(ev.fault_detail).forEach(([k, v]) =>
        detailItems.push(`${escapeHtml(k)} = ${escapeHtml(v)}`));
    }
    if (result.fault) {
      detailItems.unshift(`Fault 代码：${escapeHtml(result.fault.code)}；说明：${escapeHtml(result.fault.reason)}`);
    }
    return `
      <div class="error-box ${isFault ? "fault-box" : ""}">
        <span class="error-code">[${escapeHtml(err.code)}]</span>
        <span> ${escapeHtml(err.message)}</span>
        <p class="error-detail">${escapeHtml(err.detail)}</p>
        ${detailItems.length ? `<ul class="error-detail">${detailItems.map((d) => `<li>${d}</li>`).join("")}</ul>` : ""}
      </div>`;
  }

  function renderGroups(result) {
    if (!result.measurement_groups || !result.measurement_groups.length) return "";
    return result.measurement_groups.map((g) => `
      <div class="group-block">
        <h4>测量组：${escapeHtml(g.measurement_id)} <small>（仪器 ${escapeHtml(g.instrument_id)} · 采样时间 ${escapeHtml(g.sampled_at)}）</small></h4>
        <table class="measure-table">
          <thead><tr><th>通道（XML 字段）</th><th>单位</th><th>解析数值（附件来源）</th><th>摘要校验</th><th>附件字节证据</th></tr></thead>
          <tbody>
            ${g.channels.map((ch) => `
              <tr>
                <td class="mono">${escapeHtml(ch.code)}</td>
                <td>${escapeHtml(ch.unit)}</td>
                <td class="values-cell mono">[${ch.sample.values.map((v) => escapeHtml(v)).join(", ")}]
                  <br><small>点数 ${ch.sample.point_count} · 内嵌标识 ${escapeHtml(ch.sample.embedded_measurement_id)}</small></td>
                <td class="${ch.summary_check.match ? "check-ok" : "check-bad"}">
                  ${ch.summary_check.match ? "✓ 一致" : "✗ 不一致"}
                  <br><small class="mono">XML：${escapeHtml(ch.summary_check.digest_xml.slice(0, 20))}…<br>
                  重算：${escapeHtml(ch.summary_check.digest_recomputed.slice(0, 20))}…</small></td>
                <td>
                  cid：${escapeHtml(ch.cid)}<br>
                  ${ch.value_provenance.attachment ? `${ch.value_provenance.attachment.byte_length} 字节 ·
                  SHA-256：<span class="mono">${escapeHtml(ch.value_provenance.attachment.sha256.slice(0, 20))}…</span>` : "无附件"}
                </td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>`).join("");
  }

  function renderEvidence(result) {
    const raw = result.raw_evidence;
    if (!raw) return `<details class="evidence"><summary>来源证据</summary><p>该场景在取得可解析 MIME 前即失败，无 part 级证据。</p></details>`;
    const tiles = raw.parts.map((p) => `
      <div class="part-tile">
        <div class="kv">
          <span class="k">part 序号</span><strong>${p.part_index}</strong>
          <span class="k">Content-ID</span><span class="mono">${escapeHtml(p.content_id || "（根 part 无引用）")}</span>
          <span class="k">Content-Type</span><span>${escapeHtml(p.content_type)}</span>
          <span class="k">字节数</span><span>${p.byte_length}</span>
          <span class="k">SHA-256</span><span class="mono">${escapeHtml(p.sha256)}</span>
          <span class="k">Hex 预览</span><span class="mono">${escapeHtml(p.hex_preview)}${p.byte_length > 64 ? "…" : ""}</span>
        </div>
      </div>`).join("");
    return `
      <details class="evidence">
        <summary>来源证据：${raw.total_bytes} 字节原始响应（点击展开 part 清单与原始 MIME）</summary>
        <p><strong>来源：</strong>${escapeHtml(raw.source_kind)} · ${escapeHtml(raw.source_ref)}
          ${raw.http_status != null ? ` · HTTP ${raw.http_status}` : ""} · 外层类型 ${escapeHtml(raw.outer_content_type)}</p>
        <p><strong>整包 SHA-256：</strong><span class="mono">${escapeHtml(raw.raw_sha256)}</span></p>
        <div class="part-grid">${tiles}</div>
        <details><summary>原始 MIME（base64，可自行还原核对）</summary><pre>${escapeHtml(raw.raw_base64)}</pre></details>
      </details>`;
  }

  function card(result, defaultOpen) {
    const open = defaultOpen ? "open" : "";
    return `
      <div class="card ${open}" data-scenario="${escapeHtml(result.scenario)}">
        <div class="card-head" onclick="this.parentElement.classList.toggle('open')">
          <div class="card-title">
            <span class="caret">▶</span>
            <span class="badge ${result.status}">${escapeHtml(STATUS_CN[result.status] || result.status)}</span>
            ${escapeHtml(result.scenario)} — ${escapeHtml(result.scenario_title)}
          </div>
          <span class="card-meta">来源：${result.mode === "http" ? "HTTP 接口" : "样例文件"} · ${escapeHtml(result.started_at)}</span>
        </div>
        <div class="card-body">
          ${renderError(result)}
          ${renderGroups(result)}
          ${renderEvidence(result)}
        </div>
      </div>`;
  }

  function renderBatch(results, summary) {
    els.results.innerHTML = results.map((r, i) => card(r, i < 2)).join("");
    if (summary) {
      els.summary.hidden = false;
      els.summary.innerHTML = [
        ["complete", `正式结果 ${summary.complete}`],
        ["fault", `Fault ${summary.fault}`],
        ["rejected", `拒绝 ${summary.rejected}`],
        ["fetch_error", `获取失败 ${summary.fetch_error}`],
      ].filter(([, t]) => !t.endsWith(" 0")).map(([k, t]) => `<span class="chip ${k}">${t} / 共 ${summary.total}</span>`).join("");
    } else {
      els.summary.hidden = true;
    }
  }

  async function runSingle() {
    setBusy(true, `正在采集场景 ${els.scenario.value} …`);
    try {
      const data = await postJson("/api/run", { scenario: els.scenario.value, mode: currentMode() });
      if (!data.ok) throw new Error(data.message);
      renderBatch([data.result], null);
    } catch (e) {
      els.results.innerHTML = `<div class="empty-state">请求失败：${escapeHtml(e.message)}</div>`;
    } finally {
      setBusy(false);
    }
  }

  async function runBatch(scenarios, label) {
    setBusy(true, `${label}，共 ${scenarios.length} 个场景…`);
    const started = performance.now();
    try {
      const data = await postJson("/api/run-batch", { scenarios, mode: currentMode() });
      if (!data.ok) throw new Error(data.message);
      const elapsed = (performance.now() - started).toFixed(0);
      renderBatch(data.results, data.summary);
      els.hint.innerHTML = `本次 ${label} 真实执行完成，耗时 ${elapsed} ms（HTTP 走本地内置源站）。`;
    } catch (e) {
      els.results.innerHTML = `<div class="empty-state">请求失败：${escapeHtml(e.message)}</div>`;
      setBusy(false);
    } finally {
      setBusy(false);
    }
  }

  els.runSingle.addEventListener("click", runSingle);
  els.runDemo.addEventListener("click", () => runBatch(window.APP_BOOTSTRAP.demoScenarios, "运行示例（正常 + 边界）"));
  els.runAll.addEventListener("click", () => runBatch(window.APP_BOOTSTRAP.scenarios, "完整验收场景"));

  // 源站健康探测（仅展示，不影响文件模式）
  fetch("/api/scenarios")
    .then((r) => r.json())
    .then((d) => {
      els.sourcePill.textContent = `源站在线 · ${d.scenarios.length} 个场景`;
      els.sourcePill.classList.add("ok");
    })
    .catch(() => {
      els.sourcePill.textContent = "源站不可达";
      els.sourcePill.classList.replace("ok", "bad");
    });
})();
