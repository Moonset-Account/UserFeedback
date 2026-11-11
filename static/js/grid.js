/*
 * 本地电子表格式控件（仅服务本 RPA 演示，非通用表格系统）。
 *
 * 关键保真规则：
 * 1. 数据模型中 null=缺失(未写入)、""=空字符串，二者严格区分。
 * 2. 文本格式格逐字符保存；以 = 开头的公式样式字符串只按文本存储，不求值。
 * 3. 数值格式格保存 number，回读同时给出原始输入字面量。
 * 4. 粘贴命中任意只读格 -> 整单取消（不做部分写入），并显示明确错误。
 * 5. 隐藏列 display:none，不出现在可见标题接口中。
 */
(function () {
  "use strict";

  const state = {
    scenario: null,
    title: "",
    columns: [],        // {key, kind, hidden, frozenClass}
    rowCount: 0,
    frozenColCount: 0,
    readonly: new Set(), // "r|colKey"
    merged: [],
    // cells[r][colKey] = {value: null|""|string|number, format, rawLiteral, kind}
    cells: [],
    focused: null,       // {r, key}
    modalTarget: null,
    suppressInputCommit: false,
    // 经模态编辑器写入的格：其单行 input 无法表达制表符/换行，
    // 因此这些格的 blur 回写一律禁止，直到用户重新聚焦该格手动编辑。
    modalGuarded: new Set(),
    // 每格的显式类型提示：由自动化按列声明预置，回读时优先采用，
    // 避免对 EXP-001 这类非前导零标识做内容自动推断。
    kindHint: {},
  };

  const $ = (id) => document.getElementById(id);
  const toast = $("paste-toast");
  const statusEl = $("grid-status");

  function showToast(msg, ok) {
    toast.textContent = msg;
    toast.className = "show" + (ok ? " ok" : "");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => { toast.className = ""; }, 4200);
  }
  function setStatus(msg, cls) {
    statusEl.textContent = msg;
    statusEl.className = cls || "";
  }

  const NUM_RE = /^[+-]?(\d+\.\d+|\d+|\.\d+)([eE][+-]?\d+)?$/;
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  const LEADING_ZERO_RE = /^0\d+$/;

  function deriveKind(value, format) {
    if (value === null || value === undefined) return "missing";
    if (value === "") return "empty";
    if (format === "number") return "decimal";
    if (format === "text") {
      // 文本格式仍按内容提示子类型，回读大类为 text/id_text/date_text
      if (DATE_RE.test(value)) return "date_text";
      if (LEADING_ZERO_RE.test(value)) return "id_text";
      return "text";
    }
    // auto
    if (NUM_RE.test(value) && !LEADING_ZERO_RE.test(value)) return "decimal";
    if (DATE_RE.test(value)) return "date_text";
    if (LEADING_ZERO_RE.test(value)) return "id_text";
    return "text";
  }

  function isDangerous(s) {
    if (typeof s !== "string" || !s.length) return false;
    const t = s.trimStart();
    if (t[0] === "=") return true;
    if ("+-@".includes(t[0])) {
      if (NUM_RE.test(t)) return false; // 普通数字面量（如 -2.75）不算公式
      return true;                       // 非数字的运算符前缀串按危险处理
    }
    return false;
  }

  // ---------- 初始化 ----------
  async function init() {
    const params = new URLSearchParams(location.search);
    const scenario = params.get("scenario") || "demo-normal";
    state.scenario = scenario;

    const [cfgResp, metaResp] = await Promise.all([
      fetch(`/samples/${encodeURIComponent(scenario)}/grid_config.json`),
      fetch(`/samples/${encodeURIComponent(scenario)}/scenario.json`),
    ]);
    if (!cfgResp.ok) throw new Error("网格配置加载失败: " + cfgResp.status);
    const cfg = await cfgResp.json();
    const meta = metaResp.ok ? await metaResp.json() : { title: scenario };

    state.title = cfg.title || meta.title || scenario;
    state.frozenColCount = cfg.frozen_col_count;
    state.readonly = new Set((cfg.readonly_cells || []).map(
      (c) => `${c.row}|${c.column}`));
    state.merged = cfg.merged_headers || [];
    state.rowCount = parseInt(params.get("rows") || "0", 10);

    // 列信息来自场景的类型声明接口（公开接口，非真值）
    const schemaResp = await fetch(
      `/samples/${encodeURIComponent(scenario)}/schema.json`);
    if (!schemaResp.ok) throw new Error("列声明加载失败");
    const schema = await schemaResp.json();
    const hiddenSet = new Set(cfg.hidden_columns || []);
    state.columns = schema.columns.map((c, i) => ({
      key: c.key, kind: c.kind, hidden: hiddenSet.has(c.key),
      frozenClass: i < state.frozenColCount ? `frozen-${i}` : "",
    }));
    if (!state.rowCount) state.rowCount = schema.data_row_count;

    $("grid-title").textContent = state.title;
    $("scenario-tag").textContent = `场景：${scenario}`;

    buildHead();
    buildBody();
    bindGlobal();
    bindModal();
    setStatus("控件已加载，所有数据格初始为缺失(∅)，等待自动化录入。");
    window.dispatchEvent(new CustomEvent("rpa-grid-ready"));
  }

  // ---------- 表头：合并分组标题 + 列标题（两行） ----------
  function buildHead() {
    const thead = $("grid-thead");
    const visibleCols = state.columns.filter((c) => !c.hidden);

    const groupTr = document.createElement("tr");
    groupTr.className = "group-header";
    const rowHead0 = document.createElement("th");
    rowHead0.className = "row-head frozen-col";
    rowHead0.rowSpan = 2;
    rowHead0.textContent = "行号";
    groupTr.appendChild(rowHead0);

    const groupMap = new Map(state.merged.map((m) => [m.from, m]));
    let i = 0;
    while (i < visibleCols.length) {
      const col = visibleCols[i];
      const g = groupMap.get(col.key);
      const th = document.createElement("th");
      if (g) {
        const span = visibleCols.slice(i).findIndex(
          (c) => c.key === g.to) + 1;
        th.colSpan = span;
        th.textContent = g.label;
        if (col.frozenClass) th.className = "frozen-col " + col.frozenClass;
        groupTr.appendChild(th);
        i += span;
      } else {
        th.rowSpan = 2;
        th.textContent = col.key;
        if (col.frozenClass) th.className = "frozen-col " + col.frozenClass;
        groupTr.appendChild(th);
        i += 1;
      }
    }
    thead.appendChild(groupTr);

    const colTr = document.createElement("tr");
    colTr.className = "col-header";
    for (const c of visibleCols) {
      if (groupMap.has(c.key)) {
        const th = document.createElement("th");
        th.dataset.colKey = c.key;
        th.innerHTML = `${c.key}<span class="kind-badge">${kindLabel(c.kind)}</span>`;
        if (c.frozenClass) th.className = "frozen-col " + c.frozenClass;
        colTr.appendChild(th);
      }
    }
    thead.appendChild(colTr);
  }

  function kindLabel(k) {
    return { id_text: "标识/前导零", decimal: "十进制数", date_text: "日期文本",
      text: "文本", empty: "空串", missing: "缺失" }[k] || k;
  }

  // ---------- 表体 ----------
  function buildBody() {
    const tbody = $("grid-tbody");
    tbody.innerHTML = "";
    state.cells = [];
    const visibleCols = state.columns.filter((c) => !c.hidden);
    for (let r = 0; r < state.rowCount; r++) {
      const modelRow = {};
      for (const c of state.columns) {
        modelRow[c.key] = { value: null, format: "auto", rawLiteral: null,
          kind: "missing" };
      }
      state.cells.push(modelRow);

      const tr = document.createElement("tr");
      const rh = document.createElement("th");
      rh.className = "row-head frozen-col";
      rh.scope = "row";
      rh.textContent = String(r + 1);
      tr.appendChild(rh);

      for (const c of visibleCols) {
        const td = document.createElement("td");
        td.dataset.row = String(r);
        td.dataset.colKey = c.key;
        td.className = cellClass(r, c.key);
        const input = document.createElement("input");
        input.className = "cell-input";
        input.readOnly = isReadonly(r, c.key);
        input.dataset.row = String(r);
        input.dataset.colKey = c.key;
        input.value = "";
        td.appendChild(input);
        decorateCell(td, r, c.key);
        bindCell(td, input, r, c.key);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  function isReadonly(r, key) {
    return state.readonly.has(`${r}|${key}`);
  }

  function cellClass(r, key) {
    const m = state.cells[r][key];
    const col = state.columns.find((c) => c.key === key);
    const cls = [];
    if (col && col.frozenClass) cls.push("frozen-col", col.frozenClass);
    if (isReadonly(r, key)) cls.push("cell-readonly");
    cls.push("cell-" + m.kind);
    if (m.format === "number") cls.push("cell-number");
    if (m.format === "text") cls.push("cell-text");
    if (isDangerous(m.value)) cls.push("cell-danger");
    return cls.join(" ");
  }

  function decorateCell(td, r, key) {
    td.querySelectorAll(".ro-flag,.missing-mark,.cell-type-tag,.danger-dot")
      .forEach((n) => n.remove());
    if (isReadonly(r, key)) {
      const f = document.createElement("span");
      f.className = "ro-flag"; f.textContent = "只读";
      td.appendChild(f);
    }
    const m = state.cells[r][key];
    if (m.kind === "missing") {
      const mark = document.createElement("span");
      mark.className = "missing-mark"; mark.textContent = "∅ 缺失";
      td.appendChild(mark);
    }
    if (isDangerous(m.value)) {
      const d = document.createElement("span");
      d.className = "danger-dot"; d.title = "公式样式字符串，已按文本锁定";
      td.appendChild(d);
    }
    const tag = document.createElement("span");
    tag.className = "cell-type-tag";
    tag.textContent = m.format === "auto" ? "自动" :
      (m.format === "number" ? "数值" : "文本");
    td.appendChild(tag);
  }

  function refreshCell(r, key) {
    const td = cellEl(r, key);
    if (!td) return;
    const m = state.cells[r][key];
    td.className = cellClass(r, key);
    const input = td.querySelector(".cell-input");
    input.readOnly = isReadonly(r, key);
    // 数值格回填原始字面量（保留 1.20 尾零），其余按显示值。
    if (m.kind === "decimal" && m.rawLiteral != null) {
      input.value = m.rawLiteral;
    } else {
      input.value = m.value === null ? "" : String(m.value);
    }
    decorateCell(td, r, key);
  }

  function cellEl(r, key) {
    return document.querySelector(
      `td.cell[data-row="${r}"][data-col-key="${cssEscape(key)}"],` +
      `td[data-row="${r}"][data-col-key="${cssEscape(key)}"]`);
  }
  function cssEscape(s) {
    return String(s).replace(/(["\\])/g, "\\$1");
  }

  // ---------- 单元格交互 ----------
  function bindCell(td, input, r, key) {
    td.addEventListener("mousedown", () => focusCell(r, key));
    input.addEventListener("focus", () => focusCell(r, key));
    input.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      openModal(r, key);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commitInput(r, key, input); moveFocus(r, key, 1, 0); }
      else if (e.key === "Tab") { e.preventDefault(); commitInput(r, key, input);
        moveFocus(r, key, 0, e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape") { input.value = displayValue(r, key); }
    });
    input.addEventListener("blur", () => commitInput(r, key, input));
    // 用户/自动化直接在格内敲键：解除模态守卫，允许本次 blur 正常提交。
    // 仅响应真实键盘输入（isTrusted），避免程序化赋值/焦点同步误删守卫。
    input.addEventListener("input", (e) => {
      if (e.isTrusted) state.modalGuarded.delete(`${r}|${key}`);
    });
    // 粘贴拦截：命中只读 -> 整单取消
    input.addEventListener("paste", (e) => {
      const text = (e.clipboardData || window.clipboardData).getData("text/plain");
      e.preventDefault();
      handleGridPaste(r, key, text);
    });
  }

  function displayValue(r, key) {
    const m = state.cells[r][key];
    return m.value === null ? "" : String(m.value);
  }

  function focusCell(r, key) {
    if (state.focused) {
      const prev = cellEl(state.focused.r, state.focused.key);
      if (prev) prev.classList.remove("cell-focused");
    }
    state.focused = { r, key };
    const td = cellEl(r, key);
    if (td) {
      td.classList.add("cell-focused");
      const input = td.querySelector(".cell-input");
      input.__committed = false; // 新聚焦周期，允许提交
      $("fmt-select").value = state.cells[r][key].format;
      // 不自动 focus input，避免抢 Playwright 的键盘焦点控制；RPA 自行 click
      return input;
    }
    return null;
  }

  function moveFocus(r, key, dr, dc) {
    const vis = state.columns.filter((c) => !c.hidden);
    let idx = vis.findIndex((c) => c.key === key);
    let nr = r + dr, ni = idx + dc;
    if (ni < 0 || ni >= vis.length) return;
    if (nr < 0 || nr >= state.rowCount) return;
    const next = focusCell(nr, vis[ni].key);
    if (next) next.focus();
  }

  function commitInput(r, key, input) {
    if (isReadonly(r, key)) {
      input.value = displayValue(r, key);
      return { ok: false, reason: "只读格不允许写入" };
    }
    // 同一聚焦周期内 keydown(Tab/Enter) 已提交过，blur 不再二次提交，
    // 避免 refreshCell 回填后的规范化值覆盖原始字面量。
    if (input.__committed) {
      return { ok: true, kind: state.cells[r][key].kind, deduped: true };
    }
    // 模态编辑器打开期间，input 的 value 是被单行控件折叠过的，不能回写。
    if (state.modalTarget || state.suppressInputCommit) {
      input.value = displayValue(r, key);
      return { ok: true, kind: state.cells[r][key].kind, suppressed: true };
    }
    // 经模态写入的多行格：input 折叠了 \t/\n，其 blur 回写会破坏模型，
    // 持续守卫，仅当该格被重新聚焦手动编辑时才解除（见 focusCell）。
    if (state.modalGuarded.has(`${r}|${key}`)) {
      input.value = displayValue(r, key);
      return { ok: true, kind: state.cells[r][key].kind, guarded: true };
    }
    const m = state.cells[r][key];
    const text = input.value;
    const res = writeValue(r, key, text);
    if (res.ok) input.__committed = true;
    return res;
  }

  /*
   * 核心写入：根据格格式决定如何解释输入。
   * - number 格式：必须为合法十进制数，否则拒绝（保真失败，显式报错）。
   * - text 格式：逐字符保存，公式样式也只作文本。
   * - auto：纯数字转 number（但前导零整数保留文本），其余按字符串。
   */
  function writeValue(r, key, text, fromModal) {
    if (isReadonly(r, key)) {
      const reason = `第 ${r + 1} 行「${key}」是只读格，写入被控件拒绝`;
      setStatus(reason, "error");
      return { ok: false, reason };
    }
    // 硬保护：经模态写入的多行格，单行 input 无法表达其 \t/\n，
    // 只允许模态路径(fromModal)改写；普通 blur/键盘提交一律拒绝覆盖，
    // 从数据层杜绝焦点事件把保真值冲掉。
    if (!fromModal && state.modalGuarded.has(`${r}|${key}`)) {
      return { ok: false, reason: "该格含制表符/换行，仅可经多行编辑器修改",
               guarded: true };
    }
    const m = state.cells[r][key];
    if (text === "") {
      // 空字符串：显式区别于缺失
      m.value = ""; m.rawLiteral = ""; m.format = m.format === "auto" ? "text" : m.format;
      m.kind = "empty";
      refreshCell(r, key);
      return { ok: true, kind: "empty" };
    }
    if (m.format === "number") {
      if (!NUM_RE.test(text)) {
        const reason = `数值格式格无法保真接收非十进制数: ${preview(text)}，已拒绝该格`;
        setStatus(reason, "error");
        return { ok: false, reason };
      }
      const num = Number(text);
      m.value = num; m.rawLiteral = text; m.kind = "decimal";
      refreshCell(r, key);
      return { ok: true, kind: "decimal", rawLiteral: text };
    }
    if (m.format === "text") {
      m.value = text; m.rawLiteral = text;
      // 优先采用显式类型提示（RPA 按列声明预置），否则按内容保守细分。
      m.kind = state.kindHint[`${r}|${key}`] || deriveKind(text, "text");
      refreshCell(r, key);
      return { ok: true, kind: m.kind };
    }
    // auto
    if (NUM_RE.test(text) && !LEADING_ZERO_RE.test(text)) {
      m.value = Number(text); m.rawLiteral = text; m.kind = "decimal";
    } else {
      m.value = text; m.rawLiteral = text;
      m.kind = deriveKind(text, "auto");
    }
    refreshCell(r, key);
    return { ok: true, kind: m.kind };
  }

  function setFormat(r, key, format) {
    if (!["auto", "text", "number"].includes(format)) {
      return { ok: false, reason: "未知格格式: " + format };
    }
    if (isReadonly(r, key)) {
      return { ok: false, reason: "只读格不能修改格式" };
    }
    const m = state.cells[r][key];
    m.format = format;
    // 切换到 number 时，若现有文本不是数则拒绝切换并解释
    if (format === "number" && m.value !== null && m.value !== "" &&
        typeof m.value === "string" && !NUM_RE.test(m.value)) {
      m.format = "text";
      refreshCell(r, key);
      return { ok: false, reason: `内容 ${preview(m.value)} 不是十进制数，不能切数值格式` };
    }
    if (format === "number" && typeof m.value === "number") m.kind = "decimal";
    if (format === "text" && m.value !== null) m.kind = deriveKind(String(m.value), "text");
    refreshCell(r, key);
    if (state.focused) $("fmt-select").value = state.cells[state.focused.r][state.focused.key].format;
    return { ok: true };
  }

  // ---------- 粘贴：TSV 拆分 + 只读整单取消 ----------
  function splitTsv(text) {
    const rows = [[]];
    let field = "", inQ = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (inQ) {
        if (ch === '"') {
          if (text[i + 1] === '"') { field += '"'; i++; } else inQ = false;
        } else field += ch;
      } else if (ch === '"') inQ = true;
      else if (ch === "\t") { rows[rows.length - 1].push(field); field = ""; }
      else if (ch === "\n" || ch === "\r") {
        if (ch === "\r" && text[i + 1] === "\n") i++;
        rows[rows.length - 1].push(field); field = "";
        rows.push([]);
      } else field += ch;
    }
    rows[rows.length - 1].push(field);
    if (rows.length === 1 && rows[0].length === 1 && rows[0][0] === "" && text === "") return [];
    return rows;
  }

  function handleGridPaste(startR, startKey, clipboard) {
    const vis = state.columns.filter((c) => !c.hidden);
    const startCol = vis.findIndex((c) => c.key === startKey);
    const grid = splitTsv(clipboard);
    if (!grid.length) return;

    // 预检：任何目标只读/越界 -> 整单取消
    for (let dr = 0; dr < grid.length; dr++) {
      for (let dc = 0; dc < grid[dr].length; dc++) {
        const r = startR + dr, ci = startCol + dc;
        if (r >= state.rowCount || ci >= vis.length) {
          const reason = "粘贴区域超出网格边界，整单取消，未写入任何格";
          showToast(reason, false); setStatus(reason, "error");
          return { ok: false, reason, cellsWritten: 0 };
        }
        if (isReadonly(r, vis[ci].key)) {
          const reason = `粘贴区域包含只读格（第 ${r + 1} 行「${vis[ci].key}」），` +
            "按保真要求整单取消，未写入任何格；请改为逐格录入或调整计划";
          showToast(reason, false); setStatus(reason, "error");
          return { ok: false, reason, cellsWritten: 0 };
        }
      }
    }
    let written = 0;
    const details = [];
    for (let dr = 0; dr < grid.length; dr++) {
      for (let dc = 0; dc < grid[dr].length; dc++) {
        const r = startR + dr, key = vis[startCol + dc].key;
        const res = writeValue(r, key, grid[dr][dc]);
        if (res.ok) { written++; details.push({ r, key, ok: true }); }
        else details.push({ r, key, ok: false, reason: res.reason });
      }
    }
    setStatus(`粘贴完成：写入 ${written} 格`, "ok");
    return { ok: true, cellsWritten: written, details };
  }

  // ---------- 模态多行编辑器 ----------
  function openModal(r, key) {
    if (isReadonly(r, key)) {
      setStatus(`第 ${r + 1} 行「${key}」为只读格，不能打开编辑器`, "error");
      return { ok: false, reason: "readonly" };
    }
    state.modalTarget = { r, key };
    $("modal-meta").textContent =
      `目标：第 ${r + 1} 数据行 · 列「${key}」（内容原样保存，含 ⇥ 与 ⏎）`;
    const ta = $("modal-textarea");
    ta.value = displayValue(r, key);
    $("modal-mask").classList.add("open");
    setTimeout(() => { ta.focus(); ta.select(); }, 30);
    return { ok: true };
  }
  function closeModal() {
    $("modal-mask").classList.remove("open");
    state.modalTarget = null;
    state.suppressInputCommit = true;
    setTimeout(() => { state.suppressInputCommit = false; }, 0);
  }
  function commitModal() {
    if (!state.modalTarget) return;
    const { r, key } = state.modalTarget;
    const text = $("modal-textarea").value;
    // 模态写入前先解除该格守卫，再以 fromModal 身份写入，随后重新登记。
    state.modalGuarded.delete(`${r}|${key}`);
    const res = writeValue(r, key, text, true);
    state.modalGuarded.add(`${r}|${key}`);
    // 登记持久守卫：该格 input 无法表达多行，禁止其 blur 回写覆盖模型。
    state.modalGuarded.add(`${r}|${key}`);
    closeModal();
    const input = cellEl(r, key)?.querySelector(".cell-input");
    if (input) {
      input.value = displayValue(r, key); // 与模型保持一致（含折叠展示）
      input.focus();
    }
    return res;
  }
  function bindModal() {
    $("modal-ok").addEventListener("click", commitModal);
    $("modal-cancel").addEventListener("click", closeModal);
    $("modal-mask").addEventListener("mousedown", (e) => {
      if (e.target === $("modal-mask")) closeModal();
    });
    // 模态内粘贴不拆分：textarea 原生保留全部字符
  }
  function bindGlobal() {
    document.addEventListener("keydown", (e) => {
      if ($("modal-mask").classList.contains("open")) {
        if (e.key === "Escape") { e.preventDefault(); closeModal(); }
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commitModal(); }
      }
    });
    $("fmt-select").addEventListener("change", (e) => {
      if (!state.focused) return;
      const res = setFormat(state.focused.r, state.focused.key, e.target.value);
      if (!res.ok) {
        e.target.value = state.cells[state.focused.r][state.focused.key].format;
        setStatus(res.reason, "error");
      }
    });
  }

  function preview(s) {
    return JSON.stringify(String(s).replace(/\t/g, "⇥").replace(/\r/g, "␍")
      .replace(/\n/g, "⏎")).slice(0, 42);
  }

  // ---------- 对自动化开放的受控接口 ----------
  // 仅暴露查询与经由真实交互同源的操作；RPA 仍以真实键盘/剪贴板事件为主，
  // 这些接口用于定位、格式预置与回读，不替代输入动作。
  window.RPAGrid = {
    async ready() { return true; },
    describe() {
      return {
        scenario: state.scenario,
        rowCount: state.rowCount,
        visibleColumns: state.columns.filter((c) => !c.hidden)
          .map((c) => ({ key: c.key, kind: c.kind, frozen: !!c.frozenClass })),
        hiddenColumns: state.columns.filter((c) => c.hidden).map((c) => c.key),
        readonlyCells: [...state.readonly].map((s) => {
          const [r, key] = s.split("|");
          return { row: Number(r), column: key };
        }),
      };
    },
    locateCell(row, colKey) {
      if (!state.columns.some((c) => c.key === colKey && !c.hidden)) {
        return { found: false, reason: `列「${colKey}」不可见或不存在` };
      }
      const td = cellEl(row, colKey);
      if (!td) return { found: false, reason: "坐标越界" };
      const input = td.querySelector(".cell-input");
      return {
        found: true, readonly: isReadonly(row, colKey),
        selector: `td[data-row="${row}"][data-col-key="${cssEscape(colKey)}"] .cell-input`,
        format: state.cells[row][colKey].format,
      };
    },
    setCellFormat(row, colKey, format) { return setFormat(row, colKey, format); },
    // 预置该格的声明类型（id_text/date_text/text/decimal），回读按此返回。
    setKindHint(row, colKey, kind) {
      if (!state.columns.some((c) => c.key === colKey)) {
        return { ok: false, reason: "列不存在" };
      }
      if (!["id_text", "date_text", "text", "decimal",
            "empty", "missing"].includes(kind)) {
        return { ok: false, reason: "不支持的类型提示: " + kind };
      }
      state.kindHint[`${row}|${colKey}`] = kind;
      const m = state.cells[row][colKey];
      if (m.value !== null && m.value !== "") m.kind = kind;
      refreshCell(row, colKey);
      return { ok: true };
    },
    openEditor(row, colKey) { return openModal(row, colKey); },
    commitEditor() { return commitModal(); },
    cancelEditor() { closeModal(); return { ok: true }; },
    readCell(row, colKey) {
      if (!state.columns.some((c) => c.key === colKey)) {
        return { found: false, reason: "列不存在" };
      }
      const m = state.cells[row][colKey];
      const visible = !state.columns.find((c) => c.key === colKey).hidden;
      // 数值格回读优先返回原始输入字面量（保留 1.20 的尾零）；
      // 数值本身仍在 m.value，类型为 decimal，数值比对由自动化侧按 Decimal 处理。
      const displayText = m.value === null
        ? null
        : (m.kind === "decimal" && m.rawLiteral != null
          ? m.rawLiteral : String(m.value));
      return {
        found: true, visible,
        readonly: isReadonly(row, colKey),
        value: m.value,
        displayText,
        rawLiteral: m.rawLiteral,
        kind: m.kind,
        format: m.format,
        dangerous: isDangerous(m.value),
      };
    },
    readAllVisible() {
      const out = [];
      for (let r = 0; r < state.rowCount; r++) {
        for (const c of state.columns) {
          if (c.hidden) continue;
          const m = state.cells[r][c.key];
          out.push({ row: r, column: c.key, displayText:
            m.value === null ? null
              : (m.kind === "decimal" && m.rawLiteral != null
                ? m.rawLiteral : String(m.value)),
            rawLiteral: m.rawLiteral, kind: m.kind,
            readonly: isReadonly(r, c.key), dangerous: isDangerous(m.value) });
        }
    }
      return out;
    },
    // 仅供测试的复位入口（页面加载后所有格仍是缺失，正常 RPA 流程不调用）
    _resetAll() {
      state.modalGuarded.clear();
      state.kindHint = {};
      for (let r = 0; r < state.rowCount; r++) {
        for (const c of state.columns) {
          state.cells[r][c.key] = { value: null, format: "auto",
            rawLiteral: null, kind: "missing" };
          refreshCell(r, c.key);
        }
      }
      return { ok: true };
    },
  };

  init().catch((err) => {
    setStatus("初始化失败: " + err.message, "error");
    console.error(err);
  });
})();
