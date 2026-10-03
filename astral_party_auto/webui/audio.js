/* 音频保留独立的选择与请求状态，避免图片工作台的当前项影响音频提交。 */
(() => {
  "use strict";

  const PAGE_SIZE = 40;
  const CATEGORY_LABELS = { all: "全部音频", music: "背景音乐", voice: "角色语音", effects: "音效", other: "其他" };
  const EDIT_HINT = "当前支持兼容 WEM 替换，可先导出另一条游戏原音尝试互换；WAV / MP3 暂不能直接写回。";
  const $ = (id) => document.getElementById(id);
  const state = {
    active: false, initialized: false, query: "", category: "all", offset: 0,
    items: [], total: 0, draft: [], selected: null, candidate: null,
    selectionVersion: 0, catalogVersion: 0, catalogAbort: null, pollTimer: null,
    working: false, draftRevision: 0, players: new Map(),
  };
  let bridge;

  function message(id, text, error = false) {
    const element = $(id);
    element.textContent = text || "";
    element.classList.toggle("is-error", error);
  }

  async function request(name, args, signal) {
    const response = await fetch(`/api/${name}`, {
      method: "POST", headers: { ...bridge.headers, "Content-Type": "application/json" },
      body: JSON.stringify(args), signal,
    });
    const result = await response.json();
    if (!response.ok || result.ok === false) throw new Error(result.error || `请求失败（${response.status}）`);
    return result.data ?? result;
  }

  function savedItem(id = state.selected?.id) {
    return state.draft.find((item) => item.id === id);
  }

  function syncButtons() {
    const selected = state.selected;
    const editable = selected && selected.editable !== false;
    const locked = state.working;
    $("audio-choose").disabled = !editable || locked;
    $("audio-save").disabled = !editable || !state.candidate?.ticket || !state.players.get("replacement")?.ready || locked;
    $("audio-discard").classList.toggle("is-hidden", !state.candidate);
    $("audio-discard").disabled = locked;
    $("audio-export-original").disabled = !selected || locked;
    $("audio-export-wem").disabled = !selected || locked;
    $("audio-export-draft").disabled = !savedItem() || locked;
    $("audio-export-pack").disabled = !state.draft.length || locked;
    $("audio-install").disabled = !state.draft.length || locked;
    $("audio-clear").disabled = !state.draft.length || locked;
    $("audio-clear-accept").disabled = locked;
    document.querySelectorAll("#audio-draft-list button").forEach((button) => { button.disabled = locked; });
    let hint = EDIT_HINT;
    if (selected?.editable === false) hint = selected.reason || "这条音频暂不支持替换，可先试听或导出。";
    else if (selected) {
      const format = [selected.codec || "Wwise Vorbis", selected.sample_rate ? `${selected.sample_rate} Hz` : "", selected.channels ? `${selected.channels} 声道` : ""].filter(Boolean).join(" / ");
      hint = `${EDIT_HINT} 当前目标：${format}，替换文件需保持相同编码、采样率和声道。`;
      if (selected.music_track) hint += ` 这是音乐轨，还需与原曲采样帧数完全一致${Number(selected.duration) > 0 ? `（约 ${Number(selected.duration).toFixed(2)} 秒）` : ""}。`;
    }
    message("audio-edit-hint", hint);
  }

  function disposePlayer(side) {
    const current = state.players.get(side);
    if (current) {
      current.abort.abort();
      current.element.pause();
      current.element.removeAttribute("src");
      current.element.load();
      if (current.objectUrl) URL.revokeObjectURL(current.objectUrl);
      state.players.delete(side);
    }
  }

  function resetPlayer(side, text) {
    disposePlayer(side);
    message(`audio-${side}-name`, side === "original" ? "尚未选择" : "选择文件后，在这里试听");
    message(`audio-${side}-info`, "");
    message(`audio-${side}-status`, text);
    $(`audio-${side}-retry`).classList.add("is-hidden");
  }

  function metadataText(metadata, element) {
    const duration = Number(metadata.duration ?? element.duration);
    const details = [];
    if (Number.isFinite(duration) && duration > 0) details.push(`${duration.toFixed(2)} 秒`);
    if (metadata.sample_rate) details.push(`${(Number(metadata.sample_rate) / 1000).toLocaleString()} kHz`);
    if (metadata.channels) details.push(`${metadata.channels} 声道`);
    return details.join(" · ");
  }

  async function loadPlayer(side, source, candidate = null) {
    const selected = state.selected;
    if (!selected) return;
    disposePlayer(side);
    const selectionVersion = state.selectionVersion;
    const player = { element: $(`audio-${side}-player`), abort: new AbortController(), objectUrl: null, ready: false, metadata: null };
    state.players.set(side, player);
    const current = () => state.active && state.players.get(side) === player && selectionVersion === state.selectionVersion && selected.id === state.selected?.id;
    message(`audio-${side}-name`, candidate?.name || selected.name || selected.id);
    message(`audio-${side}-info`, "");
    message(`audio-${side}-status`, "正在解码并加载试听…");
    $(`audio-${side}-retry`).classList.add("is-hidden");
    syncButtons();
    try {
      const metadata = candidate || await request("audio_preview", [selected.id, source], player.abort.signal);
      if (!current()) return;
      if (!metadata?.url) throw new Error(metadata?.note || "没有生成可播放的音频。");
      if (side === "original") {
        for (const key of ["duration", "sample_rate", "channels", "codec", "music_track", "editable", "reason"]) {
          if (metadata[key] !== undefined) state.selected[key] = metadata[key];
        }
        syncButtons();
      }
      const url = new URL(metadata.url, window.location.href);
      if (url.origin !== window.location.origin) throw new Error("试听地址不是当前本地服务。");
      const response = await fetch(url, { headers: bridge.headers, signal: player.abort.signal });
      if (!response.ok) {
        let detail = `音频读取失败（${response.status}）`;
        try { detail = (await response.json()).error || detail; } catch (_) {}
        throw new Error(detail);
      }
      const blob = await response.blob();
      if (!current()) return;
      if (!blob.size) throw new Error("解码结果为空，请重新加载或选择其他资源。");
      player.objectUrl = URL.createObjectURL(blob);
      player.metadata = metadata;
      player.element.src = player.objectUrl;
      player.element.load();
      message(`audio-${side}-name`, metadata.name || selected.name || selected.id);
      message(`audio-${side}-info`, metadataText(metadata, player.element));
      message(`audio-${side}-status`, metadata.note || "正在读取播放信息…");
    } catch (error) {
      if (!current() || error.name === "AbortError") return;
      message(`audio-${side}-status`, `试听未加载：${error.message}`, true);
      $(`audio-${side}-retry`).classList.remove("is-hidden");
    } finally {
      if (current()) syncButtons();
    }
  }

  function updateReplacementLabel() {
    const isSaved = !!savedItem();
    $("audio-replacement-heading").textContent = state.candidate ? "待保存的替换" : isSaved ? "已保存的替换" : "你的替换";
    $("audio-replacement-badge").textContent = state.candidate ? "尚未保存" : isSaved ? "音频作品集" : "尚未选择";
  }

  function selectItem(item) {
    if (state.working) return;
    state.selectionVersion += 1;
    state.selected = { ...item };
    state.candidate = null;
    message("audio-title", item.name || item.id);
    const copies = Number(item.copy_count) > 1 ? `${item.copy_count} 份同源包同步替换` : "";
    message("audio-description", [item.description || `${CATEGORY_LABELS[item.category] || "音频"} · ${item.id}`, copies].filter(Boolean).join(" · "));
    updateReplacementLabel();
    resetPlayer("original", "正在加载原音…");
    resetPlayer("replacement", "先选择兼容的 WEM 文件");
    renderList();
    syncButtons();
    loadPlayer("original", "original");
    if (savedItem(item.id)) loadPlayer("replacement", "draft");
  }

  function renderList() {
    const list = $("audio-list");
    list.replaceChildren();
    if (!state.items.length) {
      const empty = document.createElement("div");
      empty.className = "notice";
      empty.textContent = "没有匹配的音频。可调整搜索条件，或刷新音频目录。";
      list.append(empty);
    }
    for (const item of state.items) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "audio-resource" + (item.id === state.selected?.id ? " is-active" : "");
      button.setAttribute("aria-pressed", String(item.id === state.selected?.id));
      const name = document.createElement("strong");
      name.textContent = item.name || item.id;
      const description = document.createElement("small");
      const copies = Number(item.copy_count) > 1 ? `${item.copy_count} 份同源包同步替换` : "";
      description.textContent = [CATEGORY_LABELS[item.category] || item.category, item.description || item.id, copies, savedItem(item.id) ? "已保存替换" : ""].filter(Boolean).join(" · ");
      button.title = description.textContent;
      button.append(name, description);
      button.addEventListener("click", () => selectItem(item));
      list.append(button);
    }
    message("audio-result-count", `共 ${state.total.toLocaleString()} 条`);
    message("audio-page-label", state.total ? `${Math.floor(state.offset / PAGE_SIZE) + 1} / ${Math.ceil(state.total / PAGE_SIZE)} 页` : "");
    $("audio-prev").disabled = state.offset === 0;
    $("audio-next").disabled = state.offset + PAGE_SIZE >= state.total;
  }

  function renderDraft() {
    const list = $("audio-draft-list");
    list.replaceChildren();
    message("audio-draft-count", `${state.draft.length} 项`);
    message("nav-audio-count", state.draft.length ? String(state.draft.length) : "");
    if (!state.draft.length) {
      const empty = document.createElement("p");
      empty.className = "notice";
      empty.textContent = "还没有保存的音频替换。";
      list.append(empty);
    }
    for (const item of state.draft) {
      const row = document.createElement("div");
      row.className = "audio-draft-item";
      const label = document.createElement("span");
      label.textContent = item.name || item.id;
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "button ghost compact";
      preview.textContent = "对照试听";
      preview.addEventListener("click", () => { selectItem(state.items.find((entry) => entry.id === item.id) || item); $("audio-title").scrollIntoView({ block: "nearest" }); });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "button danger compact";
      remove.textContent = "移除";
      remove.setAttribute("aria-label", `移除音频替换 ${item.name || item.id}`);
      remove.addEventListener("click", () => removeItem(item.id));
      row.append(label, preview, remove);
      list.append(row);
    }
    syncButtons();
  }

  function catalogStatus(status) {
    if (typeof status === "string") return status.replace(/段音频/g, "份媒体资源（含同源副本）");
    if (!status) return "";
    if (status.error) return `音频目录读取失败：${status.error}`;
    const progress = status.scanning && status.total ? `（${status.done || 0} / ${status.total}）` : "";
    const text = status.message || (status.scanning ? "正在扫描音频，已有结果可以先试听" : "音频目录已就绪");
    return `${text.replace(/段音频/g, "份媒体资源（含同源副本）")}${progress}`;
  }

  async function loadCatalog(refresh = false) {
    clearTimeout(state.pollTimer);
    state.catalogAbort?.abort();
    state.catalogAbort = new AbortController();
    const version = ++state.catalogVersion;
    const draftRevision = state.draftRevision;
    const current = () => state.active && version === state.catalogVersion;
    message("audio-status", refresh ? "正在刷新音频目录…" : "正在读取音频目录…");
    try {
      const data = await request("audio_catalog", [state.query, state.category, state.offset, PAGE_SIZE, refresh], state.catalogAbort.signal);
      if (!current()) return;
      state.items = data.items || [];
      state.total = Number(data.total) || 0;
      if (draftRevision === state.draftRevision) state.draft = data.draft || [];
      if (state.offset >= state.total && state.offset > 0) {
        state.offset = Math.max(0, Math.ceil(state.total / PAGE_SIZE) - 1) * PAGE_SIZE;
        return loadCatalog();
      }
      const select = $("audio-category");
      select.replaceChildren();
      const categories = data.categories?.length ? data.categories : Object.entries(CATEGORY_LABELS).map(([id, label]) => ({ id, label }));
      if (!categories.some((category) => category.id === "all")) categories.unshift({ id: "all", label: "全部音频" });
      for (const category of categories) {
        const option = document.createElement("option");
        option.value = category.id;
        option.textContent = category.label || CATEGORY_LABELS[category.id] || category.id;
        select.append(option);
      }
      select.value = state.category;
      message("audio-status", catalogStatus(data.status) || "音频目录已就绪，点击一条资源开始试听。", !!data.status?.error);
      renderList();
      renderDraft();
      if (data.status?.scanning) state.pollTimer = setTimeout(() => loadCatalog(), 1400);
    } catch (error) {
      if (!current() || error.name === "AbortError") return;
      message("audio-status", `音频目录未加载：${error.message}。可点击“刷新音频目录”重试。`, true);
    }
  }

  async function operation(label, run) {
    if (state.working) return;
    state.working = true;
    syncButtons();
    message("audio-operation-status", label);
    try {
      await run();
    } catch (error) {
      message("audio-operation-status", error.message || "操作失败，请重试。", true);
    } finally {
      state.working = false;
      syncButtons();
    }
  }

  async function chooseReplacement() {
    if (!state.selected || state.selected.editable === false) return;
    const id = state.selected.id;
    const version = state.selectionVersion;
    await operation("选择并检查替换文件…", async () => {
      const candidate = await bridge.call("audio_choose_replacement", { busy: true, busyText: "检查替换 WEM…" }, id);
      if (!candidate) { message("audio-operation-status", "已取消文件选择。"); return; }
      if (!state.active || version !== state.selectionVersion || id !== state.selected?.id) {
        if (id === state.selected?.id) state.candidate = null;
        return;
      }
      state.candidate = candidate;
      updateReplacementLabel();
      await loadPlayer("replacement", "draft", candidate);
      message("audio-operation-status", "替换文件尚未保存，请先试听，再保存到音频作品集。");
    });
  }

  async function saveCandidate() {
    if (!state.selected || !state.candidate?.ticket || !state.players.get("replacement")?.ready) return;
    const id = state.selected.id;
    const ticket = state.candidate.ticket;
    const version = state.selectionVersion;
    await operation("正在保存音频替换…", async () => {
      const result = await bridge.call("audio_commit", { busy: true, busyText: "保存音频作品集…" }, id, ticket);
      state.draftRevision += 1;
      state.draft = result.items || [];
      if (state.candidate?.ticket === ticket) state.candidate = null;
      renderDraft();
      renderList();
      if (state.active && version === state.selectionVersion && id === state.selected?.id) {
        state.candidate = null;
        updateReplacementLabel();
        await loadPlayer("replacement", "draft");
      }
      message("audio-operation-status", "已保存到音频作品集，可继续试听或安装测试。");
      bridge.toast("音频替换已保存，尚未安装到游戏");
    });
  }

  async function removeItem(id) {
    await operation("正在移除音频替换…", async () => {
      const result = await bridge.call("audio_remove", { busy: true, busyText: "移除音频替换…" }, id);
      state.draftRevision += 1;
      state.draft = result.items || [];
      renderDraft();
      renderList();
      if (id === state.selected?.id) {
        updateReplacementLabel();
        if (!state.candidate) resetPlayer("replacement", "这条替换已从音频作品集移除。");
      }
      message("audio-operation-status", "已从音频作品集移除；已安装的 Mod 可在“Mod 管理”里卸载。");
    });
  }

  async function exportPreview(source, format = "wav") {
    if (!state.selected) return;
    const id = state.selected.id;
    await operation("正在导出音频…", async () => {
      const result = await bridge.call("audio_export_preview", { busy: true, busyText: "导出音频…" }, id, source, format);
      message("audio-operation-status", result?.path ? `已导出：${result.path}` : "已取消导出。");
    });
  }

  function bindPlayer(side) {
    const element = $(`audio-${side}-player`);
    element.addEventListener("play", () => {
      for (const player of state.players.values()) if (player.element !== element) player.element.pause();
    });
    element.addEventListener("loadedmetadata", () => {
      const player = state.players.get(side);
      if (!player || element.src !== player.objectUrl) return;
      player.ready = true;
      message(`audio-${side}-info`, metadataText(player.metadata || {}, element));
      message(`audio-${side}-status`, player.metadata?.note || "已就绪，点击播放试听。");
      syncButtons();
    });
    element.addEventListener("error", () => {
      const player = state.players.get(side);
      if (!player?.objectUrl || element.src !== player.objectUrl) return;
      player.ready = false;
      message(`audio-${side}-status`, "播放器无法读取解码结果，请重新加载；若仍失败，可导出原始 WEM 检查。", true);
      $(`audio-${side}-retry`).classList.remove("is-hidden");
      syncButtons();
    });
  }

  function init(dependencies) {
    if (state.initialized) return;
    state.initialized = true;
    bridge = dependencies;
    bindPlayer("original");
    bindPlayer("replacement");
    $("audio-search-form").addEventListener("submit", (event) => { event.preventDefault(); state.query = $("audio-search").value.trim(); state.offset = 0; loadCatalog(); });
    $("audio-category").addEventListener("change", (event) => { state.category = event.target.value; state.offset = 0; loadCatalog(); });
    $("audio-refresh").addEventListener("click", () => { state.offset = 0; loadCatalog(true); });
    $("audio-prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE_SIZE); loadCatalog(); });
    $("audio-next").addEventListener("click", () => { state.offset += PAGE_SIZE; loadCatalog(); });
    $("audio-original-retry").addEventListener("click", () => loadPlayer("original", "original"));
    $("audio-replacement-retry").addEventListener("click", () => loadPlayer("replacement", "draft", state.candidate));
    $("audio-choose").addEventListener("click", chooseReplacement);
    $("audio-save").addEventListener("click", saveCandidate);
    $("audio-discard").addEventListener("click", () => {
      state.candidate = null;
      resetPlayer("replacement", "已取消本次文件选择。");
      updateReplacementLabel();
      syncButtons();
      if (savedItem()) loadPlayer("replacement", "draft");
    });
    $("audio-export-original").addEventListener("click", () => exportPreview("original"));
    $("audio-export-wem").addEventListener("click", () => exportPreview("original", "wem"));
    $("audio-export-draft").addEventListener("click", () => exportPreview("draft"));
    $("audio-clear").addEventListener("click", () => { $("audio-clear-confirm").classList.remove("is-hidden"); $("audio-clear-cancel").focus(); });
    $("audio-clear-cancel").addEventListener("click", () => $("audio-clear-confirm").classList.add("is-hidden"));
    $("audio-clear-accept").addEventListener("click", () => operation("正在清空音频作品集…", async () => {
      const result = await bridge.call("audio_clear_draft", { busy: true, busyText: "清空音频草稿…" });
      state.draftRevision += 1;
      state.draft = result.items || [];
      $("audio-clear-confirm").classList.add("is-hidden");
      renderDraft();
      renderList();
      updateReplacementLabel();
      if (!state.candidate) resetPlayer("replacement", "音频作品集已清空，可以重新选择替换文件。");
      message("audio-operation-status", "音频草稿已清空；已安装的 Mod 仍保留，可在“Mod 管理”里卸载。");
    }));
    $("audio-export-pack").addEventListener("click", () => operation("正在打包音频作品集…", async () => {
      const result = await bridge.call("audio_export_pack", { busy: true, busyText: "导出音频 ZIP…" });
      message("audio-operation-status", result?.path ? `已导出：${result.path}` : "已取消导出。");
    }));
    $("audio-install").addEventListener("click", () => operation("正在备份并安装音频作品集…", async () => {
      const result = await bridge.call("audio_install", { busy: true, busyText: "安装音频作品集…" });
      bridge.onInstall(result || {});
      message("audio-operation-status", "音频作品集已安装，可进游戏测试。在“Mod 管理”可禁用、卸载，或使用一键全还原。");
      bridge.toast("音频作品集已安装");
    }));
    document.addEventListener("visibilitychange", () => { if (document.hidden) for (const player of state.players.values()) player.element.pause(); });
    window.addEventListener("pagehide", leave);
  }

  function enter() {
    state.active = true;
    loadCatalog();
    if (state.selected) {
      loadPlayer("original", "original");
      if (state.candidate) loadPlayer("replacement", "draft", state.candidate);
      else if (savedItem()) loadPlayer("replacement", "draft");
    }
  }

  function leave() {
    state.active = false;
    state.selectionVersion += 1;
    state.catalogVersion += 1;
    state.catalogAbort?.abort();
    clearTimeout(state.pollTimer);
    $("audio-clear-confirm").classList.add("is-hidden");
    disposePlayer("original");
    disposePlayer("replacement");
  }

  window.AudioWorkbench = { init, enter, leave };
})();
