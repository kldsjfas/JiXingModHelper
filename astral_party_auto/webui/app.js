/* 吉星派对 Mod 助手 — HTML 前端，通过本地 HTTP API 调用 Python 后端 */
(() => {
  "use strict";

  const PAGE_SIZE = 25;
  const API_TOKEN = new URLSearchParams(window.location.hash.slice(1)).get("token") || "";
  const API_HEADERS = { "X-JiXing-Token": API_TOKEN };
  const PAGE_TITLES = {
    dashboard: "仪表盘",
    manage: "Mod 管理",
    browse: "浏览资源",
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
    query: "",
    characterFilter: "",
    characterList: [],
    characterLabels: { bundles: {}, resources: {} },
    selection: null,
    selectionRequest: 0,
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

  // 裁剪弹窗状态：cropState 保存图片几何信息 + 当前框；cropPending 保存确认后的回调
  let cropState = null;
  let cropPending = null;
  let cropDrag = null;

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
    if (page === "browse") { refreshBrowse(); renderPreview(); }
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
    fillAssetTypes();
    try {
      const [cats, charList, labels] = await Promise.all([
        call("get_categories", { quiet: true }, state.assetType),
        call("get_character_list", { quiet: true }),
        call("get_character_labels", { quiet: true }),
      ]);
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
      if (seq !== state.browseRequestSeq) return;
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
      if (seq !== state.browseRequestSeq) return;
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
    rememberTextEdit();
    const request = ++state.selectionRequest;
    state.studioRequest += 1;
    stopAllAnimations();
    closeLightbox();
    state.replacement = null;
    state.previewAnimation = null;
    state.previewLoading = false;
    state.previewError = "";
    state.originalAnimation = null;
    state.sequenceFrames = [];
    try {
      const sel = await call("select_asset", { busy: true, busyText: "加载预览…" }, state.assetType, bundle, name);
      if (request !== state.selectionRequest) return;
      state.selection = sel;
      state.previewLoading = !!isPlayableAnimation(sel);
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
      if (isPlayableAnimation(sel)) await loadBrowseAnimation(sel, request);
    } catch (_) {}
  }

  async function loadBrowseAnimation(selection, selectionRequest = state.selectionRequest) {
    const request = ++state.previewRequest;
    const key = selectionKey(selection);
    state.previewLoading = true;
    state.previewError = "";
    if (state.page === "browse") renderPreview();
    try {
      const animation = await call("get_animation_preview", { quiet: true }, selection.bundle, selection.name, "original", previewFps());
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
    if (title) title.textContent = sel.caption || sel.name || "资源";
    if (desc) desc.textContent = sel.reason || sel.category_desc || "";
    if (media) {
      if (isPlayableAnimation(sel)) {
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
      if (sel.frame_names && sel.frame_names.length) {
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

  function isPlayableAnimation(selection) {
    if (!selection || selection.playable === false) return false;
    const kind = selection.asset_type || selection.kind;
    if (kind === "anim") return selection.playable === true || selection.animation_kind === "sprite_clip";
    return kind === "dynamic" &&
      (["sequence", "gif", "webp", "apng"].includes(selection.animation_kind) || !!selection.frame_names?.length);
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
    state.selection = sel;
    empty.classList.toggle("is-hidden", !!sel);
    content.classList.toggle("is-hidden", !sel);
    if (!sel) return;
    const key = selectionKey(sel);
    $("#studio-title").textContent = sel.caption || sel.name || "";
    $("#studio-description").textContent = sel.category_desc || "";
    const kind = sel.asset_type || sel.kind || "texture";
    const editable = canEdit(sel);
    const animated = isPlayableAnimation(sel);
    const sequence = isSequenceAnimation(sel);
    $("#studio-image-mode").classList.toggle("is-hidden", kind === "text");
    $("#studio-text-mode").classList.toggle("is-hidden", kind !== "text");
    $("#choose-replacement").classList.toggle("is-hidden", !editable);
    $("#choose-frame-folder").classList.toggle("is-hidden", !editable || !sequence);
    $("#choose-replacement").textContent = kind === "text" ? "导入文本文件…" : kind === "anim" ? "选择动画数据 / 图集…" : animated ? "选择替换动画…" : "选择替换文件";
    const cropBtn = $("#crop-replacement");
    cropBtn.classList.toggle("is-hidden", !["texture", "anim"].includes(kind) || !editable);
    cropBtn.disabled = !state.replacement?.preview_data || /\.(animbin|bin)$/i.test(state.replacement?.name || "");
    $("#commit-replacement").disabled = !editable;
    $("#animation-settings").classList.toggle("is-hidden", !animated);
    $("#sequence-fps-label").classList.toggle("is-hidden", !sequence);
    $("#animation-guidance").textContent = kind === "anim"
      ? "按动画片段中的 Sprite 引用顺序和时间播放，可暂停、逐帧查看和调节播放倍速。"
      : !editable ? "当前资源可逐帧查看。替换能力和限制见下方说明。" : sequence
      ? "导入 GIF、WebP 或 APNG 后按原序列帧数量和尺寸适配。此处帧率用于预览，游戏内速度由游戏控制。"
      : "保留动画图片的逐帧时长，可逐帧检查替换前后效果。";
    const animationNotice = kind === "anim" ? ".animbin 仅接受同源兼容动画片段，用于改动作数据；图片只替换关联图集，动作顺序和时间不变。" : "";
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
    if (animated) {
      if (!state.replacement) renderAnimationState($("#studio-replacement"), "正在加载作品集动画…");
      const [original, draft] = await Promise.allSettled([
        call("get_animation_preview", { quiet: true }, sel.bundle, sel.name, "original", previewFps()),
        state.replacement?.animation ? Promise.resolve(state.replacement.animation) :
          call("get_animation_preview", { quiet: true }, sel.bundle, sel.name, "draft", previewFps()),
      ]);
      if (request !== state.studioRequest || key !== selectionKey() || state.page !== "studio") return;
      if (original.status === "fulfilled") {
        state.originalAnimation = original.value;
        renderAnimation($("#studio-original"), original.value, sequence);
      } else {
        renderAnimationState($("#studio-original"), `动画预览未加载：${original.reason.message}`, refreshStudio);
      }
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
    if (replacement?.animation) {
      renderAnimation(media, replacement.animation, isSequenceAnimation(state.selection));
    } else {
      setMedia(media, replacement?.preview_data, replacement?.name || "选择文件后可在这里对照预览");
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
      button.textContent = "重新加载动画";
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
      const kind = item.kind === "texture" ? "图" : item.kind === "text" ? "文" : ["anim", "dynamic"].includes(item.kind) ? "动" : item.kind || "?";
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
      $("#draft-replace").disabled = true;
      $("#draft-crop").disabled = true;
      $("#draft-remove").disabled = true;
    }
  }

  async function showDraftDetail(index) {
    const request = ++state.draftDetailRequest;
    setMedia($("#draft-original"), null, "正在加载原内容…");
    setMedia($("#draft-modified"), null, "正在加载修改内容…");
    state.draftIndex = index;
    renderDraftList();
    try {
      const data = await call("get_draft_detail", { quiet: true }, index);
      if (request !== state.draftDetailRequest || state.page !== "pack") return;
      const item = data.item || {};
      const kindLabel = { texture: "图片", text: "文字", anim: "动画片段", dynamic: "动画" }[item.kind] || "资源";
      $("#draft-detail-title").textContent = `${kindLabel} · ${item.name || ""}`;
      setMedia($("#draft-original"), data.original_data, "无预览");
      setMedia($("#draft-modified"), data.modified_data, "无预览");
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
      $("#draft-crop").disabled = item.kind !== "texture";
      $("#draft-remove").disabled = false;
    } catch (err) {
      if (request === state.draftDetailRequest && state.page === "pack") {
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

    $("#crop-smart")?.addEventListener("click", smartCropBox);
    $("#crop-reset")?.addEventListener("click", resetCropFull);
    $("#crop-cancel")?.addEventListener("click", closeCropModal);
    $("#crop-confirm")?.addEventListener("click", async () => {
      if (!cropState || !cropPending) return;
      const box = cropState.box.map((v) => Math.round(v));
      const onConfirm = cropPending.onConfirm;
      closeCropModal();
      try {
        await onConfirm(box);
      } catch (_) {}
    });
    $("#crop-box")?.addEventListener("pointerdown", (e) => {
      if (!cropState || e.target.closest(".crop-handle")) return;
      startCropDrag("move", e);
    });
    $$(".crop-handle").forEach((handle) => {
      handle.addEventListener("pointerdown", (e) => {
        e.stopPropagation();
        startCropDrag(handle.dataset.handle, e);
      });
    });

    $("#draft-name")?.addEventListener("change", async (e) => {
      try {
        state.draft = await call("set_draft_name", { quiet: true }, e.target.value);
        renderDraftList();
      } catch (_) {}
    });
    $("#draft-replace")?.addEventListener("click", async () => {
      if (state.draftIndex < 0) return;
      try {
        const data = await call("replace_draft_image", { busy: true, busyText: "换图并保存中…" }, state.draftIndex);
        if (!data) return;
        state.draft = data.draft;
        state.installed = data.installed || state.installed;
        state.dashboard = data.dashboard || state.dashboard;
        renderInstalled();
        renderDashboard();
        toast("换图已保存到作品集");
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

  async function chooseReplacement(sequenceFolder = false) {
    const key = selectionKey();
    if (!canEdit(state.selection)) return;
    try {
      const data = await call("choose_replacement", { busy: true, busyText: "读取替换文件…" }, sequenceFolder);
      if (!data || key !== selectionKey()) return;
      state.replacement = data;
      if (data.text_content != null) {
        $("#studio-text").value = data.text_content;
        configureTextFields(state.selection);
        updateTextStatus();
      } else if (state.page === "studio") {
        renderReplacement();
      }
      const cropBtn = $("#crop-replacement");
      if (cropBtn) cropBtn.disabled = !data.preview_data || /\.(animbin|bin)$/i.test(data.name || "") ||
        (!!data.animation && (state.selection?.asset_type || state.selection?.kind) !== "anim");
      toast(`已选择：${data.name}${data.encoding ? `（${data.encoding}）` : ""}`);
    } catch (_) {}
  }

  // ---------- 裁剪弹窗 ----------
  function openCropModal({ url, targetW, targetH, onConfirm }) {
    const modal = $("#crop-modal");
    const img = $("#crop-image");
    if (!modal || !img) return;
    cropPending = { onConfirm };
    cropState = null;
    modal.classList.remove("is-hidden");
    const applyGeometry = () => {
      const stage = $("#crop-stage");
      const stageRect = stage.getBoundingClientRect();
      const imgRect = img.getBoundingClientRect();
      cropState = {
        natW: img.naturalWidth,
        natH: img.naturalHeight,
        scale: imgRect.width / (img.naturalWidth || 1),
        offsetX: imgRect.left - stageRect.left,
        offsetY: imgRect.top - stageRect.top,
        box: [0, 0, img.naturalWidth, img.naturalHeight],
        targetW: targetW || 0,
        targetH: targetH || 0,
      };
      smartCropBox();
    };
    img.onload = applyGeometry;
    if (img.src === url && img.complete && img.naturalWidth) {
      // 同一张图重新打开：src 不变不会再触发 onload，等布局刷新后手动跑一次
      requestAnimationFrame(applyGeometry);
    } else {
      img.src = url;
    }
  }

  function closeCropModal() {
    $("#crop-modal")?.classList.add("is-hidden");
    cropState = null;
    cropPending = null;
    cropDrag = null;
  }

  function renderCropBox() {
    if (!cropState) return;
    const boxEl = $("#crop-box");
    if (!boxEl) return;
    const [l, t, r, b] = cropState.box;
    const s = cropState.scale;
    boxEl.style.left = cropState.offsetX + l * s + "px";
    boxEl.style.top = cropState.offsetY + t * s + "px";
    boxEl.style.width = (r - l) * s + "px";
    boxEl.style.height = (b - t) * s + "px";
  }

  function updateCropInfo() {
    const info = $("#crop-info");
    if (!info || !cropState) return;
    const [l, t, r, b] = cropState.box;
    const cw = r - l;
    const ch = b - t;
    if (cropState.targetW > 0 && cropState.targetH > 0) {
      info.textContent = `裁切区 ${cw}×${ch}  →  输出为游戏原尺寸 ${cropState.targetW}×${cropState.targetH}`;
    } else {
      info.textContent = `原图 ${cropState.natW}×${cropState.natH}  →  裁切 ${cw}×${ch}`;
    }
  }

  function smartCropBox() {
    if (!cropState) return;
    const { natW, natH, targetW, targetH } = cropState;
    let box;
    if (targetW > 0 && targetH > 0) {
      const srcRatio = natW / natH;
      const tgtRatio = targetW / targetH;
      let nw, nh;
      if (srcRatio > tgtRatio) {
        nh = natH;
        nw = Math.max(1, Math.round(natH * tgtRatio));
      } else {
        nw = natW;
        nh = Math.max(1, Math.round(natW / tgtRatio));
      }
      const l = Math.max(0, Math.floor((natW - nw) / 2));
      const t = Math.max(0, Math.floor((natH - nh) / 2));
      box = [l, t, Math.min(natW, l + nw), Math.min(natH, t + nh)];
    } else {
      const side = Math.min(natW, natH);
      const l = Math.floor((natW - side) / 2);
      const t = Math.floor((natH - side) / 2);
      box = [l, t, l + side, t + side];
    }
    cropState.box = box;
    renderCropBox();
    updateCropInfo();
  }

  function resetCropFull() {
    if (!cropState) return;
    cropState.box = [0, 0, cropState.natW, cropState.natH];
    renderCropBox();
    updateCropInfo();
  }

  function startCropDrag(mode, e) {
    if (!cropState) return;
    e.preventDefault();
    cropDrag = { mode, startX: e.clientX, startY: e.clientY, startBox: cropState.box.slice() };
    document.addEventListener("pointermove", onCropPointerMove);
    document.addEventListener("pointerup", onCropPointerUp);
  }

  function onCropPointerMove(e) {
    if (!cropDrag || !cropState) return;
    const scale = cropState.scale || 1;
    const dxN = (e.clientX - cropDrag.startX) / scale;
    const dyN = (e.clientY - cropDrag.startY) / scale;
    const natW = cropState.natW;
    const natH = cropState.natH;
    const MIN = 8;
    let [l, t, r, b] = cropDrag.startBox;
    if (cropDrag.mode === "move") {
      const w = r - l;
      const h = b - t;
      l += dxN;
      r = l + w;
      t += dyN;
      b = t + h;
      if (l < 0) { r -= l; l = 0; }
      if (t < 0) { b -= t; t = 0; }
      if (r > natW) { l -= r - natW; r = natW; }
      if (b > natH) { t -= b - natH; b = natH; }
    } else {
      if (cropDrag.mode.includes("w")) l = Math.min(r - MIN, Math.max(0, l + dxN));
      if (cropDrag.mode.includes("e")) r = Math.max(l + MIN, Math.min(natW, r + dxN));
      if (cropDrag.mode.includes("n")) t = Math.min(b - MIN, Math.max(0, t + dyN));
      if (cropDrag.mode.includes("s")) b = Math.max(t + MIN, Math.min(natH, b + dyN));
    }
    cropState.box = [Math.round(l), Math.round(t), Math.round(r), Math.round(b)];
    renderCropBox();
    updateCropInfo();
  }

  function onCropPointerUp() {
    cropDrag = null;
    document.removeEventListener("pointermove", onCropPointerMove);
    document.removeEventListener("pointerup", onCropPointerUp);
  }

  async function cropReplacement() {
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
    openCropModal({
      url: state.replacement.preview_data,
      targetW: sel?.width || 0,
      targetH: sel?.height || 0,
      onConfirm: async (box) => {
        const data = await call("crop_replacement", { busy: true, busyText: "裁剪中…" }, box);
        state.replacement = { ...state.replacement, ...data };
        renderReplacement();
        toast(`已裁剪为 ${box[2] - box[0]}×${box[3] - box[1]}`);
      },
    });
  }

  async function cropReplaceDraft() {
    if (state.draftIndex < 0) return;
    try {
      const data = await call("pick_draft_crop_source", { busy: false }, state.draftIndex);
      if (!data) return;
      openCropModal({
        url: data.preview_data,
        targetW: data.target_width,
        targetH: data.target_height,
        onConfirm: async (box) => {
          const res = await call("commit_draft_crop", { busy: true, busyText: "裁剪并保存中…" }, box);
          state.draft = res.draft;
          state.installed = res.installed || state.installed;
          state.dashboard = res.dashboard || state.dashboard;
          renderInstalled();
          renderDashboard();
          toast("裁剪换图已保存到作品集");
          await showDraftDetail(state.draftIndex);
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
      }
      toast("已保存到作品集，可继续预览或手动安装测试");
      renderDraftList();
    } catch (_) {}
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
