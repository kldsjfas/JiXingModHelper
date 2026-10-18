/* 吉星派对 Mod 助手 — HTML 前端，通过本地 HTTP API 调用 Python 后端 */
(() => {
  "use strict";

  const PAGE_SIZE = 25;
  const REPLACEMENT_FILE_LIMIT = 12 * 1024 * 1024;
  const API_TOKEN = new URLSearchParams(window.location.hash.slice(1)).get("token") || "";
  const API_HEADERS = { "X-JiXing-Token": API_TOKEN };
  const PAGE_TITLES = {
    dashboard: "仪表盘",
    manage: "Mod 管理",
    workshop: "Mod 工坊",
    browse: "浏览资源",
    audio: "音频工作台",
    studio: "制作替换",
    pack: "我的作品集",
    logs: "运行日志",
  };
  const TYPE_HINTS = {
    texture: "图片资源。走路/攻击一帧帧图在细分类「角色动作帧」。",
    text: "文案与配置。支持原文对照、查找替换；二进制内容只读。",
    mesh: "3D 三角面模型，只导出不替换。",
    anim: "Unity 动画片段。Sprite 动画按真实帧顺序播放；其他片段会说明预览限制。",
    dynamic: "序列帧、GIF / WebP / APNG 可播放和替换；其他动态类型按资源能力显示。",
  };

  const state = {
    page: "dashboard",
    dashboard: null,
    installed: [],
    draft: { name: "我的Mod套装", items: [] },
    assetTypes: [],
    assetType: "texture",
    categories: [],
    categoryId: "hand_card",
    resources: [],
    resourcePage: 0,
    totalResources: 0,
    multiSelectMode: false,
    selectedResources: [],
    browseRequestSeq: 0,
    browseRefreshSeq: 0,
    query: "",
    characterFilter: "",
    characterList: [],
    characterLabels: { bundles: {}, resources: {} },
    selection: null,
    selectionRequest: 0,
    comparisonRequest: 0,
    replacementRequest: 0,
    replacementImporting: false,
    studioRequest: 0,
    draftDetailRequest: 0,
    editorKey: "",
    textEdits: new Map(),
    textFields: null,
    textFieldPage: 0,
    originalAnimation: null,
    previewAnimation: null,
    previewLoading: false,
    previewError: "",
    previewRequest: 0,
    animationPlayers: new Map(),
    draftIndex: -1,
    pendingMod: null,
    migrationSource: null,
    replacement: null,
    emoteVideoName: "",
    emoteDraftVideoName: "",
    emoteSelectionKey: "",
    emoteReplacement: null,
    emoteRequest: 0,
    emotePreview: null,
    emoteDraftPreview: null,
    emotePreviewLoading: false,
    emoteDraftLoading: false,
    emotePreviewError: "",
    emoteDraftError: "",
    emoteImportError: "",
    emotePreviewRequest: 0,
    emoteOriginalRequest: 0,
    emoteDraftRequest: 0,
    busy: false,
    modBundles: [],
    modBundleActive: "",
    modPreviewTitle: "",
    sequenceFrames: [],
    sequenceTimer: null,
    lightboxFrames: [],
    lightboxSequenceTimer: null,
    lightbox: {
      scale: 1,
      tx: 0,
      ty: 0,
      dragging: false,
      startX: 0,
      startY: 0,
      origTx: 0,
      origTy: 0,
    },
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // 裁剪框保持目标比例；只平移、缩放图片，最后换算回原图像素坐标。
  let cropState = null;
  let cropPending = null;
  let cropDrag = null;
  let cropRequest = 0;

  function toast(message, isError = false) {
    const region = $("#toast-region");
    if (!region || !message) return;
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " is-error" : "");
    el.textContent = message;
    region.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  function setBusy(on, text = "处理中…") {
    state.busy = !!on;
    const overlay = $("#busy-overlay");
    if (!overlay) return;
    overlay.classList.toggle("is-hidden", !on);
    const label = $("#busy-text");
    if (label) label.textContent = text;
  }

  function setHeaderStatus(text, isError = false) {
    const pill = $("#header-status");
    if (!pill) return;
    pill.classList.toggle("is-error", !!isError);
    const span = pill.querySelector("span");
    if (span) span.textContent = text;
  }

  async function api(name, ...args) {
    const resp = await fetch("/api/" + name, {
      method: "POST",
      headers: { ...API_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(args),
    });
    const result = await resp.json();
    if (result && typeof result === "object" && "ok" in result) {
      if (!result.ok) throw new Error(result.error || "操作失败");
      return result.data;
    }
    return result;
  }

  async function call(name, options = {}, ...args) {
    const { busy = false, busyText = "处理中…", quiet = false } = options;
    if (busy) setBusy(true, busyText);
    try {
      return await api(name, ...args);
    } catch (err) {
      if (!quiet) toast(err.message || String(err), true);
      throw err;
    } finally {
      if (busy) setBusy(false);
    }
  }

  function showPage(page) {
    if (!PAGE_TITLES[page]) return;
    closeCropModal();
    if (state.page === "audio") window.AudioWorkbench?.leave();
    if (state.page === "workshop") window.ModWorkshop?.leave();
    if (state.page === "studio") window.VisualAnimation?.leave();
    rememberTextEdit();
    state.studioRequest += 1;
    state.draftDetailRequest += 1;
    stopAllAnimations();
    closeLightbox();
    state.page = page;
    $("#main-content").scrollTop = 0;
    $$(".page").forEach((el) => el.classList.toggle("is-active", el.id === `page-${page}`));
    $$(".nav-button").forEach((btn) =>
      btn.classList.toggle("is-active", btn.dataset.page === page)
    );
    const title = $("#page-title");
    if (title) title.textContent = PAGE_TITLES[page];
    document.body.classList.remove("nav-open");
    if (page === "browse") { refreshBrowse(); renderPreview(); refreshSelectionComparison(); }
    if (page === "audio") window.AudioWorkbench?.enter();
    if (page === "workshop") window.ModWorkshop?.enter();
    if (page === "studio") refreshStudio();
    if (page === "pack") refreshPack();
    if (page === "manage") renderInstalled();
    if (page === "logs") refreshLogs();
    if (page === "dashboard") renderDashboard();
  }

  function renderDashboard() {
    const d = state.dashboard || {};
    const game = $("#stat-game");
    const bundles = $("#stat-bundles");
    const installed = $("#stat-installed");
    const backups = $("#stat-backups");
    const path = $("#game-path");
    if (game) game.textContent = d.has_game ? d.game_name || "已找到" : "未检测到";
    if (bundles) {
      const tex = d.texture_bundle_count != null ? d.texture_bundle_count : "—";
      bundles.textContent = d.bundle_count != null ? `${d.bundle_count}（含贴图 ${tex}）` : "—";
    }
    if (installed) installed.textContent = String(d.installed_count ?? 0);
    if (backups) backups.textContent = String(d.backup_count ?? 0);
    if (path) {
      path.textContent = d.has_game
        ? `自动找到的游戏：${d.game_exe || ""}`
        : "未找到游戏，请确认已安装 Steam 或 TapTap 版吉星派对。";
    }
    setHeaderStatus(d.has_game ? "游戏已连接" : "未检测到游戏", !d.has_game);
  }

  function renderInstalled() {
    const list = $("#installed-list");
    const badge = $("#installed-count");
    if (badge) badge.textContent = `${state.installed.length} 个`;
    if (!list) return;
    list.innerHTML = "";
    if (!state.installed.length) {
      list.innerHTML = `<div class="notice">还没有安装任何 Mod。</div>`;
      return;
    }
    state.installed.forEach((mod) => {
      const row = document.createElement("div");
      row.className = "list-row";
      const disabled = !!mod.disabled;
      const fileCount = mod.files_count ?? (Array.isArray(mod.files) ? mod.files.length : mod.count ?? "");
      row.innerHTML = `
        <div class="list-row-main">
          <strong>${escapeHtml(mod.name || "")}</strong>
          <span>${disabled ? "已禁用" : "已启用"} · ${escapeHtml(String(fileCount))} 个包</span>
        </div>
        <div class="list-row-actions"></div>
      `;
      const actions = row.querySelector(".list-row-actions");
      const mk = (label, cls, fn) => {
        const b = document.createElement("button");
        b.className = `button ${cls} compact`;
        b.textContent = label;
        b.addEventListener("click", fn);
        actions.appendChild(b);
      };
      mk("预览", "ghost", () => previewInstalled(mod.name));
      if (disabled) mk("启用", "primary", () => changeMod("enable", mod.name));
      else mk("禁用", "purple", () => changeMod("disable", mod.name));
      mk("卸载", "ghost", () => changeMod("uninstall", mod.name));
      list.appendChild(row);
    });
  }

  function setModPreviewPanel(title, bundles, firstPreview) {
    state.modPreviewTitle = title || "";
    state.modBundles = bundles || [];
    state.modBundleActive = (firstPreview && firstPreview.bundle) || state.modBundles[0] || "";
    const titleEl = $("#mod-preview-title");
    if (titleEl) {
      titleEl.textContent = state.modPreviewTitle
        ? `${state.modPreviewTitle}（${state.modBundles.length} 个包，点左侧看图）`
        : "选待装包或点已装「预览」后，左侧点资源包看图。";
    }
    renderModBundleList();
    if (firstPreview) {
      showModBundlePreview(firstPreview);
    } else if (state.modBundleActive) {
      previewModBundle(state.modBundleActive);
    } else {
      showModBundlePreview(null);
    }
  }

  function renderModBundleList() {
    const list = $("#mod-bundle-list");
    if (!list) return;
    list.innerHTML = "";
    if (!state.modBundles.length) {
      list.innerHTML = `<div class="notice">暂无资源包列表</div>`;
      return;
    }
    state.modBundles.forEach((name) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mod-bundle-item" + (name === state.modBundleActive ? " is-active" : "");
      btn.textContent = name;
      btn.title = name;
      btn.addEventListener("click", () => previewModBundle(name));
      list.appendChild(btn);
    });
  }

  function showModBundlePreview(data) {
    const cap = $("#mod-preview-caption");
    const media = $("#mod-preview-media");
    if (!data || !data.preview_data) {
      if (cap) cap.textContent = (data && data.message) || "选左侧一项看图";
      if (media) media.innerHTML = "<span>—</span>";
      return;
    }
    if (cap) {
      const bits = [data.texture || data.bundle || "", data.size || ""].filter(Boolean);
      cap.textContent = bits.join(" · ");
    }
    if (media) media.innerHTML = `<img src="${data.preview_data}" alt="mod preview">`;
  }

  async function previewModBundle(bundleName) {
    state.modBundleActive = bundleName;
    renderModBundleList();
    try {
      const data = await call("preview_mod_bundle", { busy: true, busyText: "加载预览…" }, bundleName);
      showModBundlePreview(data);
    } catch (_) {
      showModBundlePreview({ message: "预览失败" });
    }
  }

  async function previewInstalled(name) {
    try {
      const data = await call("load_mod_preview", { busy: true, busyText: "加载 Mod 预览…" }, name);
      setModPreviewPanel(data.title || `已装 · ${name}`, data.bundles || [], data.first || null);
      toast(`已加载预览：${name}`);
      // 滚到预览区
      $("#mod-bundle-list")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (_) {}
  }

  async function changeMod(action, name) {
    const labels = { enable: "启用", disable: "禁用", uninstall: "卸载" };
    if (action === "uninstall" && !confirm(`确定卸载「${name}」并还原对应资源？`)) return;
    try {
      const data = await call("change_mod", { busy: true, busyText: `${labels[action] || "处理"}中…` }, action, name);
      state.installed = data.installed || [];
      state.dashboard = data.dashboard || state.dashboard;
      renderInstalled();
      renderDashboard();
      toast(`已${labels[action] || "处理"}：${name}`);
    } catch (_) {}
  }

  function fillAssetTypes() {
    const select = $("#asset-type");
    if (!select) return;
    select.innerHTML = "";
    (state.assetTypes.length ? state.assetTypes : [
      { id: "texture", label: "贴图" },
      { id: "text", label: "文本" },
      { id: "mesh", label: "3D模型" },
      { id: "anim", label: "动画" },
      { id: "dynamic", label: "动态图像" },
    ]).forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = t.label;
      select.appendChild(opt);
    });
    select.value = state.assetType;
    const hint = $("#asset-type-hint");
    if (hint) hint.textContent = TYPE_HINTS[state.assetType] || "";
  }

  function fillCharacterFilter() {
    const sel = $("#character-filter");
    if (!sel) return;
    const current = state.characterFilter || "";
    const options = ['<option value="">全部</option>', '<option value="__unmarked">未标注</option>'];
    (state.characterList || []).forEach((c) => {
      options.push(`<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`);
    });
    sel.innerHTML = options.join("");
    sel.value = current;
  }

  function fillResourceCharacterSelect() {
    const sel = $("#resource-character");
    if (!sel) return;
    const current = sel.value || "";
    const options = ['<option value="">未标注</option>'];
    (state.characterList || []).forEach((c) => {
      options.push(`<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`);
    });
    sel.innerHTML = options.join("");
    if (current && (state.characterList || []).includes(current)) sel.value = current;
  }

  function effectiveCharacter(bundle, name) {
    const res = state.characterLabels?.resources?.[bundle]?.[name];
    if (res) return res;
    return state.characterLabels?.bundles?.[bundle] || "";
  }

  function resourceKey(bundle, name) {
    return `${bundle}::${name}`;
  }

  function isResourceSelected(bundle, name) {
    const key = resourceKey(bundle, name);
    return state.selectedResources.some((item) => resourceKey(item.bundle, item.name) === key);
  }

  function updateMultiSelectUI() {
    const info = $("#multi-select-info");
    if (info) {
      info.textContent = state.multiSelectMode ? `已选 ${state.selectedResources.length} 项` : "";
      info.classList.toggle("is-hidden", !state.multiSelectMode);
    }
    const applyBtn = $("#apply-resource-character");
    if (applyBtn) {
      applyBtn.textContent = state.multiSelectMode ? "批量标注选中资源" : "标注当前资源";
      applyBtn.disabled = state.multiSelectMode ? state.selectedResources.length === 0 : !state.selection;
    }
    const bundleBtn = $("#apply-bundle-character");
    if (bundleBtn) bundleBtn.disabled = state.multiSelectMode || !state.selection;
  }

  function setMultiSelectMode(on) {
    state.multiSelectMode = !!on;
    if (!state.multiSelectMode) state.selectedResources = [];
    const btn = $("#multi-select-toggle");
    if (btn) {
      btn.classList.toggle("is-active", state.multiSelectMode);
      btn.textContent = state.multiSelectMode ? "☑ 退出多选" : "☑ 多选";
    }
    updateMultiSelectUI();
    renderResources();
  }

  function toggleSelectedResource(bundle, name) {
    const key = resourceKey(bundle, name);
    const index = state.selectedResources.findIndex(
      (item) => resourceKey(item.bundle, item.name) === key
    );
    if (index >= 0) {
      state.selectedResources.splice(index, 1);
    } else {
      state.selectedResources.push({ bundle, name });
    }
    updateMultiSelectUI();
    renderResources();
  }

  function exitMultiSelect() {
    if (state.multiSelectMode) setMultiSelectMode(false);
  }

  async function refreshBrowse() {
    const request = ++state.browseRefreshSeq;
    const assetType = state.assetType;
    // 分类刷新期间，旧列表响应也不能继续写回当前类型。
    state.browseRequestSeq += 1;
    fillAssetTypes();
    try {
      const [cats, charList, labels] = await Promise.all([
        call("get_categories", { quiet: true }, assetType),
        call("get_character_list", { quiet: true }),
        call("get_character_labels", { quiet: true }),
      ]);
      if (request !== state.browseRefreshSeq || assetType !== state.assetType || state.page !== "browse") return;
      state.categories = cats || [];
      state.characterList = charList || [];
      state.characterLabels = labels || { bundles: {}, resources: {} };
      fillCharacterFilter();
      fillResourceCharacterSelect();
      const validateBtn = $("#validate-dynamic");
      if (validateBtn) validateBtn.classList.toggle("is-hidden", state.assetType !== "dynamic");
      if (!state.categories.some((c) => c.id === state.categoryId)) {
        state.categoryId = state.categories[0]?.id || "all";
        state.resourcePage = 0;
      }
      renderCategories();
      await loadResources();
    } catch (err) {
      if (request !== state.browseRefreshSeq || assetType !== state.assetType || state.page !== "browse") return;
      $("#resource-status").textContent = err.message || "加载失败";
    }
  }

  function renderCategories() {
    const host = $("#category-list");
    if (!host) return;
    host.innerHTML = "";
    if (!state.categories.length) {
      host.innerHTML = `<div class="notice">无分类（可先刷新索引）</div>`;
      return;
    }
    state.categories.forEach((cat) => {
      if (state.assetType === "texture" && cat.count === 0 && !["all", "hand_card"].includes(cat.id)) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "category-button" + (cat.id === state.categoryId ? " is-active" : "");
      btn.innerHTML = `<span>${escapeHtml(cat.label)}</span><small>${cat.count} ${state.assetType === "texture" ? "张" : "条"}</small>`;
      btn.addEventListener("click", () => {
        state.categoryId = cat.id;
        state.resourcePage = 0;
        exitMultiSelect();
        renderCategories();
        loadResources();
      });
      host.appendChild(btn);
    });
  }

  async function loadResources() {
    const seq = ++state.browseRequestSeq;
    $("#resource-status").textContent = "加载中…";
    try {
      const data = await call(
        "browse_assets",
        { quiet: true },
        state.assetType,
        state.categoryId,
        state.query || "",
        state.characterFilter || "",
        state.resourcePage,
        PAGE_SIZE
      );
      if (seq !== state.browseRequestSeq || state.page !== "browse") return;
      state.resources = (data && data.items) || [];
      state.totalResources = (data && data.total) || 0;
      const pages = Math.max(1, Math.ceil(state.totalResources / PAGE_SIZE));
      if (state.resourcePage > pages - 1) {
        state.resourcePage = Math.max(0, pages - 1);
        return loadResources();
      }
      if (!state.resources.length && state.resourcePage === 0) {
        state.selection = null;
        renderPreview();
        updateExportButtons();
      }
      renderResources();
    } catch (err) {
      if (seq !== state.browseRequestSeq || state.page !== "browse") return;
      state.resources = [];
      state.totalResources = 0;
      renderResources();
      $("#resource-status").textContent = err.message || "加载失败";
    }
  }

  function renderResources() {
    const list = $("#resource-list");
    const status = $("#resource-status");
    const pageLabel = $("#resource-page-label");
    if (!list) return;
    list.innerHTML = "";
    const total = state.totalResources || state.resources.length || 0;
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    state.resourcePage = Math.min(state.resourcePage, pages - 1);
    const items = state.resources || [];
    if (status) status.textContent = total ? `共 ${total} 条（按 Bundle 排序，同名分开显示）` : "没有匹配";
    if (pageLabel) pageLabel.textContent = total ? `第 ${state.resourcePage + 1}/${pages} 页` : "";
    if (!items.length) {
      list.innerHTML = `<div class="notice">没有匹配的资源。</div>`;
      return;
    }
    const selKey = state.selection ? `${state.selection.bundle}::${state.selection.name}` : "";
    items.forEach((row) => {
      const key = `${row.bundle}::${row.name}`;
      const item = document.createElement("button");
      item.type = "button";
      item.className = "resource-item";
      const isSelected = state.multiSelectMode && isResourceSelected(row.bundle, row.name);
      if (!state.multiSelectMode && key === selKey) item.classList.add("is-active");
      if (isSelected) item.classList.add("is-selected");
      const char = row.character || effectiveCharacter(row.bundle, row.name);
      const badge = char ? `<span class="character-badge">${escapeHtml(char)}</span>` : "";
      const bundleLabel = shortBundleName(row.bundle);
      const detail = row.duplicates > 1 ? `同名 ${row.duplicates} 个 · ${bundleLabel}` : bundleLabel;
      const multiCheck = state.multiSelectMode ? `<span class="multi-check">${isSelected ? "✓" : ""}</span>` : "";
      item.innerHTML = `
        <span class="resource-main">
          <span class="resource-bundle">${escapeHtml(row.bundle)}</span>
          <span class="resource-name">${escapeHtml(row.name)}</span>
        </span>
        ${badge}
        <small>${escapeHtml(detail)}</small>
        ${multiCheck}
      `;
      item.title = `${row.name}
${row.bundle}`;
      item.addEventListener("click", () => {
        if (state.multiSelectMode) {
          toggleSelectedResource(row.bundle, row.name);
        } else {
          selectResource(row.bundle, row.name);
        }
      });
      list.appendChild(item);
    });
  }

  async function selectResource(bundle, name) {
    closeCropModal();
    rememberTextEdit();
    const request = ++state.selectionRequest;
    state.studioRequest += 1;
    state.comparisonRequest += 1;
    state.replacementRequest += 1;
    state.replacementImporting = false;
    stopAllAnimations();
    closeLightbox();
    state.replacement = null;
    state.selection = null;
    renderPreview();
    window.VisualAnimation?.leave();
    resetEmoteReplacement();
    resetEmotePreview();
    state.emoteVideoName = "";
    state.emoteDraftVideoName = "";
    state.emoteSelectionKey = "";
    state.previewAnimation = null;
    state.previewLoading = false;
    state.previewError = "";
    state.originalAnimation = null;
    state.sequenceFrames = [];
    try {
      const sel = await call("select_asset", { busy: true, busyText: "加载预览…" }, state.assetType, bundle, name);
      if (request !== state.selectionRequest) return;
      state.selection = sel;
      state.previewLoading = !!isPlayableAnimation(sel) || !!sel.emote?.video_name;
      renderPreview();
      renderResources();
      updateExportButtons();
      fillResourceCharacterSelect();
      const eff = effectiveCharacter(bundle, name);
      const charSel = $("#resource-character");
      if (charSel) charSel.value = eff || "";
      $("#apply-resource-character").disabled = false;
      $("#apply-bundle-character").disabled = false;
      $("#refresh-resource").disabled = false;
      if (isPlayableAnimation(sel) || sel.emote?.video_name) await loadBrowseAnimation(sel, request);
    } catch (_) {}
  }

  async function loadBrowseAnimation(selection, selectionRequest = state.selectionRequest) {
    const request = ++state.previewRequest;
    const key = selectionKey(selection);
    state.previewLoading = true;
    state.previewError = "";
    if (state.page === "browse") renderPreview();
    try {
      const animation = selection.emote?.video_name
        ? await call("get_emote_preview", { quiet: true }, selection.emote.video_name, "original")
        : await call("get_animation_preview", { quiet: true }, selection.bundle, selection.name, "original", previewFps());
      if (request !== state.previewRequest || selectionRequest !== state.selectionRequest || key !== selectionKey()) return;
      if (!animation?.frames?.length) throw new Error(animation?.notice || "没有可播放的帧");
      state.previewAnimation = animation;
      state.sequenceFrames = animation.frames;
      state.sequenceFps = animation.fps || 30;
    } catch (err) {
      if (request !== state.previewRequest || selectionRequest !== state.selectionRequest || key !== selectionKey()) return;
      state.previewAnimation = null;
      state.previewError = err.message;
    } finally {
      if (request === state.previewRequest && selectionRequest === state.selectionRequest && key === selectionKey()) {
        state.previewLoading = false;
        if (state.page === "browse") renderPreview();
      }
    }
  }

  function renderPreview() {
    const sel = state.selection;
    const title = $("#preview-title");
    const desc = $("#preview-description");
    const media = $("#preview-media");
    const go = $("#go-studio");
    disposeAnimation("preview-media");
    renderBrowseComparison();
    if (!sel) {
      if (title) title.textContent = "还没选资源";
      if (desc) desc.textContent = "点中间列表的一项";
      if (media) media.innerHTML = "<span>等待选择</span>";
      if (go) go.disabled = true;
      stopSequencePreview();
      state.sequenceFrames = [];
      $("#apply-resource-character").disabled = true;
      $("#apply-bundle-character").disabled = true;
      $("#refresh-resource").disabled = true;
      updateExportButtons();
      updateMultiSelectUI();
      return;
    }
    const emoteLabel = sel.emote?.choices?.find((choice) => choice.name === sel.emote.video_name)?.label;
    if (title) title.textContent = sel.emote ? emoteLabel || "静态表情" : sel.caption || sel.name || "资源";
    if (desc) desc.textContent = sel.reason || sel.category_desc || "";
    if (media) {
      if (isPlayableAnimation(sel) || sel.emote?.video_name) {
        if (state.previewLoading) renderAnimationState(media, "正在加载动画…");
        else if (state.previewError) renderAnimationState(media, `动画预览未加载：${state.previewError}`, () => loadBrowseAnimation(sel));
        else if (state.previewAnimation?.frames?.length) renderAnimation(media, state.previewAnimation, isSequenceAnimation(sel));
        else renderAnimationState(media, "动画尚未加载", () => loadBrowseAnimation(sel));
      } else if ((sel.asset_type || sel.kind) === "anim") {
        renderAnimationState(media, sel.reason || "此动画片段暂不支持动作播放；关联图集仅作静态参考。", null, sel.preview_data);
      } else if (sel.preview_data) {
        stopSequencePreview();
        media.innerHTML = `<img src="${sel.preview_data}" alt="preview">`;
      } else if (sel.text_preview) {
        stopSequencePreview();
        media.innerHTML = `<pre>${escapeHtml(sel.text_preview)}</pre>`;
      } else {
        stopSequencePreview();
        media.innerHTML = "<span>无可视预览</span>";
      }
    }
    if (go) go.disabled = (sel.asset_type || sel.kind) === "mesh";
    if (go) go.textContent = canEdit(sel) ? "去制作替换" : "查看资源详情";
    updateExportButtons();
    updateMultiSelectUI();
  }

  function updateExportButtons() {
    const sel = state.selection;
    const a = $("#export-primary");
    const b = $("#export-secondary");
    if (!a || !b) return;
    if (!sel) {
      a.disabled = b.disabled = true;
      a.textContent = "导出";
      b.textContent = "副格式";
      return;
    }
    a.disabled = false;
    b.disabled = false;
    const kind = sel.asset_type || sel.kind;
    if (kind === "texture") {
      a.textContent = "导出 PNG";
      b.textContent = "导出 JPG";
    } else if (kind === "text") {
      a.textContent = "导出文本";
      b.textContent = "导出";
    } else if (kind === "mesh") {
      a.textContent = "导出 OBJ";
      b.textContent = "导出";
    } else if (kind === "anim") {
      a.textContent = "导出 JSON";
      b.textContent = "导出二进制";
    } else if (kind === "dynamic") {
      if (sel.animation_kind === "cri_usm") {
        a.textContent = "导出 USM";
        a.disabled = sel.exportable === false;
        b.textContent = "仅原始 USM";
        b.disabled = true;
      } else if (sel.frame_names && sel.frame_names.length) {
        a.textContent = "导出 APNG";
        b.textContent = "导出首帧 PNG";
      } else {
        a.textContent = "导出";
        b.textContent = "导出";
      }
    } else {
      a.textContent = "导出";
      b.textContent = "导出";
    }
  }

  function selectionKey(selection = state.selection) {
    return selection ? `${selection.asset_type || selection.kind}::${selection.bundle}::${selection.name}` : "";
  }

  function renderBrowseComparison() {
    const selection = state.selection;
    const visible = (selection?.asset_type || selection?.kind) === "texture";
    const modified = selection?.comparison?.modified;
    $("#preview-comparison")?.classList.toggle("has-comparison", visible);
    $("#preview-original-heading")?.classList.toggle("is-hidden", !visible);
    $("#preview-modified-card")?.classList.toggle("is-hidden", !visible);
    const notice = $("#preview-comparison-notice");
    if (notice) {
      notice.classList.toggle("is-hidden", !visible);
      notice.textContent = visible ? selection.comparison?.notice || (modified ? "当前作品集中的修改，可进入制作继续替换。" : "当前作品集尚未修改这张贴图。") : "";
    }
    setMedia($("#preview-modified"), visible ? modified?.preview_data : null, visible ? selection.comparison?.notice || "当前作品集无修改" : "等待选择");
  }

  async function refreshSelectionComparison() {
    if ((state.selection?.asset_type || state.selection?.kind) !== "texture") return;
    const key = selectionKey();
    const selectionRequest = state.selectionRequest;
    const request = ++state.comparisonRequest;
    try {
      // 只读取作品集状态，不刷新游戏资源或清除其他制作入口的候选。
      const selection = await call("get_studio_state", { quiet: true });
      if (request !== state.comparisonRequest || selectionRequest !== state.selectionRequest ||
          key !== selectionKey() || key !== selectionKey(selection)) return;
      state.selection = { ...state.selection, comparison: selection.comparison };
      renderBrowseComparison();
      if (state.page === "studio") renderReplacement();
    } catch (err) {
      if (request !== state.comparisonRequest || selectionRequest !== state.selectionRequest || key !== selectionKey()) return;
      state.selection = { ...state.selection, comparison: { modified: null, notice: `作品集对比未加载：${err.message}` } };
      renderBrowseComparison();
      if (state.page === "studio") renderReplacement();
    }
  }

  function resetEmoteReplacement() {
    state.emoteReplacement = null;
    state.emoteImportError = "";
    state.emoteRequest += 1;
    disposeAnimation("emote-replacement");
  }

  function resetEmotePreview() {
    state.emotePreviewRequest += 1;
    state.emoteOriginalRequest += 1;
    state.emoteDraftRequest += 1;
    state.emotePreview = null;
    state.emoteDraftPreview = null;
    state.emotePreviewLoading = false;
    state.emoteDraftLoading = false;
    state.emotePreviewError = "";
    state.emoteDraftError = "";
    disposeAnimation("emote-original");
    disposeAnimation("emote-replacement");
  }

  function selectedEmoteVideo() {
    return state.selection?.emote?.choices?.find((choice) => choice.name === state.emoteVideoName) || null;
  }

  function emotePreviewName(variant) {
    const emote = state.selection?.emote;
    if (!emote) return "";
    if (variant === "original") return emote.video_name || "";
    return selectedEmoteVideo()?.name || state.emoteDraftVideoName || emote.video_name || "";
  }

  function renderEmoteSettings() {
    const panel = $("#emote-settings");
    if (!panel) return;
    const selection = state.selection;
    const emote = selection?.emote;
    panel.classList.toggle("is-hidden", !emote);
    const key = selectionKey();
    if (state.emoteSelectionKey !== key) {
      resetEmoteReplacement();
      resetEmotePreview();
      // Existing videos are an explicit alternative; the original preview
      // comes from emote.video_name independently of this optional picker.
      state.emoteVideoName = "";
      state.emoteDraftVideoName = emote?.draft_video_name || "";
      state.emoteSelectionKey = key;
    }
    if (!emote) return;
    const picker = $("#emote-video");
    picker.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "可选：使用游戏已有动图";
    picker.appendChild(placeholder);
    for (const [index, choice] of (emote.choices || []).entries()) {
      const option = document.createElement("option");
      option.value = choice.name;
      option.textContent = choice.label || `游戏动态表情 ${index + 1}`;
      picker.appendChild(option);
    }
    let choice = selectedEmoteVideo();
    if (!choice && state.emoteVideoName) {
      state.emoteVideoName = "";
    }
    picker.value = state.emoteVideoName;
    picker.disabled = emote.editable !== true;
    choice = selectedEmoteVideo();
    const stored = state.emoteReplacement;
    if (stored && (emote.editable !== true || stored.sourceKey !== key || !stored.templateName)) resetEmoteReplacement();
    const editable = emote.editable === true;
    $("#emote-import").disabled = !editable;
    $("#emote-export").disabled = !editable || !choice;
    $("#emote-save").disabled = !editable || (!state.emoteReplacement && !choice);
    const current = emote.choices?.find((entry) => entry.name === emote.video_name);
    $("#emote-current").textContent = `原来的表情：${current?.label || (emote.video_name ? "游戏动态表情" : "静态图片")}`;
    $("#emote-reason").textContent = emote.editable === true ? "" : emote.reason || "这个表情暂时不能替换成动态表情。";
    const ids = choice?.affected_ids || [];
    $("#emote-shared").textContent = "仅修改当前表情，其他原生动图保留。";
    $("#emote-shared-ids").textContent = ids.length ? `同时使用这个动图的表情编号：${ids.join("、")}` : "没有其他表情。";
    $("#emote-shared-details")?.classList.toggle("is-hidden", !choice || state.emoteReplacement?.independent === true);
    const candidate = state.emoteReplacement;
    if (candidate) {
      const size = Number(candidate.size);
      const fileSize = Number.isFinite(size) && size >= 0 ? `${(size / 1024 / 1024).toFixed(2)} MiB` : "大小未知";
      const info = candidate.video_info || {};
      const dimensions = info.width && info.height ? `${info.width} × ${info.height}` : "";
      const animation = candidate.animation || {};
      const count = animation.total || animation.frames?.length;
      const summary = [dimensions, count ? `${count} 帧` : ""].filter(Boolean).join(" · ");
      const notice = (candidate.notice || "请对照下面的实际播放效果，再保存到作品集。")
        .replace("预览由 USM 颜色流和透明度流实际解码合成", "预览读取转换后的实际动图");
      $("#emote-file-status").textContent = `已导入：${candidate.source_name || candidate.name} · ${fileSize}${summary ? ` · ${summary}` : ""}。${notice}`;
    } else {
      $("#emote-file-status").textContent = state.emoteImportError || (choice && choice.name !== state.emoteDraftVideoName
        ? "未导入自己的文件：保存后使用所选游戏动图。" : state.emoteDraftVideoName
        ? "已保存到作品集，右侧为保存后的实际效果。"
        : "直接导入你的动图或视频，预览满意后保存。");
    }
    renderEmotePreviews();
  }

  async function onEmoteVideoChange() {
    resetEmoteReplacement();
    resetEmotePreview();
    state.emoteVideoName = $("#emote-video").value;
    state.emoteDraftVideoName = "";
    renderEmoteSettings();
    await loadEmotePreview();
  }

  function renderEmotePreviews() {
    const original = $("#emote-original");
    const modified = $("#emote-replacement");
    if (!original || !modified) return;
    const showAnimation = (media, animation) => {
      if (state.animationPlayers.get(media.id)?.animation !== animation) renderAnimation(media, animation);
    };
    if (!emotePreviewName("original")) setMedia(original, state.selection?.preview_data, "当前表情没有静态预览。");
    else if (state.emotePreviewLoading) renderAnimationState(original, "正在加载当前表情的原动图…");
    else if (state.emotePreview?.frames?.length) showAnimation(original, state.emotePreview);
    else renderAnimationState(original, state.emotePreviewError || state.emotePreview?.notice || "原版动图尚未加载。", () => loadEmotePreview("original"));
    const candidate = state.emoteReplacement;
    if (candidate?.animation?.frames?.length) showAnimation(modified, candidate.animation);
    else if (candidate) renderAnimationState(modified, candidate.animation?.notice || candidate.notice || "这个替换文件没有可播放的预览，请先确认后再安装。");
    else if (state.emoteDraftLoading) renderAnimationState(modified, "正在读取作品集中的实际动图…");
    else if (state.emoteDraftPreview?.frames?.length) showAnimation(modified, state.emoteDraftPreview);
    else renderAnimationState(modified, state.emoteDraftError || state.emoteDraftPreview?.notice || "导入你的动图或视频，在这里查看动态效果。", emotePreviewName("draft") ? () => loadEmotePreview("draft") : null);
  }

  async function loadEmotePreview(variant = "both") {
    if (!state.selection?.emote) return;
    const key = selectionKey();
    const selectionRequest = state.selectionRequest;
    const request = state.emotePreviewRequest;
    const variants = (variant === "both" ? ["original", "draft"] : [variant])
      .filter((source) => emotePreviewName(source));
    if (!variants.length) {
      renderEmotePreviews();
      return;
    }
    const videoNames = variants.map((source) => emotePreviewName(source));
    const variantRequests = variants.map((source) => source === "original"
      ? ++state.emoteOriginalRequest : ++state.emoteDraftRequest);
    if (variants.includes("original")) {
      state.emotePreviewLoading = true;
      state.emotePreviewError = "";
    }
    if (variants.includes("draft")) {
      state.emoteDraftLoading = true;
      state.emoteDraftError = "";
    }
    renderEmotePreviews();
    const results = await Promise.allSettled(variants.map((source, index) =>
      call("get_emote_preview", { quiet: true }, videoNames[index], source)));
    if (request !== state.emotePreviewRequest || key !== selectionKey() ||
        selectionRequest !== state.selectionRequest) return;
    results.forEach((result, index) => {
      const source = variants[index];
      const latest = source === "original" ? state.emoteOriginalRequest : state.emoteDraftRequest;
      if (variantRequests[index] !== latest || videoNames[index] !== emotePreviewName(source)) return;
      const animationKey = source === "original" ? "emotePreview" : "emoteDraftPreview";
      const errorKey = source === "original" ? "emotePreviewError" : "emoteDraftError";
      const loadingKey = source === "original" ? "emotePreviewLoading" : "emoteDraftLoading";
      if (result.status === "fulfilled") state[animationKey] = result.value;
      else {
        state[animationKey] = null;
        state[errorKey] = `动图预览未加载：${result.reason.message || result.reason}`;
      }
      state[loadingKey] = false;
    });
    if (state.page === "studio") renderEmotePreviews();
  }

  async function chooseEmoteFile() {
    const emote = state.selection?.emote;
    if (emote?.editable !== true) return;
    const key = selectionKey();
    const selectionRequest = state.selectionRequest;
    const templateName = emote.default_video_name || emote.video_name || "";
    resetEmoteReplacement();
    const request = state.emoteRequest;
    renderEmoteSettings();
    const current = () => request === state.emoteRequest && selectionRequest === state.selectionRequest &&
      key === selectionKey();
    try {
      const data = await call("choose_emote_file", { busy: true, busyText: "自动识别并转换…" }, templateName);
      if (!current()) return;
      if (data) {
        state.emoteReplacement = { ...data, sourceKey: key, templateName: data.video_name || templateName };
        toast(`已导入：${data.source_name || data.name}，可以播放并逐帧检查效果`);
      }
      renderEmoteSettings();
    } catch (err) {
      if (current()) {
        resetEmoteReplacement();
        state.emoteImportError = `导入没有通过：${err.message || err}。请重新选择动图或视频。`;
        renderEmoteSettings();
      }
    }
  }

  async function exportEmoteVideo() {
    const choice = selectedEmoteVideo();
    if (state.selection?.emote?.editable !== true || !choice) return;
    try {
      const data = await call("export_emote_video", { busy: true, busyText: "导出原 USM…" }, choice.name);
      if (data) toast(`已导出：${data.path || data.name || choice.name}`);
    } catch (_) {}
  }

  async function saveEmote() {
    const choice = selectedEmoteVideo();
    if (state.selection?.emote?.editable !== true) return;
    const key = selectionKey();
    const selectionRequest = state.selectionRequest;
    const request = state.emoteRequest;
    const candidate = state.emoteReplacement;
    const useCustom = !!candidate && candidate.sourceKey === key && !!candidate.templateName;
    const videoName = useCustom ? candidate.templateName : choice?.name;
    if (!videoName) return;
    try {
      const data = await call("save_emote", { busy: true, busyText: "保存动态表情到作品集…" }, videoName, useCustom);
      state.draft = data.draft || state.draft;
      renderDraftList();
      toast("动态表情已保存到作品集，正在读取保存后的实际效果");
      if (request === state.emoteRequest && selectionRequest === state.selectionRequest && key === selectionKey()) {
        resetEmoteReplacement();
        if (useCustom && candidate.independent === true) state.emoteVideoName = "";
        state.emoteDraftVideoName = data.item?.video_name || (candidate?.independent === true ? "" : videoName);
        renderEmoteSettings();
        await loadEmotePreview("draft");
      }
    } catch (err) {
      if (request === state.emoteRequest && selectionRequest === state.selectionRequest && key === selectionKey()) {
        resetEmoteReplacement();
        state.emoteImportError = `保存没有通过：${err.message || err}。请重新选择动图或视频。`;
        renderEmoteSettings();
      }
    } finally {
      if (request === state.emoteRequest && selectionRequest === state.selectionRequest && key === selectionKey()) {
        resetEmoteReplacement();
        renderEmoteSettings();
      }
    }
  }

  function isPlayableAnimation(selection) {
    if (!selection || selection.playable === false) return false;
    const kind = selection.asset_type || selection.kind;
    if (kind === "anim") return selection.playable === true || selection.animation_kind === "sprite_clip";
    return kind === "dynamic" &&
      (["sequence", "gif", "webp", "apng", "cri_usm"].includes(selection.animation_kind) || !!selection.frame_names?.length);
  }

  function isSequenceAnimation(selection) {
    return selection && (selection.asset_type || selection.kind) === "dynamic" &&
      selection.animation_kind !== "sprite_clip" &&
      (selection.animation_kind === "sequence" || !!selection.frame_names?.length);
  }

  function canEdit(selection) {
    if (!selection || selection.editable === false) return false;
    const kind = selection.asset_type || selection.kind;
    return kind !== "mesh" && (kind !== "dynamic" || isPlayableAnimation(selection));
  }

  function animationImportNotice(selection) {
    const dataNotice = "同源 .animbin 用于修改动作数据；替换图集不改变动作顺序和时间。";
    if (selection.atlas_editable !== true) {
      return `${selection.atlas_reason || "此动画没有可直接替换的单张原图集。"} 此处只能导入同源 .animbin。`;
    }
    const atlas = `${selection.atlas_name} · ${selection.atlas_width} × ${selection.atlas_height} 像素`;
    return `整张原图集：${atlas}。\n图片尺寸及碎片布局必须与原图集一致，不能用单张角色帧、PNG 帧文件夹或 GIF 替代，也不能裁剪。先到「浏览资源 → 贴图」搜索图集名并导出 PNG，再按原布局修改。\n${dataNotice}`;
  }

  function syncStudioImportControls() {
    const selection = state.selection;
    const kind = selection?.asset_type || selection?.kind;
    const editable = canEdit(selection);
    const cropBtn = $("#crop-replacement");
    if (cropBtn) {
      cropBtn.classList.toggle("is-hidden", kind !== "texture" || !editable);
      cropBtn.disabled = kind !== "texture" || !editable || !state.replacement?.preview_data;
    }
    const saveBtn = $("#commit-replacement");
    if (saveBtn) saveBtn.disabled = state.replacementImporting || !editable || (kind === "anim" && !state.replacement);
    if (cropBtn && state.replacementImporting) cropBtn.disabled = true;
    const chooseBtn = $("#choose-replacement");
    if (chooseBtn) chooseBtn.disabled = state.replacementImporting;
    const dropzone = $("#studio-import-dropzone");
    const supportsDrop = canDropReplacement(selection);
    dropzone?.classList.toggle("is-disabled", !supportsDrop || state.replacementImporting);
    const hint = $("#studio-drop-hint");
    if (hint) hint.textContent = state.replacementImporting ? "正在读取替换文件，请稍候…"
      : !supportsDrop ? "当前资源请使用上方的动画制作入口或下方选择文件按钮。"
      : kind === "text" ? "拖入一个文本文件，单个不超过 12 MiB；也可点击下方导入文本文件。"
      : kind === "anim" ? "拖入同源 .animbin，或保持原布局的完整图集，单个不超过 12 MiB。"
      : "拖入一张静态图片，单个不超过 12 MiB；动图和视频请用上方专用入口。";
  }

  function previewFps() {
    return Math.max(1, Math.min(120, Number($("#sequence-fps")?.value) || 30));
  }

  function rememberTextEdit() {
    if (!state.editorKey || !$("#studio-text")) return;
    const previous = state.textEdits.get(state.editorKey);
    if (previous) previous.content = $("#studio-text").value;
  }

  function updateTextStatus() {
    const editor = $("#studio-text");
    const original = $("#studio-original-text").value;
    const search = $("#text-find").value;
    const searchable = state.textFields ? Object.values(state.textFields.current) : [editor.value];
    const count = search ? searchable.reduce((total, text) => total + String(text).split(search).length - 1, 0) : 0;
    $("#text-match-count").textContent = search ? `当前修改中找到 ${count} 处（按原样匹配）` : "查找只针对当前资源，保留原有格式和占位符。";
    $("#text-replace-all").disabled = !canEdit(state.selection) || count === 0;
    $("#text-find-next").disabled = count === 0;
    $("#text-edit-status").textContent = `${editor.value.length.toLocaleString()} 字符 · ${editor.value.split("\n").length.toLocaleString()} 行 · ${editor.value === original ? "与原文相同" : "包含修改"}；点“保存到作品集”保留修改。`;
    rememberTextEdit();
  }

  function configureTextFields(selection) {
    state.textFields = null;
    state.textFieldPage = 0;
    if (selection.text_format === "fairygui") {
      try {
        const original = JSON.parse($("#studio-original-text").value);
        const current = JSON.parse($("#studio-text").value);
        if (original && current && !Array.isArray(current) && typeof current === "object" &&
            Object.values(current).every((value) => typeof value === "string")) {
          state.textFields = { original, current };
        }
      } catch (_) {}
    }
    $("#plain-text-editor").classList.toggle("is-hidden", !!state.textFields);
    $("#fairygui-editor").classList.toggle("is-hidden", !state.textFields);
    if (state.textFields) renderTextFields();
  }

  function findNextText() {
    const search = $("#text-find").value;
    if (!search) return;
    let editor = $("#studio-text");
    if (state.textFields) {
      const fields = $$("#fairygui-rows textarea");
      const focused = document.activeElement;
      const start = Math.max(0, fields.indexOf(focused));
      editor = fields.slice(start).find((input) => input.value.indexOf(search, input === focused ? input.selectionEnd : 0) >= 0)
        || fields.find((input) => input.value.includes(search));
      if (!editor) { toast("本页没有匹配的修改文字，可翻页继续查找"); return; }
    }
    let index = editor.value.indexOf(search, editor.selectionEnd);
    if (index < 0) index = editor.value.indexOf(search);
    if (index < 0) return;
    editor.focus();
    editor.setSelectionRange(index, index + search.length);
  }

  function replaceAllText() {
    if (!canEdit(state.selection)) return;
    const search = $("#text-find").value;
    const replacement = $("#text-replace").value;
    if (!search) return;
    let count = 0;
    const replace = (value) => {
      const parts = value.split(search);
      count += parts.length - 1;
      return parts.join(replacement);
    };
    if (state.textFields) {
      for (const key of Object.keys(state.textFields.current)) state.textFields.current[key] = replace(state.textFields.current[key]);
      syncTextFields();
      renderTextFields();
    } else {
      $("#studio-text").value = replace($("#studio-text").value);
      updateTextStatus();
    }
    toast(`已替换 ${count} 处，保存后更新作品集`);
  }

  function syncTextFields() {
    $("#studio-text").value = JSON.stringify(state.textFields.current, null, 2);
    updateTextStatus();
  }

  function renderTextFields() {
    if (!state.textFields) return;
    const { original, current } = state.textFields;
    const search = $("#text-find").value;
    const keys = Object.keys(current).filter((key) => !search || current[key].includes(search) || String(original[key] ?? "").includes(search));
    const pageSize = 40;
    const pages = Math.max(1, Math.ceil(keys.length / pageSize));
    state.textFieldPage = Math.min(state.textFieldPage, pages - 1);
    const rows = $("#fairygui-rows");
    rows.innerHTML = "";
    keys.slice(state.textFieldPage * pageSize, (state.textFieldPage + 1) * pageSize).forEach((key) => {
      const row = document.createElement("div");
      row.className = "fairygui-row";
      const field = document.createElement("code");
      field.textContent = key;
      const before = document.createElement("div");
      before.className = "fairygui-original";
      before.textContent = original[key] ?? "";
      const input = document.createElement("textarea");
      input.value = current[key];
      input.rows = Math.min(6, Math.max(2, current[key].split("\n").length));
      input.readOnly = !canEdit(state.selection);
      input.dataset.field = key;
      input.setAttribute("aria-label", `修改文字 ${key}`);
      input.addEventListener("input", () => { current[key] = input.value; syncTextFields(); });
      row.append(field, before, input);
      rows.appendChild(row);
    });
    if (!keys.length) rows.textContent = "没有匹配的文字。";
    $("#fairygui-page").textContent = `${keys.length} 个字段 · 第 ${state.textFieldPage + 1} / ${pages} 页`;
    $("#fairygui-prev").disabled = state.textFieldPage === 0;
    $("#fairygui-next").disabled = state.textFieldPage >= pages - 1;
  }

  async function refreshStudio() {
    rememberTextEdit();
    const request = ++state.studioRequest;
    const selectionRequest = state.selectionRequest;
    const empty = $("#studio-empty");
    const content = $("#studio-content");
    let sel;
    try {
      sel = await call("get_studio_state", { quiet: true });
    } catch (err) {
      if (request === state.studioRequest && state.page === "studio") toast(err.message, true);
      return;
    }
    if (request !== state.studioRequest || selectionRequest !== state.selectionRequest || state.page !== "studio") return;
    if (selectionKey(sel) !== selectionKey()) {
      resetEmoteReplacement();
      state.emoteVideoName = "";
      state.emoteDraftVideoName = "";
    }
    state.selection = sel;
    renderBrowseComparison();
    if (typeof sel?.emote?.draft_video_name === "string" && state.emoteDraftVideoName !== sel.emote.draft_video_name) {
      state.emoteDraftVideoName = sel.emote.draft_video_name;
      state.emoteDraftRequest += 1;
      state.emoteDraftPreview = null;
      state.emoteDraftError = "";
      state.emoteDraftLoading = false;
    }
    empty.classList.toggle("is-hidden", !!sel);
    content.classList.toggle("is-hidden", !sel);
    renderEmoteSettings();
    window.VisualAnimation?.refresh(sel);
    if (!sel) return;
    const key = selectionKey(sel);
    const emoteLabel = sel.emote?.choices?.find((choice) => choice.name === sel.emote.video_name)?.label;
    $("#studio-title").textContent = sel.emote ? emoteLabel || "静态表情" : sel.caption || sel.name || "";
    $("#studio-description").textContent = sel.category_desc || "";
    const kind = sel.asset_type || sel.kind || "texture";
    const editable = canEdit(sel);
    const animated = isPlayableAnimation(sel);
    const sequence = isSequenceAnimation(sel);
    $("#studio-image-mode").classList.toggle("is-hidden", kind === "text");
    $("#studio-image-mode").classList.toggle("is-readonly", !editable);
    $("#studio-replacement-card")?.classList.toggle("is-hidden", !editable);
    $("#studio-import-dropzone")?.classList.toggle("is-hidden", !editable);
    $("#commit-replacement")?.classList.toggle("is-hidden", !editable);
    $("#studio-text-mode").classList.toggle("is-hidden", kind !== "text");
    $("#studio-original-heading").textContent = sel.emote ? "原静态封面" : "游戏原内容";
    $("#studio-replacement-heading").textContent = sel.emote ? "你的封面图片" : "你的替换文件";
    $("#choose-replacement").classList.toggle("is-hidden", !editable);
    $("#choose-frame-folder").classList.toggle("is-hidden", !editable || !sequence);
    $("#choose-replacement").textContent = kind === "text" ? "导入文本文件…" : kind === "anim"
      ? sel.atlas_editable === true ? "选择同源动画 / 整张图集…" : "选择同源动画…"
      : animated ? "选择替换动画…" : sel.emote ? "更换静态封面…" : "选择替换文件";
    $("#commit-replacement").textContent = sel.emote ? "保存封面到作品集" : "保存到作品集";
    syncStudioImportControls();
    $("#animation-settings").classList.toggle("is-hidden", !animated);
    $("#sequence-fps-label").classList.toggle("is-hidden", !sequence);
    $("#animation-guidance").textContent = kind === "anim"
      ? "按动画片段中的 Sprite 引用顺序和时间播放，可暂停、逐帧查看和调节播放倍速。"
      : !editable ? "当前资源可逐帧查看。替换能力和限制见下方说明。" : sequence
      ? "导入 GIF、WebP 或 APNG 后按原序列帧数量和尺寸适配。此处帧率用于预览，游戏内速度由游戏控制。"
      : "保留动画图片的逐帧时长，可逐帧检查替换前后效果。";
    const animationNotice = kind === "anim" ? animationImportNotice(sel) : "";
    const notice = [sel.reason, animationNotice].filter(Boolean).join(" ") || (!editable ? "该资源目前仅支持预览和导出，不能直接替换。"
      : animated ? "先对照预览，再保存到作品集；安装到游戏后需以实际效果为准。"
      : editable ? "保存只更新作品集，稍后可在作品集中手动安装测试。" : "此类资源暂不支持直接替换。");
    $("#studio-notice").textContent = notice;
    $("#studio-notice").classList.toggle("is-hidden", !notice);
    disposeAnimation("studio-original");
    disposeAnimation("studio-replacement");
    if (kind === "text") {
      const original = sel.original_text ?? sel.full_text ?? sel.text_preview ?? "";
      const saved = state.textEdits.get(key);
      const current = saved?.content ?? sel.full_text ?? original;
      state.editorKey = key;
      state.textEdits.set(key, { content: current });
      $("#studio-original-text").value = original;
      $("#studio-text").value = current;
      $("#studio-text").readOnly = !editable;
      $("#text-restore").disabled = !editable;
      $("#text-replace").disabled = !editable;
      $("#text-format").textContent = [sel.text_encoding, sel.text_format, editable ? "可编辑" : "只读"].filter(Boolean).join(" · ");
      configureTextFields(sel);
      updateTextStatus();
      return;
    }
    state.editorKey = "";
    if (animated) renderAnimationState($("#studio-original"), "正在加载动画…");
    else if (kind === "anim") renderAnimationState($("#studio-original"), sel.reason || "此动画片段暂不支持动作播放；关联图集仅作静态参考。", null, sel.preview_data);
    else setMedia($("#studio-original"), sel.preview_data, "无可视预览");
    renderReplacement();
    if (sel.emote) await loadEmotePreview();
    if (animated) {
      if (editable && !state.replacement) renderAnimationState($("#studio-replacement"), "正在加载作品集动画…");
      const [original, draft] = await Promise.allSettled([
        call("get_animation_preview", { quiet: true }, sel.bundle, sel.name, "original", previewFps()),
        !editable ? Promise.resolve(null) : state.replacement?.animation ? Promise.resolve(state.replacement.animation) :
          call("get_animation_preview", { quiet: true }, sel.bundle, sel.name, "draft", previewFps()),
      ]);
      if (request !== state.studioRequest || key !== selectionKey() || state.page !== "studio") return;
      if (original.status === "fulfilled") {
        state.originalAnimation = original.value;
        renderAnimation($("#studio-original"), original.value, sequence);
      } else {
        renderAnimationState($("#studio-original"), `动画预览未加载：${original.reason.message}`, refreshStudio);
      }
      if (!editable) return;
      if (state.replacement) renderReplacement();
      else if (draft.status === "fulfilled") renderAnimation($("#studio-replacement"), draft.value, sequence);
      else {
        renderAnimationState($("#studio-replacement"), `作品集动画未加载：${draft.reason.message}`, refreshStudio);
      }
    }
  }

  function renderReplacement() {
    const replacement = state.replacement;
    const media = $("#studio-replacement");
    const texture = (state.selection?.asset_type || state.selection?.kind) === "texture";
    const modified = texture ? state.selection?.comparison?.modified : null;
    const heading = $("#studio-replacement-heading");
    const notice = $("#studio-comparison-notice");
    if (texture && heading) heading.textContent = replacement ? "新替换图片 · 尚未保存" : modified ? "作品集修改后 · 已保存" : "你的替换图片";
    if (notice) {
      notice.classList.toggle("is-hidden", !texture);
      notice.textContent = !texture ? "" : replacement ? modified
        ? "当前正在预览新文件，保存后覆盖作品集中这张图片的旧修改。"
        : "新文件尚未保存；保存后会加入当前作品集。"
        : state.selection.comparison?.notice || (modified ? "这是当前作品集中保存的修改；可继续选择文件替换。" : "当前作品集尚未修改这张贴图。");
    }
    if (replacement?.animation) {
      renderAnimation(media, replacement.animation, isSequenceAnimation(state.selection));
    } else {
      const clip = (state.selection?.asset_type || state.selection?.kind) === "anim";
      const empty = clip ? state.selection.atlas_editable === true
        ? "尚未导入替换。请选择同源 .animbin，或按原布局修改的整张图集。"
        : "尚未导入替换。请选择同源 .animbin 动画数据。"
        : "选择文件后可在这里对照预览";
      setMedia(media, replacement?.preview_data || (!replacement ? modified?.preview_data : null), replacement?.name || (texture ? state.selection.comparison?.notice || "当前作品集无修改，选择文件后可预览" : empty));
    }
  }

  function disposeAnimation(id) {
    const player = state.animationPlayers.get(id);
    if (player) { clearTimeout(player.timer); state.animationPlayers.delete(id); }
    const media = document.getElementById(id);
    media?.classList.remove("has-animation");
  }

  function stopAllAnimations() {
    for (const id of Array.from(state.animationPlayers.keys())) disposeAnimation(id);
    stopSequencePreview();
    stopLightboxSequence();
  }

  function renderAnimation(media, animation, sequence = false) {
    disposeAnimation(media.id);
    if (!animation?.frames?.length) { renderAnimationState(media, animation?.notice || "没有可播放的帧"); return; }
    media.classList.add("has-animation");
    media.innerHTML = `<div class="animation-stage"><img alt="动画逐帧预览" draggable="false"></div>
      <div class="animation-controls">
        <div class="animation-buttons"><button type="button" class="button ghost compact" data-animation="previous" title="上一帧">上一帧</button><button type="button" class="button primary compact" data-animation="play">暂停</button><button type="button" class="button ghost compact" data-animation="next" title="下一帧">下一帧</button><select aria-label="动画播放速度"><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option></select></div>
        <input type="range" min="0" max="${animation.frames.length - 1}" value="0" step="1" aria-label="动画帧进度">
        <span class="animation-position" aria-live="off"></span><small class="animation-notice"></small>
      </div>`;
    const player = { animation, index: 0, speed: 1, playing: true, timer: null, sequence };
    const img = media.querySelector("img");
    const progress = media.querySelector('input[type="range"]');
    const playButton = media.querySelector('[data-animation="play"]');
    const position = media.querySelector(".animation-position");
    const notice = media.querySelector(".animation-notice");
    const frameDurations = animation.frames.map((_, index) => Number(animation.durations?.[index]) || 1000 / (Number(animation.fps) || 30));
    const durationSeconds = frameDurations.reduce((total, duration) => total + duration, 0) / 1000;
    notice.textContent = [animation.notice, animation.truncated ? `抽样显示 ${animation.frames.length} / ${animation.total} 帧` : ""].filter(Boolean).join("；");
    state.animationPlayers.set(media.id, player);
    const renderFrame = () => {
      img.src = animation.frames[player.index];
      progress.value = String(player.index);
      position.textContent = `${player.index + 1} / ${animation.frames.length} 帧 · ${durationSeconds.toFixed(2)} 秒 · ${animation.width || "?"} × ${animation.height || "?"}`;
      playButton.textContent = player.playing ? "暂停" : "播放";
    };
    const schedule = () => {
      clearTimeout(player.timer);
      if (!player.playing || animation.frames.length < 2 || document.hidden) return;
      const sourceFps = Number(animation.fps) || 30;
      const frameDuration = frameDurations[player.index];
      const duration = sequence ? frameDuration * sourceFps / previewFps() : frameDuration;
      player.timer = setTimeout(() => {
        if (state.animationPlayers.get(media.id) !== player || !media.isConnected) return;
        player.index = (player.index + 1) % animation.frames.length;
        renderFrame(); schedule();
      }, Math.max(8, duration / player.speed));
    };
    player.reschedule = schedule;
    media.querySelectorAll("[data-animation]").forEach((button) => button.addEventListener("click", () => {
      if (button.dataset.animation === "play") player.playing = !player.playing;
      else {
        player.playing = false;
        player.index = (player.index + (button.dataset.animation === "next" ? 1 : -1) + animation.frames.length) % animation.frames.length;
      }
      renderFrame(); schedule();
    }));
    progress.addEventListener("input", () => { player.playing = false; player.index = Number(progress.value); renderFrame(); schedule(); });
    media.querySelector("select").addEventListener("change", (event) => { player.speed = Number(event.target.value); schedule(); });
    renderFrame(); schedule();
  }

  function renderAnimationState(media, message, retry, previewData) {
    if (!media) return;
    disposeAnimation(media.id);
    media.innerHTML = "";
    const status = document.createElement("div");
    status.className = "animation-state";
    status.setAttribute("role", "status");
    if (previewData) {
      const img = document.createElement("img");
      img.src = previewData;
      img.alt = "关联图集（静态参考）";
      status.appendChild(img);
      const caption = document.createElement("small");
      caption.textContent = "关联图集 · 静态参考";
      status.appendChild(caption);
    }
    const label = document.createElement("span");
    label.textContent = message;
    status.appendChild(label);
    if (retry) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button ghost compact";
      button.textContent = "重新加载预览";
      button.addEventListener("click", retry);
      status.appendChild(button);
    }
    media.appendChild(status);
  }

  function setMedia(el, dataUrl, fallback) {
    if (!el) return;
    disposeAnimation(el.id);
    if (dataUrl) el.innerHTML = `<img src="${dataUrl}" alt="">`;
    else el.innerHTML = `<span>${escapeHtml(fallback || "—")}</span>`;
  }

  function openLightbox(src, title, frames, durations) {
    const overlay = $("#lightbox-overlay");
    const img = $("#lightbox-image");
    const titleEl = $("#lightbox-title");
    if (!overlay || !img || !src) return;
    state.lightbox.scale = 1;
    state.lightbox.tx = 0;
    state.lightbox.ty = 0;
    state.lightbox.dragging = false;
    stopLightboxSequence();
    if (titleEl) titleEl.textContent = title || "图片预览";
    overlay.classList.remove("is-hidden");
    let initialized = false;
    const init = () => {
      if (initialized) return;
      initialized = true;
      resetLightboxView();
      if (frames && frames.length) startLightboxSequence(frames, state.sequenceFps || 30, durations);
    };
    img.onload = () => {
      img.onload = null;
      requestAnimationFrame(init);
    };
    img.src = src;
    if (img.complete && img.naturalWidth) {
      requestAnimationFrame(init);
    }
  }

  function closeLightbox() {
    const overlay = $("#lightbox-overlay");
    const img = $("#lightbox-image");
    const stage = $("#lightbox-stage");
    if (!overlay) return;
    overlay.classList.add("is-hidden");
    stopLightboxSequence();
    if (img) img.src = "";
    if (stage) stage.classList.remove("dragging");
    state.lightbox.dragging = false;
  }

  function resetLightboxView() {
    const stage = $("#lightbox-stage");
    const img = $("#lightbox-image");
    if (!stage || !img) return;
    if (!img.naturalWidth) {
      // 某些大图 dataURL 在 complete 后仍未解码完成，等待后再适配
      requestAnimationFrame(resetLightboxView);
      return;
    }
    // 以实际窗口客户区为准，避免个别环境下 stage.getBoundingClientRect 返回异常尺寸。
    const viewportW = window.innerWidth || stage.clientWidth || 800;
    const viewportH = window.innerHeight || stage.clientHeight || 600;
    const toolbar = $("#lightbox-toolbar");
    const toolbarH = toolbar ? toolbar.getBoundingClientRect().height : 56;
    const availW = Math.max(100, viewportW);
    const availH = Math.max(100, viewportH - toolbarH);
    const pad = 48;
    const fit = Math.min(
      1,
      (availW - pad) / img.naturalWidth,
      (availH - pad) / img.naturalHeight
    );
    state.lightbox.scale = Math.max(0.1, fit || 1);
    state.lightbox.tx = (availW - img.naturalWidth * state.lightbox.scale) / 2;
    state.lightbox.ty = (availH - img.naturalHeight * state.lightbox.scale) / 2;
    // 固定图片布局尺寸，切换序列帧时保持缩放/平移对齐
    img.style.width = img.naturalWidth + "px";
    img.style.height = img.naturalHeight + "px";
    applyLightboxTransform();
  }

  function applyLightboxTransform() {
    const img = $("#lightbox-image");
    const s = state.lightbox;
    if (!img) return;
    // 必须固定为左上角原点，否则缩放会放大 translate 偏移导致无法居中
    img.style.transformOrigin = "0 0";
    img.style.transform = `translate(${s.tx}px, ${s.ty}px) scale(${s.scale})`;
  }

  function handleLightboxWheel(e) {
    e.preventDefault();
    const stage = $("#lightbox-stage");
    const s = state.lightbox;
    if (!stage || !s) return;
    const rect = stage.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
    const newScale = Math.min(20, Math.max(0.1, s.scale * factor));
    const ix = (mx - s.tx) / s.scale;
    const iy = (my - s.ty) / s.scale;
    s.tx = mx - ix * newScale;
    s.ty = my - iy * newScale;
    s.scale = newScale;
    applyLightboxTransform();
  }

  function startLightboxDrag(e) {
    if (e.button !== 0) return;
    const s = state.lightbox;
    s.dragging = true;
    s.startX = e.clientX;
    s.startY = e.clientY;
    s.origTx = s.tx;
    s.origTy = s.ty;
    const stage = $("#lightbox-stage");
    if (stage) stage.classList.add("dragging");
    e.preventDefault();
  }

  function moveLightboxDrag(e) {
    const s = state.lightbox;
    if (!s.dragging) return;
    s.tx = s.origTx + (e.clientX - s.startX);
    s.ty = s.origTy + (e.clientY - s.startY);
    applyLightboxTransform();
  }

  function endLightboxDrag() {
    state.lightbox.dragging = false;
    const stage = $("#lightbox-stage");
    if (stage) stage.classList.remove("dragging");
  }

  function stopSequencePreview() {
    if (state.sequenceTimer) {
      clearInterval(state.sequenceTimer);
      state.sequenceTimer = null;
    }
  }

  function stopLightboxSequence() {
    if (state.lightboxSequenceTimer) {
      clearTimeout(state.lightboxSequenceTimer);
      state.lightboxSequenceTimer = null;
    }
    state.lightboxFrames = [];
  }

  function startSequencePreview(img, frames, fps) {
    stopSequencePreview();
    if (!img || !frames || !frames.length) return;
    let index = 0;
    img.src = frames[0];
    const interval = Math.max(33, Math.round(1000 / (fps || 30)));
    state.sequenceTimer = setInterval(() => {
      index = (index + 1) % frames.length;
      img.src = frames[index];
    }, interval);
  }

  function startLightboxSequence(frames, fps, durations) {
    stopLightboxSequence();
    const img = $("#lightbox-image");
    if (!img || !frames || !frames.length) return;
    state.lightboxFrames = frames;
    let index = 0;
    img.src = frames[0];
    const nextFrame = () => {
      const duration = Number(durations?.[index]) || 1000 / (fps || 30);
      state.lightboxSequenceTimer = setTimeout(() => {
        index = (index + 1) % frames.length;
        img.src = frames[index];
        nextFrame();
      }, Math.max(8, duration));
    };
    if (frames.length > 1) nextFrame();
  }

  function renderDraftList() {
    const list = $("#draft-list");
    const count = $("#draft-count");
    const navCount = $("#nav-pack-count");
    const items = state.draft?.items || [];
    if (count) count.textContent = `${items.length} 项`;
    if (navCount) navCount.textContent = items.length ? String(items.length) : "";
    const nameInput = $("#draft-name");
    if (nameInput && document.activeElement !== nameInput) nameInput.value = state.draft?.name || "";
    if (!list) return;
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = `<div class="notice">作品集是空的。去浏览资源替换后加入。</div>`;
      return;
    }
    items.forEach((item, index) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "draft-item" + (index === state.draftIndex ? " is-active" : "");
      const kind = item.kind === "texture" ? "图" : item.kind === "text" ? "文" : ["anim", "dynamic", "emote", "visual_animation"].includes(item.kind) ? "动" : item.kind || "?";
      btn.innerHTML = `<span>[${kind}] ${escapeHtml(item.name || "")}</span><small>${escapeHtml(item.note || "")}</small>`;
      btn.addEventListener("click", () => showDraftDetail(index));
      list.appendChild(btn);
    });
  }

  async function refreshPack() {
    try {
      state.draft = (await call("get_draft", { quiet: true })) || state.draft;
    } catch (_) {}
    renderDraftList();
    if (state.draftIndex >= 0 && state.draftIndex < (state.draft.items || []).length) {
      await showDraftDetail(state.draftIndex);
    } else {
      state.draftIndex = -1;
      $("#draft-detail-title").textContent = "尚未选择";
      setMedia($("#draft-original"), null, "—");
      setMedia($("#draft-modified"), null, "—");
      $("#draft-mode-notice").textContent = "";
      $("#draft-mode-notice").classList.add("is-hidden");
      $("#draft-replace").disabled = true;
      $("#draft-crop").disabled = true;
      $("#draft-remove").disabled = true;
    }
  }

  async function showDraftDetail(index) {
    closeCropModal();
    const request = ++state.draftDetailRequest;
    const name = state.draft.items?.[index]?.name || "资源";
    $("#draft-detail-title").textContent = `正在加载 · ${name}`;
    $("#draft-replace").disabled = true;
    $("#draft-crop").disabled = true;
    $("#draft-remove").disabled = true;
    $("#draft-mode-notice").textContent = "";
    $("#draft-mode-notice").classList.add("is-hidden");
    setMedia($("#draft-original"), null, "正在加载原内容…");
    setMedia($("#draft-modified"), null, "正在加载修改内容…");
    state.draftIndex = index;
    renderDraftList();
    try {
      const data = await call("get_draft_detail", { quiet: true }, index);
      if (request !== state.draftDetailRequest || state.page !== "pack") return;
      const item = data.item || {};
      const kindLabel = item.from_anim ? "动画图集" : { texture: "图片", text: "文字", anim: "动画片段", dynamic: "动画", emote: "动态表情", visual_animation: data.visual_info?.label || "动态卡面 / 立绘（试用）" }[item.kind] || "资源";
      $("#draft-detail-title").textContent = `${kindLabel} · ${item.name || ""}`;
      setMedia($("#draft-original"), data.original_data, "无预览");
      setMedia($("#draft-modified"), data.modified_data, "无预览");
      if (item.kind === "visual_animation") {
        $("#draft-mode-notice").textContent = data.visual_info?.notice || "动态卡面 / 立绘试用功能。右侧读取作品集实际动画；安装后请在游戏中测试。";
        $("#draft-mode-notice").classList.remove("is-hidden");
        if (!data.modified_animation?.frames?.length) {
          renderAnimationState($("#draft-modified"), data.modified_animation?.notice || "动态预览未加载，请重新打开作品集检查。");
        }
      }
      if (item.kind === "emote") {
        const info = data.emote_info || {};
        const independent = info.independent_video === true || item.independent_video === true || info.independent === true || item.independent === true;
        const mode = independent ? "使用自己的动图：仅修改当前表情，其他原生动图保留。"
          : info.custom_video ? "使用自己的动图：其他使用同一个游戏动图的表情也会一起变。" : "使用游戏现有动图：只更改当前表情，其他表情不会变。";
        $("#draft-mode-notice").textContent = `${info.video_label || (independent ? "自定义动态表情" : "已选择的游戏动态表情")}。${mode} ${info.preview_notice || "右侧预览读取作品集中的实际动图，安装后请在游戏中确认显示效果。"}`;
        $("#draft-mode-notice").classList.remove("is-hidden");
        if (data.emote_original_animation?.frames?.length) renderAnimation($("#draft-original"), data.emote_original_animation);
        if (data.emote_animation?.frames?.length) renderAnimation($("#draft-modified"), data.emote_animation);
        else renderAnimationState($("#draft-modified"), data.emote_animation?.notice || "动态预览暂时不可用，请重新加载；原静态封面不代表动态播放效果。");
      }
      if (data.original_text != null || data.modified_text != null) {
        setTextMedia($("#draft-original"), data.original_text ?? "无原文");
        setTextMedia($("#draft-modified"), data.modified_text ?? "无修改文本");
      }
      for (const side of ["original", "modified"]) {
        const animation = data[`${side}_animation`];
        if (!animation) continue;
        const media = $(`#draft-${side}`);
        if (animation.frames?.length) renderAnimation(media, animation);
        else renderAnimationState(media, animation.notice || "此动画暂不支持播放。", null, data[`${side}_data`]);
      }
      $("#draft-replace").disabled = item.kind !== "texture";
      $("#draft-replace").textContent = item.from_anim ? "替换整张图集并保存…" : "换图并保存…";
      $("#draft-replace").title = item.from_anim ? "仅接受与原图集尺寸相同、碎片布局一致的完整图片" : "";
      $("#draft-crop").disabled = item.kind !== "texture" || !!item.from_anim;
      $("#draft-crop").title = item.from_anim ? "动画图集须保留原碎片布局，不能裁剪" : "";
      $("#draft-remove").disabled = false;
    } catch (err) {
      if (request === state.draftDetailRequest && state.page === "pack") {
        $("#draft-detail-title").textContent = `预览未加载 · ${name}`;
        renderAnimationState($("#draft-original"), `预览未加载：${err.message}`, () => showDraftDetail(index));
        setMedia($("#draft-modified"), null, "预览未加载，点击左侧按钮重试。");
        toast(err.message, true);
      }
    }
  }

  function setTextMedia(media, text) {
    disposeAnimation(media.id);
    media.innerHTML = "";
    const pre = document.createElement("pre");
    pre.textContent = text;
    media.appendChild(pre);
  }

  async function refreshLogs() {
    try {
      const lines = await call("get_logs", { quiet: true });
      const out = $("#log-output");
      if (out) out.textContent = (lines || []).join("\n") || "（暂无日志）";
    } catch (_) {}
  }

  function appendLogLine(line) {
    const out = $("#log-output");
    if (!out) return;
    if (!out.textContent || out.textContent === "（暂无日志）") out.textContent = line;
    else out.textContent += "\n" + line;
    out.scrollTop = out.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function shortBundleName(value) {
    const name = String(value || "").replace(/\.bundle$/i, "");
    if (name.length <= 16) return name || "未知包";
    return `${name.slice(0, 8)}…${name.slice(-6)}`;
  }

  function bindEvents() {
    bindReplacementDrop();
    window.VisualAnimation?.init({
      call, toast, renderAnimation, renderAnimationState,
      getSelection: () => state.selection,
      isStudio: () => state.page === "studio",
      onSave(data) {
        if (data?.draft) state.draft = data.draft;
        renderDraftList();
        refreshSelectionComparison();
      },
    });
    window.ModWorkshop?.init({
      call,
      toast,
      onInstall(data) {
        if (data.dashboard) state.dashboard = data.dashboard;
        if (data.installed) state.installed = data.installed;
        renderDashboard();
        renderInstalled();
      },
    });
    window.AudioWorkbench?.init({
      call,
      toast,
      headers: API_HEADERS,
      onInstall(data) {
        if (data.dashboard) state.dashboard = data.dashboard;
        if (data.installed) state.installed = data.installed;
        renderDashboard();
        renderInstalled();
      },
    });
    $$(".nav-button").forEach((btn) => btn.addEventListener("click", () => showPage(btn.dataset.page)));
    $$("[data-page-link]").forEach((btn) =>
      btn.addEventListener("click", () => showPage(btn.dataset.pageLink))
    );

    $("#mobile-nav-toggle")?.addEventListener("click", () => {
      document.body.classList.toggle("nav-open");
    });

    $("#sidebar-launch")?.addEventListener("click", () => doAction("launch-game"));
    $("#sidebar-restore")?.addEventListener("click", () => doAction("restore-all"));

    $$("[data-action]").forEach((btn) =>
      btn.addEventListener("click", () => doAction(btn.dataset.action))
    );

    // 点击任意预览小图，打开全屏大图预览
    document.addEventListener("click", (e) => {
      const img = e.target.closest(".preview-media img");
      if (img && img.src) {
        const isBrowsePreview = img.closest("#preview-media") !== null;
        const player = state.animationPlayers.get(img.closest(".preview-media")?.id);
        const animation = player?.animation || (isBrowsePreview ? state.previewAnimation : null);
        openLightbox(img.src, img.alt || "图片预览", animation?.frames, animation?.durations);
      }
    });

    // 全屏大图预览：关闭 / 滚轮缩放 / 拖拽平移
    $("#lightbox-close")?.addEventListener("click", closeLightbox);
    $("#lightbox-overlay")?.addEventListener("click", (e) => {
      if (e.target === $("#lightbox-overlay") || e.target === $("#lightbox-stage")) {
        closeLightbox();
      }
    });
    $("#lightbox-stage")?.addEventListener("wheel", handleLightboxWheel, { passive: false });
    $("#lightbox-image")?.addEventListener("mousedown", startLightboxDrag);
    window.addEventListener("mousemove", moveLightboxDrag);
    window.addEventListener("mouseup", endLightboxDrag);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeLightbox();
    });

    $("#choose-mod-folder")?.addEventListener("click", () => chooseMod("folder"));
    $("#choose-mod-archive")?.addEventListener("click", () => chooseMod("archive"));
    $("#install-mod")?.addEventListener("click", installMod);
    $("#choose-migration-file")?.addEventListener("click", () => chooseMigrationSource("file"));
    $("#choose-migration-folder")?.addEventListener("click", () => chooseMigrationSource("folder"));
    $("#run-migration")?.addEventListener("click", runMigration);

    $("#asset-type")?.addEventListener("change", (e) => {
      rememberTextEdit();
      state.selectionRequest += 1;
      state.studioRequest += 1;
      stopAllAnimations();
      state.previewAnimation = null;
      state.replacement = null;
      resetEmoteReplacement();
      resetEmotePreview();
      state.emoteVideoName = "";
      state.emoteDraftVideoName = "";
      state.emoteSelectionKey = "";
      state.assetType = e.target.value;
      state.categoryId = state.assetType === "texture" ? "hand_card" : "all";
      state.resourcePage = 0;
      state.selection = null;
      exitMultiSelect();
      renderPreview();
      const hint = $("#asset-type-hint");
      if (hint) hint.textContent = TYPE_HINTS[state.assetType] || "";
      refreshBrowse();
    });
    $("#resource-search-button")?.addEventListener("click", () => {
      state.query = $("#resource-search").value.trim();
      state.resourcePage = 0;
      exitMultiSelect();
      loadResources();
    });
    $("#character-filter")?.addEventListener("change", (e) => {
      state.characterFilter = e.target.value;
      state.resourcePage = 0;
      exitMultiSelect();
      loadResources();
    });
    $("#multi-select-toggle")?.addEventListener("click", () => {
      setMultiSelectMode(!state.multiSelectMode);
    });
    $("#apply-resource-character")?.addEventListener("click", async () => {
      const char = $("#resource-character")?.value || "";
      if (state.multiSelectMode) {
        if (!state.selectedResources.length) {
          toast("请先选择要批量标注的资源", true);
          return;
        }
        const count = state.selectedResources.length;
        const items = state.selectedResources.map((r) => ({ bundle: r.bundle, name: r.name }));
        state.characterLabels = await call("set_resources_character", { quiet: true }, items, char);
        fillResourceCharacterSelect();
        setMultiSelectMode(false);
        await loadResources();
        toast(`已批量标注 ${count} 个资源`);
        return;
      }
      if (!state.selection) return;
      state.characterLabels = await call("set_resource_character", { quiet: true }, state.selection.bundle, state.selection.name, char);
      fillResourceCharacterSelect();
      const eff = effectiveCharacter(state.selection.bundle, state.selection.name);
      const charSel = $("#resource-character");
      if (charSel) charSel.value = eff || "";
      await loadResources();
    });
    $("#apply-bundle-character")?.addEventListener("click", async () => {
      if (state.multiSelectMode || !state.selection) return;
      const char = $("#resource-character")?.value || "";
      state.characterLabels = await call("set_bundle_character", { quiet: true }, state.selection.bundle, char);
      fillResourceCharacterSelect();
      const eff = effectiveCharacter(state.selection.bundle, state.selection.name);
      const charSel = $("#resource-character");
      if (charSel) charSel.value = eff || "";
      await loadResources();
    });
    $("#resource-search")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        state.query = e.target.value.trim();
        state.resourcePage = 0;
        exitMultiSelect();
        loadResources();
      }
    });
    $("#build-index")?.addEventListener("click", buildIndex);
    $("#validate-dynamic")?.addEventListener("click", validateDynamic);
    $("#resource-prev")?.addEventListener("click", () => {
      if (state.resourcePage > 0) {
        state.resourcePage -= 1;
        loadResources();
      }
    });
    $("#resource-next")?.addEventListener("click", () => {
      const pages = Math.max(1, Math.ceil((state.totalResources || 0) / PAGE_SIZE));
      if (state.resourcePage < pages - 1) {
        state.resourcePage += 1;
        loadResources();
      }
    });
    $("#go-studio")?.addEventListener("click", () => {
      if ((state.selection?.asset_type || state.selection?.kind) === "mesh") {
        toast("3D 模型只支持导出，不替换", true);
        return;
      }
      showPage("studio");
    });
    $("#export-primary")?.addEventListener("click", () => exportSelection("primary"));
    $("#export-secondary")?.addEventListener("click", () => exportSelection("secondary"));
    $("#refresh-resource")?.addEventListener("click", refreshResource);

    $("#choose-replacement")?.addEventListener("click", () => chooseReplacement(false));
    $("#choose-frame-folder")?.addEventListener("click", () => chooseReplacement(true));
    $("#crop-replacement")?.addEventListener("click", cropReplacement);
    $("#commit-replacement")?.addEventListener("click", commitReplacement);
    $("#emote-video")?.addEventListener("change", onEmoteVideoChange);
    $("#emote-export")?.addEventListener("click", exportEmoteVideo);
    $("#emote-import")?.addEventListener("click", chooseEmoteFile);
    $("#emote-save")?.addEventListener("click", saveEmote);
    $("#studio-text")?.addEventListener("input", updateTextStatus);
    $("#text-find")?.addEventListener("input", () => { state.textFieldPage = 0; updateTextStatus(); renderTextFields(); });
    $("#text-find-next")?.addEventListener("click", findNextText);
    $("#text-replace-all")?.addEventListener("click", replaceAllText);
    $("#text-restore")?.addEventListener("click", () => {
      if (!canEdit(state.selection)) return;
      $("#studio-text").value = $("#studio-original-text").value;
      configureTextFields(state.selection);
      updateTextStatus();
      toast("已恢复原文，保存后更新作品集");
    });
    $("#fairygui-prev")?.addEventListener("click", () => { state.textFieldPage = Math.max(0, state.textFieldPage - 1); renderTextFields(); });
    $("#fairygui-next")?.addEventListener("click", () => { state.textFieldPage += 1; renderTextFields(); });
    $("#sequence-fps")?.addEventListener("change", () => {
      $("#sequence-fps").value = String(previewFps());
      for (const player of state.animationPlayers.values()) player.reschedule();
    });
    document.addEventListener("visibilitychange", () => {
      for (const player of state.animationPlayers.values()) {
        if (document.hidden) clearTimeout(player.timer);
        else player.reschedule();
      }
      if (document.hidden) stopLightboxSequence();
    });
    window.addEventListener("beforeunload", stopAllAnimations);

    $("#crop-reset")?.addEventListener("click", resetCropFull);
    $("#crop-cancel")?.addEventListener("click", closeCropModal);
    $("#crop-confirm")?.addEventListener("click", async () => {
      if (!cropState || !cropPending) return;
      if (!isCropCurrent()) return;
      const box = cropBoxInPixels(cropState);
      const onConfirm = cropPending.onConfirm;
      cropPending.onCancel = null;
      closeCropModal();
      try {
        await onConfirm(box);
      } catch (_) {}
    });
    $("#crop-stage")?.addEventListener("pointerdown", startCropDrag);
    $("#crop-stage")?.addEventListener("wheel", onCropWheel, { passive: false });
    $("#crop-zoom")?.addEventListener("input", (e) => setCropZoom(Number(e.target.value) / 100));
    window.addEventListener("resize", resizeCropStage);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && cropPending) closeCropModal();
    });

    $("#draft-name")?.addEventListener("change", async (e) => {
      try {
        state.draft = await call("set_draft_name", { quiet: true }, e.target.value);
        renderDraftList();
      } catch (_) {}
    });
    $("#draft-replace")?.addEventListener("click", async () => {
      if (state.draftIndex < 0) return;
      if (state.draft.items?.[state.draftIndex]?.kind !== "texture") return;
      try {
        const data = await call("replace_draft_image", { busy: true, busyText: "换图并保存中…" }, state.draftIndex);
        if (!data) return;
        state.draft = data.draft;
        state.installed = data.installed || state.installed;
        state.dashboard = data.dashboard || state.dashboard;
        renderInstalled();
        renderDashboard();
        toast("换图已保存到作品集");
        await refreshSelectionComparison();
        await showDraftDetail(state.draftIndex);
      } catch (_) {}
    });
    $("#draft-crop")?.addEventListener("click", cropReplaceDraft);
    $("#draft-remove")?.addEventListener("click", async () => {
      if (state.draftIndex < 0) return;
      try {
        const data = await call("remove_draft_item", { busy: true, busyText: "移除并同步中…" }, state.draftIndex);
        state.draft = data.draft;
        state.installed = data.installed || state.installed;
        state.dashboard = data.dashboard || state.dashboard;
        renderInstalled();
        renderDashboard();
        state.draftIndex = -1;
        await refreshSelectionComparison();
        await refreshPack();
        toast("已从作品集移除");
      } catch (_) {}
    });
    $("#draft-export")?.addEventListener("click", async () => {
      try {
        const data = await call("export_draft", { busy: true, busyText: "导出 ZIP…" });
        toast(`已导出：${data.path}`);
      } catch (_) {}
    });
    $("#draft-install")?.addEventListener("click", async () => {
      try {
        const data = await call("install_draft", { busy: true, busyText: "安装作品集…" });
        state.installed = data.installed || state.installed;
        state.dashboard = data.dashboard || state.dashboard;
        renderDashboard();
        toast("作品集已安装到游戏");
      } catch (_) {}
    });
    $("#draft-open-dir")?.addEventListener("click", () => call("open_made_dir", { quiet: false }));
    $("#draft-clear")?.addEventListener("click", async () => {
      if (!confirm("清空整个作品集？")) return;
      try {
        const data = await call("clear_draft", { busy: true, busyText: "清空并同步中…" });
        state.draft = data.draft;
        state.installed = data.installed || state.installed;
        state.dashboard = data.dashboard || state.dashboard;
        renderInstalled();
        renderDashboard();
        state.draftIndex = -1;
        await refreshSelectionComparison();
        await refreshPack();
        toast("作品集已清空");
      } catch (_) {}
    });

    $("#clear-logs")?.addEventListener("click", async () => {
      await call("clear_logs", { quiet: true });
      $("#log-output").textContent = "（暂无日志）";
    });
  }

  async function doAction(action) {
    try {
      if (action === "launch-game") {
        await call("launch_game", { busy: true, busyText: "启动游戏…" });
        toast("已尝试启动游戏");
      } else if (action === "restore-all") {
        if (!confirm("一键全还原：恢复所有备份的原始资源包？")) return;
        const data = await call("restore_all", { busy: true, busyText: "还原中…" });
        state.dashboard = data.dashboard || state.dashboard;
        state.installed = data.installed || [];
        renderDashboard();
        renderInstalled();
        toast(`已还原 ${data.restored || 0} 个资源包`);
      } else if (action === "refresh-detection") {
        state.dashboard = await call("refresh_detection", { busy: true, busyText: "检测中…" });
        renderDashboard();
        toast("已刷新检测");
      } else if (action === "open-assets") {
        await call("open_asset_dir");
      } else if (action === "quick-sfw-texture") {
        if (!confirm("将扫描所有 *_sfw 贴图，并用同包无 _sfw 版本替换后自动安装为 Mod（自动备份原文件）。继续？")) return;
        const data = await call("quick_create_sfw_texture", { busy: true, busyText: "正在创建并安装去SFW贴图Mod…" });
        state.installed = data.installed || state.installed;
        state.dashboard = data.dashboard || state.dashboard;
        renderInstalled();
        renderDashboard();
        toast(`已创建并安装：${data.name}，替换 ${data.pairs} 张贴图 / ${data.bundle_count} 个资源包`);
      }
    } catch (_) {}
  }

  async function chooseMod(mode) {
    try {
      const data = await call("choose_mod", { busy: true, busyText: "分析 Mod…" }, mode);
      if (!data) return;
      state.pendingMod = data;
      const box = $("#mod-analysis");
      box.textContent = `已选：${data.name}\n路径：${data.path}\n可装 ${data.matched} / 共 ${data.total}（跳过 ${data.unmatched}）`;
      $("#install-mod").disabled = !(data.matched > 0);
      setModPreviewPanel(data.preview_title || `待装 · ${data.name}`, data.bundles || [], null);
      if (data.bundles && data.bundles.length) {
        await previewModBundle(data.bundles[0]);
      }
      toast(`分析完成：可装 ${data.matched} 个包，可点下方列表预览`);
    } catch (_) {}
  }

  async function installMod() {
    try {
      const data = await call("install_pending_mod", { busy: true, busyText: "安装中…" });
      state.installed = data.installed || [];
      state.dashboard = data.dashboard || state.dashboard;
      state.pendingMod = null;
      $("#install-mod").disabled = true;
      $("#mod-analysis").textContent = `已安装「${data.name}」：替换 ${data.matched} 个包。下方可继续预览。`;
      if (data.bundles && data.bundles.length) {
        setModPreviewPanel(data.preview_title || `已装 · ${data.name}`, data.bundles, null);
        await previewModBundle(data.bundles[0]);
      }
      renderInstalled();
      renderDashboard();
      toast(`安装完成：${data.name}`);
    } catch (_) {}
  }

  function migrationReportText(report, finished = false) {
    if (!report) return "还没有选择旧 Mod。";
    const prefix = finished
      ? `迁移完成：已加入作品集 ${report.added || 0} 张。`
      : `已扫描 ${report.files || 0} 个包、${report.textures || 0} 张贴图。`;
    const details = `可匹配 ${report.matched || 0}，重名歧义 ${report.ambiguous || 0}，新版缺失 ${report.missing || 0}`;
    const failed = finished && report.failed ? `，失败 ${report.failed}` : "";
    const warning = report.warnings?.length ? `\n另有 ${report.warnings.length} 个文件无法读取。` : "";
    return `${prefix}\n${details}${failed}。${warning}`;
  }

  async function chooseMigrationSource(mode) {
    try {
      const data = await call(
        "choose_migration_source",
        { busy: true, busyText: "分析旧 Mod…" },
        mode
      );
      if (!data) return;
      state.migrationSource = data;
      $("#migration-analysis").textContent = `${data.path}\n${migrationReportText(data)}`;
      $("#run-migration").disabled = !(data.matched > 0);
      toast(`找到 ${data.matched || 0} 张可迁移贴图`);
    } catch (_) {}
  }

  async function runMigration() {
    if (!state.migrationSource) return;
    try {
      const data = await call("run_migration", { busy: true, busyText: "迁移旧贴图…" });
      state.draft = data.draft || state.draft;
      renderDraftList();
      await refreshSelectionComparison();
      $("#migration-analysis").textContent = migrationReportText(data.report, true);
      $("#run-migration").disabled = true;
      toast(`迁移完成：${data.report?.added || 0} 张已加入作品集`);
    } catch (_) {}
  }

  async function buildIndex() {
    try {
      await call("build_index", { busy: false });
      setBusy(true, "建索引…");
      toast("开始扫描资源包…");
    } catch (_) {
      setBusy(false);
    }
  }

  async function validateDynamic() {
    try {
      await call("validate_dynamic", { busy: false });
      setBusy(true, "校验动态资源…");
      toast("开始校验动态资源，请稍候…");
    } catch (_) {
      setBusy(false);
    }
  }

  async function exportSelection(variant) {
    if (!state.selection) return;
    try {
      const data = await call("export_selection", { busy: true, busyText: "导出中…" }, variant);
      if (!data) return;
      toast(`已导出：${data.path}`);
    } catch (_) {}
  }

  async function refreshResource() {
    if (!state.selection) return;
    const key = selectionKey();
    try {
      const sel = await call("refresh_selection", { busy: true, busyText: "刷新资源…" });
      if (key !== selectionKey() || !sel) return;
      await selectResource(sel.bundle, sel.name);
      toast("资源已刷新");
    } catch (_) {}
  }

  function canDropReplacement(selection = state.selection) {
    return canEdit(selection) && ["texture", "text", "anim"].includes(selection?.asset_type || selection?.kind);
  }

  function isFileDrag(event) {
    return Array.from(event.dataTransfer?.types || []).includes("Files");
  }

  function readReplacementFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        const separator = result.indexOf(";base64,");
        if (separator < 0) reject(new Error("无法读取文件，请重新拖入或点击选择文件。"));
        else resolve(result.slice(separator + 8));
      };
      reader.onerror = () => reject(new Error("文件读取失败，请确认文件仍在原位置且可以打开。"));
      reader.onabort = () => reject(new Error("文件读取已取消。"));
      reader.readAsDataURL(file);
    });
  }

  function applyReplacement(data) {
    state.replacement = data;
    if (data.text_content != null) {
      $("#studio-text").value = data.text_content;
      configureTextFields(state.selection);
      updateTextStatus();
    } else if (state.page === "studio") {
      renderReplacement();
    }
    syncStudioImportControls();
    toast(`已选择：${data.name}${data.encoding ? `（${data.encoding}）` : ""}`);
  }

  async function importDroppedReplacement(event) {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.stopPropagation();
    $("#studio-import-dropzone")?.classList.remove("is-dragover");
    if (state.page !== "studio" || !canDropReplacement()) {
      toast("请先选择可替换的图片、文本或动画片段；动图和视频请使用对应制作入口。", true);
      return;
    }
    if (state.busy || state.replacementImporting) {
      toast("正在处理文件，请稍候再拖入。", true);
      return;
    }
    const items = Array.from(event.dataTransfer.items || []).filter((item) => item.kind === "file");
    if (items.some((item) => item.webkitGetAsEntry?.()?.isDirectory)) {
      toast("暂不支持拖入文件夹，请拖入一个文件；序列帧可用“选择 PNG 帧文件夹”。", true);
      return;
    }
    const files = Array.from(event.dataTransfer.files || []);
    if (files.length !== 1 || items.length > 1) {
      toast("请一次只拖入一个替换文件。", true);
      return;
    }
    const file = files[0];
    if (!file.size || file.size > REPLACEMENT_FILE_LIMIT) {
      toast(file.size ? "拖入文件不能超过 12 MiB，请缩小文件后再试。" : "这个文件是空的，请重新选择。", true);
      return;
    }
    const key = selectionKey();
    const selectionRequest = state.selectionRequest;
    const request = ++state.replacementRequest;
    const expectedSelection = { bundle: state.selection.bundle, name: state.selection.name,
      asset_type: state.selection.asset_type || state.selection.kind };
    const current = () => request === state.replacementRequest && selectionRequest === state.selectionRequest &&
      key === selectionKey();
    state.replacementImporting = true;
    syncStudioImportControls();
    try {
      const content = await readReplacementFile(file);
      if (!current() || state.page !== "studio") return;
      const data = await call("import_replacement_file", { quiet: true }, file.name, content, expectedSelection);
      // 后端已接受新候选，切页也要同步；否则返回制作页会看见旧图却保存新图。
      if (!current() || !data) return;
      applyReplacement(data);
    } catch (err) {
      if (current()) toast(err.message || "导入失败，请重新选择文件。", true);
    } finally {
      if (request === state.replacementRequest) {
        state.replacementImporting = false;
        syncStudioImportControls();
      }
    }
  }

  function bindReplacementDrop() {
    const dropzone = $("#studio-import-dropzone");
    if (!dropzone) return;
    // 只拦截文件拖入，文本编辑器原有的文字拖放继续交给浏览器。
    document.addEventListener("dragover", (event) => { if (isFileDrag(event)) event.preventDefault(); });
    document.addEventListener("drop", (event) => { if (isFileDrag(event)) event.preventDefault(); });
    dropzone.addEventListener("dragover", (event) => {
      if (!isFileDrag(event)) return;
      event.preventDefault();
      const accepted = state.page === "studio" && canDropReplacement() && !state.busy && !state.replacementImporting;
      event.dataTransfer.dropEffect = accepted ? "copy" : "none";
      dropzone.classList.toggle("is-dragover", accepted);
    });
    dropzone.addEventListener("dragleave", (event) => {
      if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove("is-dragover");
    });
    dropzone.addEventListener("drop", importDroppedReplacement);
  }

  async function chooseReplacement(sequenceFolder = false) {
    const key = selectionKey();
    if (!canEdit(state.selection) || state.replacementImporting) return;
    const selectionRequest = state.selectionRequest;
    const request = ++state.replacementRequest;
    const clip = (state.selection.asset_type || state.selection.kind) === "anim";
    if (clip) {
      state.replacement = null;
      syncStudioImportControls();
      if (state.page === "studio") renderReplacement();
    }
    state.replacementImporting = true;
    syncStudioImportControls();
    try {
      const data = await call("choose_replacement", { busy: true, busyText: "读取替换文件…" }, sequenceFolder);
      if (!data || request !== state.replacementRequest || selectionRequest !== state.selectionRequest || key !== selectionKey()) return;
      applyReplacement(data);
    } catch (err) {
      if (clip && request === state.replacementRequest && selectionRequest === state.selectionRequest && key === selectionKey()) {
        state.replacement = null;
        syncStudioImportControls();
        if (state.page === "studio") renderAnimationState($("#studio-replacement"), `未导入替换：${err.message}`);
      }
    } finally {
      if (request === state.replacementRequest) {
        state.replacementImporting = false;
        syncStudioImportControls();
      }
    }
  }

  // ---------- 裁剪弹窗 ----------
  function openCropModal({ url, targetW, targetH, onConfirm, isCurrent = () => true, onCancel = null }) {
    const modal = $("#crop-modal");
    const img = $("#crop-image");
    if (!modal || !img) return;
    closeCropModal();
    const pending = { onConfirm, isCurrent, onCancel, loader: new Image() };
    cropPending = pending;
    modal.classList.remove("is-hidden");
    $("#crop-confirm").disabled = true;
    $("#crop-reset").disabled = true;
    $("#crop-zoom").disabled = true;
    $("#crop-info").textContent = "正在读取图片…";
    pending.loader.onload = () => {
      if (cropPending !== pending || !isCropCurrent()) return;
      const natW = pending.loader.naturalWidth;
      const natH = pending.loader.naturalHeight;
      if (!(natW > 0 && natH > 0)) return pending.loader.onerror();
      cropState = {
        natW, natH,
        targetW: Number(targetW) > 0 ? Number(targetW) : 0,
        targetH: Number(targetH) > 0 ? Number(targetH) : 0,
        zoom: 1,
      };
      img.src = url;
      resizeCropStage();
      $("#crop-confirm").disabled = false;
      $("#crop-reset").disabled = false;
      $("#crop-stage").focus({ preventScroll: true });
    };
    pending.loader.onerror = () => {
      if (cropPending !== pending) return;
      closeCropModal();
      toast("图片读取失败，请重新选择图片后再裁剪", true);
    };
    pending.loader.src = url;
  }

  function closeCropModal() {
    cropRequest += 1;
    const pending = cropPending;
    if (!pending && !cropState && !cropDrag) return;
    cropPending = null;
    if (pending?.loader) {
      pending.loader.onload = null;
      pending.loader.onerror = null;
    }
    $("#crop-modal")?.classList.add("is-hidden");
    $("#crop-image")?.removeAttribute("src");
    $("#crop-box")?.classList.add("is-hidden");
    cropState = null;
    onCropPointerUp();
    if (pending?.onCancel) Promise.resolve(pending.onCancel()).catch(() => {});
  }

  function isCropCurrent() {
    if (!cropPending) return false;
    if (cropPending.isCurrent()) return true;
    closeCropModal();
    return false;
  }

  function cropFrameGeometry(stageW, stageH, ratio) {
    const availableW = Math.max(1, stageW - 56);
    const availableH = Math.max(1, stageH - 56);
    const width = Math.min(availableW, availableH * ratio);
    const height = width / ratio;
    return { left: (stageW - width) / 2, top: (stageH - height) / 2, width, height };
  }

  function clampCropImage(geometry) {
    const { frame, scale, natW, natH } = geometry;
    geometry.imageX = Math.min(frame.left, Math.max(frame.left + frame.width - natW * scale, geometry.imageX));
    geometry.imageY = Math.min(frame.top, Math.max(frame.top + frame.height - natH * scale, geometry.imageY));
  }

  function cropBoxInPixels(geometry) {
    const { frame, scale, imageX, imageY, natW, natH } = geometry;
    const width = Math.max(1, Math.min(natW, Math.round(frame.width / scale)));
    const height = Math.max(1, Math.min(natH, Math.round(frame.height / scale)));
    const left = Math.max(0, Math.min(natW - width, Math.round((frame.left - imageX) / scale)));
    const top = Math.max(0, Math.min(natH - height, Math.round((frame.top - imageY) / scale)));
    return [left, top, left + width, top + height];
  }

  function resizeCropStage() {
    if (!cropState || !isCropCurrent()) return;
    onCropPointerUp();
    const geometry = cropState;
    const oldFrame = geometry.frame;
    const centerX = oldFrame ? (oldFrame.left + oldFrame.width / 2 - geometry.imageX) / geometry.scale : geometry.natW / 2;
    const centerY = oldFrame ? (oldFrame.top + oldFrame.height / 2 - geometry.imageY) / geometry.scale : geometry.natH / 2;
    const stage = $("#crop-stage");
    const ratio = geometry.targetW && geometry.targetH ? geometry.targetW / geometry.targetH : geometry.natW / geometry.natH;
    geometry.frame = cropFrameGeometry(stage.clientWidth, stage.clientHeight, ratio);
    geometry.baseScale = Math.max(geometry.frame.width / geometry.natW, geometry.frame.height / geometry.natH);
    const shortestCropSide = Math.min(geometry.frame.width, geometry.frame.height) / geometry.baseScale;
    geometry.maxZoom = Math.max(1, Math.min(8, Math.floor(shortestCropSide * 100) / 100));
    geometry.zoom = Math.min(geometry.maxZoom, geometry.zoom);
    geometry.scale = geometry.baseScale * geometry.zoom;
    geometry.imageX = geometry.frame.left + geometry.frame.width / 2 - centerX * geometry.scale;
    geometry.imageY = geometry.frame.top + geometry.frame.height / 2 - centerY * geometry.scale;
    renderCropBox();
  }

  function renderCropBox() {
    if (!cropState) return;
    clampCropImage(cropState);
    const { frame, imageX, imageY, scale, natW, natH, zoom } = cropState;
    const img = $("#crop-image");
    img.style.width = natW * scale + "px";
    img.style.height = natH * scale + "px";
    img.style.left = imageX + "px";
    img.style.top = imageY + "px";
    const box = $("#crop-box");
    box.style.left = frame.left + "px";
    box.style.top = frame.top + "px";
    box.style.width = frame.width + "px";
    box.style.height = frame.height + "px";
    box.classList.remove("is-hidden");
    $("#crop-zoom").max = String(Math.round(cropState.maxZoom * 100));
    $("#crop-zoom").disabled = cropState.maxZoom <= 1;
    $("#crop-zoom").value = String(Math.round(zoom * 100));
    $("#crop-zoom-value").textContent = `${Math.round(zoom * 100)}%`;
    const [l, t, r, b] = cropBoxInPixels(cropState);
    const output = cropState.targetW && cropState.targetH ? ` → 游戏原尺寸 ${cropState.targetW}×${cropState.targetH}` : " · 保持原图比例";
    const pixelHint = Math.min(r - l, b - t) < 8 ? " · 像素较少，边界按整像素取整" : "";
    $("#crop-info").textContent = `裁切区域 ${r - l}×${b - t}${output}${pixelHint}`;
  }

  function resetCropFull() {
    if (!cropState || !isCropCurrent()) return;
    onCropPointerUp();
    cropState.zoom = 1;
    cropState.frame = null;
    resizeCropStage();
  }

  function setCropZoom(zoom, focusX, focusY) {
    if (!cropState || !isCropCurrent() || !Number.isFinite(zoom)) return;
    onCropPointerUp();
    const geometry = cropState;
    const { frame } = geometry;
    const x = focusX ?? frame.left + frame.width / 2;
    const y = focusY ?? frame.top + frame.height / 2;
    const sourceX = (x - geometry.imageX) / geometry.scale;
    const sourceY = (y - geometry.imageY) / geometry.scale;
    geometry.zoom = Math.max(1, Math.min(geometry.maxZoom, zoom));
    geometry.scale = geometry.baseScale * geometry.zoom;
    geometry.imageX = x - sourceX * geometry.scale;
    geometry.imageY = y - sourceY * geometry.scale;
    renderCropBox();
  }

  function onCropWheel(e) {
    if (!cropState || !isCropCurrent()) return;
    e.preventDefault();
    const stage = $("#crop-stage").getBoundingClientRect();
    const { frame } = cropState;
    const x = Math.max(frame.left, Math.min(frame.left + frame.width, e.clientX - stage.left));
    const y = Math.max(frame.top, Math.min(frame.top + frame.height, e.clientY - stage.top));
    setCropZoom(cropState.zoom * Math.exp(-e.deltaY * 0.0015), x, y);
  }

  function startCropDrag(e) {
    if (!cropState || !isCropCurrent() || (e.button != null && e.button !== 0) || e.isPrimary === false) return;
    e.preventDefault();
    onCropPointerUp();
    cropDrag = { pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, imageX: cropState.imageX, imageY: cropState.imageY };
    $("#crop-stage").classList.add("is-dragging");
    document.addEventListener("pointermove", onCropPointerMove);
    document.addEventListener("pointerup", onCropPointerUp);
    document.addEventListener("pointercancel", onCropPointerUp);
    window.addEventListener("blur", onCropPointerUp);
  }

  function onCropPointerMove(e) {
    if (!cropDrag || !cropState || e.pointerId !== cropDrag.pointerId || !isCropCurrent()) return;
    cropState.imageX = cropDrag.imageX + e.clientX - cropDrag.startX;
    cropState.imageY = cropDrag.imageY + e.clientY - cropDrag.startY;
    renderCropBox();
  }

  function onCropPointerUp() {
    cropDrag = null;
    $("#crop-stage")?.classList.remove("is-dragging");
    document.removeEventListener("pointermove", onCropPointerMove);
    document.removeEventListener("pointerup", onCropPointerUp);
    document.removeEventListener("pointercancel", onCropPointerUp);
    window.removeEventListener("blur", onCropPointerUp);
  }

  async function cropReplacement() {
    if ((state.selection?.asset_type || state.selection?.kind) === "anim") {
      toast("动画图集不能裁剪，否则会破坏原来的碎片位置", true);
      return;
    }
    if (!state.replacement) {
      toast("请先选择替换文件", true);
      return;
    }
    if (/\.(animbin|bin)$/i.test(state.replacement.name || "")) {
      toast("动画字节文件不能裁剪", true);
      return;
    }
    if (!state.replacement.preview_data) {
      toast("这个文件没有图片预览，无法裁剪", true);
      return;
    }
    const sel = state.selection;
    const replacement = state.replacement;
    const selectionRequest = state.selectionRequest;
    const studioRequest = state.studioRequest;
    const replacementRequest = state.replacementRequest;
    const page = state.page;
    const key = selectionKey();
    const sameCandidate = () => selectionRequest === state.selectionRequest && replacementRequest === state.replacementRequest &&
      key === selectionKey() && state.replacement === replacement;
    const current = () => state.page === page && studioRequest === state.studioRequest && sameCandidate();
    openCropModal({
      url: replacement.preview_data,
      targetW: sel?.width || 0,
      targetH: sel?.height || 0,
      isCurrent: current,
      onConfirm: async (box) => {
        if (!current()) return;
        const data = await call("crop_replacement", { busy: true, busyText: "裁剪中…" }, box, replacement.crop_token);
        if (!sameCandidate()) return;
        state.replacement = { ...replacement, ...data };
        if (state.page === "studio") renderReplacement();
        toast(`已裁剪为 ${box[2] - box[0]}×${box[3] - box[1]}`);
      },
    });
  }

  async function cropReplaceDraft() {
    if (state.draftIndex < 0) return;
    if (state.draft.items?.[state.draftIndex]?.kind !== "texture") return;
    if (state.draft.items?.[state.draftIndex]?.from_anim) {
      toast("动画图集不能裁剪，否则会破坏原来的碎片位置", true);
      return;
    }
    closeCropModal();
    const request = cropRequest;
    const index = state.draftIndex;
    const item = state.draft.items[index];
    const detailRequest = state.draftDetailRequest;
    const sameDetail = () => state.page === "pack" && index === state.draftIndex && detailRequest === state.draftDetailRequest;
    const current = () => sameDetail() && item === state.draft.items?.[index];
    try {
      const data = await call("pick_draft_crop_source", { busy: false }, index);
      if (!data) return;
      const cancel = () => call("cancel_draft_crop", { quiet: true }, data.crop_token);
      if (request !== cropRequest || !current()) {
        await cancel();
        return;
      }
      openCropModal({
        url: data.preview_data,
        targetW: data.target_width,
        targetH: data.target_height,
        isCurrent: current,
        onCancel: cancel,
        onConfirm: async (box) => {
          if (!current()) return;
          const res = await call("commit_draft_crop", { busy: true, busyText: "裁剪并保存中…" }, box, data.crop_token);
          state.draft = res.draft;
          state.installed = res.installed || state.installed;
          state.dashboard = res.dashboard || state.dashboard;
          renderInstalled();
          renderDashboard();
          renderDraftList();
          toast("裁剪换图已保存到作品集");
          await refreshSelectionComparison();
          if (sameDetail()) await showDraftDetail(index);
        },
      });
    } catch (_) {}
  }

  async function commitReplacement() {
    const kind = state.selection?.asset_type || state.selection?.kind;
    if (!canEdit(state.selection)) {
      toast(state.selection?.reason || "此类资源暂不支持替换", true);
      return;
    }
    if (state.replacementImporting) return;
    if (kind !== "text" && !state.replacement) {
      toast("请先选择替换文件", true);
      return;
    }
    const key = selectionKey();
    const text = kind === "text" ? $("#studio-text").value : "";
    try {
      const data = await call("commit_replacement", { busy: true, busyText: "写入作品集…" }, text, previewFps());
      state.draft = data.draft;
      if (key === selectionKey()) {
        state.replacement = null;
        $("#crop-replacement").disabled = true;
        if (state.page === "studio") await refreshStudio();
        else await refreshSelectionComparison();
      }
      toast("已保存到作品集，可继续预览或手动安装测试");
      renderDraftList();
    } catch (err) {
      if (kind === "anim" && key === selectionKey()) {
        state.replacement = null;
        syncStudioImportControls();
        if (state.page === "studio") renderAnimationState($("#studio-replacement"), `保存未通过，请重新选择文件：${err.message}`);
      }
    }
  }

  // 后端推送事件
  window.handleBackendEvent = function handleBackendEvent(packet) {
    const event = packet?.event;
    const payload = packet?.payload || {};
    if (event === "log" && payload.line) {
      appendLogLine(payload.line);
    } else if (event === "index_progress") {
      setBusy(true, `建索引… ${payload.done || 0}/${payload.total || "?"}`);
    } else if (event === "index_done") {
      setBusy(false);
      const c = payload.counts || {};
      toast(
        `索引完成：${payload.bundles || 0} 包 · 贴图${c.texture || 0}/文本${c.text || 0}/模型${c.mesh || 0}/动画${c.anim || 0}`
      );
      if (state.page === "browse") refreshBrowse();
      call("bootstrap", { quiet: true })
        .then((boot) => {
          if (boot?.dashboard) {
            state.dashboard = boot.dashboard;
            renderDashboard();
          }
        })
        .catch(() => {});
    } else if (event === "dynamic_validate_progress") {
      setBusy(true, `校验动态资源… ${payload.done || 0}/${payload.total || "?"}`);
    } else if (event === "dynamic_validate_done") {
      setBusy(false);
      if (payload.error) {
        toast(payload.error, true);
      } else {
        toast(`动态资源校验完成：有效 ${payload.valid || 0}/${payload.total || 0}，已隐藏 ${payload.invalid || 0} 个无效资源`);
      }
      if (state.page === "browse") refreshBrowse();
    }
  };

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function ensureGameConnected() {
    // 冷启动有时第一帧检测为空，自动多探几次，免得点「刷新检测」
    if (state.dashboard?.has_game) return;
    for (let i = 0; i < 4; i++) {
      await sleep(350 + i * 200);
      try {
        const dash = await call("refresh_detection", { quiet: true });
        state.dashboard = dash;
        if (dash?.has_game) {
          renderDashboard();
          return;
        }
      } catch (_) {}
    }
    renderDashboard();
  }

  async function bootstrap() {
    setHeaderStatus("连接后端…");
    const boot = await call("bootstrap", { quiet: false });
    window.ProjectUpdates?.init({ api, toast, appInfo: boot.app_info || {} });
    state.dashboard = boot.dashboard;
    state.installed = boot.installed || [];
    state.draft = boot.draft || state.draft;
    state.assetTypes = boot.assetTypes || [];
    (boot.logs || []).forEach(appendLogLine);
    fillAssetTypes();
    renderDashboard();
    renderInstalled();
    renderDraftList();
    $("#loading-overlay")?.classList.add("is-hidden");
    $("#app")?.classList.remove("is-loading");
    showPage("dashboard");

    if (!state.dashboard?.has_game) {
      setHeaderStatus("正在检测游戏…", false);
      await ensureGameConnected();
    }
    setHeaderStatus(
      state.dashboard?.has_game ? "游戏已连接" : "未检测到游戏",
      !state.dashboard?.has_game
    );
  }

  // 后端事件改成轮询 /poll 拉取（不再依赖 pywebview 推送）
  let eventCursor = 0;
  async function pollEvents() {
    try {
      const resp = await fetch("/poll?since=" + eventCursor, { headers: API_HEADERS });
      const data = await resp.json();
      eventCursor = data.cursor ?? eventCursor;
      (data.events || []).forEach((e) => window.handleBackendEvent(e));
    } catch (_) {}
    setTimeout(pollEvents, 700);
  }

  async function waitForBackend() {
    // 本地服务先于页面就绪；这里轻探几次，失败也继续（bootstrap 自带重试）
    for (let i = 0; i < 40; i++) {
      try {
        const r = await fetch("/poll?since=0", { headers: API_HEADERS });
        if (r.ok) return;
      } catch (_) {}
      await sleep(100);
    }
  }

  async function start() {
    bindEvents();
    await waitForBackend();
    pollEvents();
    await sleep(30);
    let lastErr = null;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        await bootstrap();
        return;
      } catch (err) {
        lastErr = err;
        await sleep(200 * (attempt + 1));
      }
    }
    $("#loading-overlay")?.classList.add("is-hidden");
    $("#app")?.classList.remove("is-loading");
    setHeaderStatus("后端连接失败", true);
    toast(lastErr?.message || "启动失败", true);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
