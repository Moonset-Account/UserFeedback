/*
 * 模拟实验控制台前端逻辑（纯虚构数据，无任何真实设备通信）。
 * 菜单结构与规则来自公开只读接口 GET /lab/api/snapshot。
 */
(function () {
  "use strict";

  const SNAPSHOT_URL = "/lab/api/snapshot";
  const $ = (sel) => document.querySelector(sel);
  const menuLayer = $("#menu-layer");
  const eventLog = $("#event-log");
  let snapshot = null;
  let logSeq = 0;
  // 记录最近使用的命令 id（用于搜索排序中的近期加权，确定性、无随机数）。
  const recentUsage = [];

  // ---------- 初始化 ----------
  async function refreshSnapshot() {
    const resp = await fetch(SNAPSHOT_URL);
    if (!resp.ok) throw new Error("快照接口异常: " + resp.status);
    snapshot = await resp.json();
    renderMenubar();
    renderContext();
    renderShortcutConflicts();
    renderDevices();
    return snapshot;
  }

  function renderContext() {
    $("#current-context").textContent = snapshot.context_label;
    $("#context-switcher").value = snapshot.context;
  }

  function renderDevices() {
    Object.entries(snapshot.states.devices).forEach(([name, state]) => {
      const card = document.getElementById("device-card-" + name);
      if (card) card.querySelector(".device-state").textContent = state;
    });
    $("#current-preset").textContent = snapshot.states.current_preset || "未选择";
  }

  function renderShortcutConflicts() {
    const banner = $("#shortcut-conflict");
    if (!snapshot.shortcuts.length) {
      banner.hidden = true;
      return;
    }
    const lines = snapshot.shortcuts.map((c) => {
      const names = c.commands.map((cmd) => cmd.path.join(" > ")).join("　与　");
      return `快捷键冲突：${c.shortcut} 同时绑定于「${names}」`;
    });
    banner.textContent = "⚠ " + lines.join("；");
    banner.hidden = false;
  }

  function log(message, fail) {
    logSeq += 1;
    const li = document.createElement("li");
    const idx = document.createElement("span");
    idx.className = "log-idx";
    idx.textContent = String(logSeq).padStart(3, "0");
    const msg = document.createElement("span");
    msg.className = "log-msg" + (fail ? " fail" : "");
    msg.textContent = `[${snapshot.context}] ${message}`;
    li.append(idx, msg);
    eventLog.prepend(li);
  }

  // ---------- 菜单栏与多级弹出菜单 ----------
  function renderMenubar() {
    const bar = $("#menubar");
    bar.innerHTML = "";
    snapshot.menu.forEach((root) => {
      const el = document.createElement("div");
      el.className = "menu-root";
      el.dataset.label = root.label;
      el.textContent = root.label;
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        closeAllPopups();
        openPopup(root, el.getBoundingClientRect().bottom, el.getBoundingClientRect().left, 0);
        el.classList.add("active");
      });
      bar.appendChild(el);
    });
  }

  function closeAllPopups() {
    menuLayer.innerHTML = "";
    document.querySelectorAll(".menu-root.active").forEach((n) => n.classList.remove("active"));
  }

  function buildPopup(container, node, x, y, depth) {
    const popup = document.createElement("div");
    popup.className = "menu-popup";
    popup.dataset.depth = String(depth);
    popup.style.left = x + "px";
    popup.style.top = y + "px";
    container.appendChild(popup);

    node.children.forEach((child) => {
      if (child.kind === "command") {
        popup.appendChild(commandItem(child));
      } else {
        popup.appendChild(groupItem(child, depth));
      }
    });
    return popup;
  }

  function commandItem(node) {
    const item = document.createElement("div");
    item.className = "menu-item" + (node.enabled ? "" : " disabled");
    item.dataset.id = node.id;
    item.dataset.path = node.path.join(" > ");
    item.dataset.enabled = node.enabled ? "true" : "false";
    item.dataset.scope = node.scope || "";

    const label = document.createElement("span");
    label.className = "item-label";
    label.textContent = node.label;
    item.appendChild(label);

    if (!node.enabled && node.disabled_reason) {
      const reason = document.createElement("span");
      reason.className = "disabled-reason";
      reason.textContent = "（" + node.disabled_reason + "）";
      item.appendChild(reason);
    }
    if (node.shortcut) {
      const sc = document.createElement("span");
      sc.className = "item-shortcut";
      sc.textContent = node.shortcut;
      item.appendChild(sc);
    }
    item.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (!node.enabled) {
        log(`拒绝执行禁用命令 ${node.id}：${node.disabled_reason || "当前上下文不可用"}`, true);
        return;
      }
      executeCommand(node);
      closeAllPopups();
    });
    return item;
  }

  function groupItem(node, depth) {
    const item = document.createElement("div");
    item.className = "menu-item";
    item.dataset.path = node.path.join(" > ");
    const label = document.createElement("span");
    label.className = "item-label";
    label.textContent = node.label;
    item.appendChild(label);
    const arrow = document.createElement("span");
    arrow.className = "item-arrow";
    arrow.textContent = "▶";
    item.appendChild(arrow);

    item.addEventListener("mouseenter", () => {
      // 关闭同级弹出层。
      menuLayer
        .querySelectorAll(`.menu-popup[data-depth='${depth + 1}']`)
        .forEach((n) => n.remove());
      const rect = item.getBoundingClientRect();
      if (node.kind === "dynamic") {
        openDynamicPopup(node, rect.right, rect.top, depth + 1, item);
      } else {
        openPopup(node, rect.top, rect.right, depth + 1);
      }
    });
    return item;
  }

  function openPopup(node, y, x, depth) {
    return buildPopup(menuLayer, node, x, y, depth);
  }

  // 动态子菜单：先显示加载占位，300ms 后按当前上下文拉取内容。
  async function openDynamicPopup(node, x, y, depth, anchorItem) {
    const popup = document.createElement("div");
    popup.className = "menu-popup";
    popup.dataset.depth = String(depth);
    popup.style.left = x + "px";
    popup.style.top = y + "px";
    const loading = document.createElement("div");
    loading.className = "dynamic-loading";
    loading.textContent = "正在加载当前上下文的预设…";
    popup.appendChild(loading);
    menuLayer.appendChild(popup);

    await new Promise((resolve) => setTimeout(resolve, 300));
    if (!document.body.contains(anchorItem)) return; // 已被其他悬停关闭
    popup.innerHTML = "";
    snapshot.presets.forEach((preset) => {
      const cmdLike = {
        id: preset.id,
        label: preset.label,
        path: node.path.concat(preset.label),
        scope: "preset",
        enabled: true,
        shortcut: null,
        preset_key: preset.preset_key,
        kind: "command",
      };
      const item = commandItem(cmdLike);
      item.dataset.presetKey = preset.preset_key;
      popup.appendChild(item);
    });
    node.dynamic_loaded = true;
  }

  // ---------- 命令执行（仅修改本地虚构状态，再重新拉快照） ----------
  async function executeCommand(node) {
    const resp = await fetch("/lab/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command_id: node.id, preset_key: node.preset_key || null }),
    });
    const body = await resp.json();
    if (!resp.ok || !body.ok) {
      log(`命令 ${node.id} 被服务端拒绝：${body.error || "未知原因"}`, true);
    } else {
      log(body.event);
      pushRecent(node.id);
    }
    await refreshSnapshot();
  }

  function pushRecent(commandId) {
    const idx = recentUsage.indexOf(commandId);
    if (idx >= 0) recentUsage.splice(idx, 1);
    recentUsage.unshift(commandId);
    if (recentUsage.length > 5) recentUsage.length = 5;
  }

  // ---------- 命令面板：上下文相关的动态排序 ----------
  function allCommands() {
    const out = [];
    const walk = (nodes) =>
      nodes.forEach((n) => {
        if (n.kind === "command") out.push(n);
        if (n.children) walk(n.children);
      });
    walk(snapshot.menu);
    // 动态预设命令也参与搜索。
    snapshot.presets.forEach((p) =>
      out.push({
        id: p.id,
        label: p.label,
        path: ["实验", "实验预设", p.label],
        scope: "preset",
        enabled: true,
        shortcut: null,
      })
    );
    return out;
  }

  function scoreCommand(cmd, query) {
    const q = query.trim();
    const label = cmd.label;
    const pathText = cmd.path.join(" > ");
    let score = 0;
    if (!q) {
      score = 1;
    } else if (label.startsWith(q)) {
      score += 100;
    } else if (label.includes(q)) {
      score += 60;
    } else if (pathText.includes(q)) {
      score += 30;
    } else {
      return null; // 不匹配，不进入结果
    }
    // 上下文亲和度（切换上下文后排序重排的根因）。
    score += (snapshot.search_affinity && snapshot.search_affinity[cmd.id]) || 0;
    // 近期使用加权。
    const recentIdx = recentUsage.indexOf(cmd.id);
    if (recentIdx >= 0) score += 8 - recentIdx;
    // 禁用项降权但保留在结果中（用于演示禁用目标）。
    if (!cmd.enabled) score -= 25;
    return score;
  }

  function runSearch(query) {
    const rows = allCommands()
      .map((cmd) => ({ cmd, score: scoreCommand(cmd, query) }))
      .filter((x) => x.score !== null)
      .sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        return a.cmd.id.localeCompare(b.cmd.id); // 同分确定性次序
      });
    const list = $("#palette-results");
    list.innerHTML = "";
    if (!rows.length) {
      const empty = document.createElement("li");
      empty.className = "empty-hint";
      empty.textContent = "没有匹配的命令";
      list.appendChild(empty);
      return;
    }
    rows.forEach(({ cmd, score }) => {
      const li = document.createElement("li");
      li.className = "palette-row" + (cmd.enabled ? "" : " disabled");
      li.dataset.id = cmd.id;
      li.dataset.path = cmd.path.join(" > ");
      li.dataset.enabled = cmd.enabled ? "true" : "false";
      const label = document.createElement("span");
      label.className = "row-label";
      label.textContent = cmd.label + (cmd.enabled ? "" : "（禁用）");
      const path = document.createElement("span");
      path.className = "row-path";
      path.textContent = cmd.path.join(" > ");
      const scoreEl = document.createElement("span");
      scoreEl.className = "row-score";
      scoreEl.textContent = "score=" + score + " · " + cmd.scope;
      li.append(label, path, scoreEl);
      li.addEventListener("click", () => {
        if (!cmd.enabled) {
          log(`命令面板拒绝执行禁用命令 ${cmd.id}`, true);
          return;
        }
        closePalette();
        executeCommand(cmd);
      });
      list.appendChild(li);
    });
  }

  // ---------- 命令面板开关 ----------
  function openPalette() {
    $("#palette-overlay").hidden = false;
    const input = $("#palette-input");
    input.value = "";
    runSearch("");
    input.focus();
  }
  function closePalette() {
    $("#palette-overlay").hidden = true;
  }

  // ---------- 事件绑定 ----------
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".menu-popup") && !ev.target.closest(".menu-root")) {
      closeAllPopups();
    }
  });
  $("#context-switcher").addEventListener("change", async (ev) => {
    const resp = await fetch("/lab/api/context", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ context: ev.target.value }),
    });
    if (!resp.ok) {
      log("上下文切换失败", true);
    }
    await refreshSnapshot();
    log(`切换实验上下文 → ${snapshot.context_label}`);
  });
  $("#open-palette").addEventListener("click", openPalette);
  $("#palette-input").addEventListener("input", (ev) => runSearch(ev.target.value));
  document.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey && ev.shiftKey && ev.code === "KeyP") {
      ev.preventDefault();
      openPalette();
    } else if (ev.key === "Escape") {
      closePalette();
      closeAllPopups();
    }
  });
  $("#palette-overlay").addEventListener("click", (ev) => {
    if (ev.target === $("#palette-overlay")) closePalette();
  });

  // 全局越界请求记录位（采集器路由守卫写入）。
  window.__rpaBlocked = window.__rpaBlocked || [];

  refreshSnapshot().then(() => log("实验台初始化完成（虚构数据）"));
})();
