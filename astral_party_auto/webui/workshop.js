/* 工坊只展示后端验证的审核目录；下载、检查与安装分别由用户发起。 */
(() => {
  "use strict";

  const CATEGORY_LABELS = {
    texture: "贴图", text: "文字", animation: "动画", emote: "表情", audio: "音频", mixed: "综合",
  };
  const $ = (id) => document.getElementById(id);
  const state = {
    initialized: false, loaded: false, working: false, catalogLoading: false,
    items: [], selected: null, prepared: null, submission: null,
    selectionVersion: 0, catalogVersion: 0,
  };
  let bridge;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }

  function message(id, text, error = false) {
    const element = $(id);
    element.textContent = text || "";
    element.classList.toggle("is-error", error);
  }

  function count(value) {
    return Array.isArray(value) ? value.length : Math.max(0, Number(value) || 0);
  }

  function formatSize(value) {
    const size = Math.max(0, Number(value) || 0);
    if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
    if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${size} B`;
  }

  function sameEntry(first, second) {
    return Boolean(first && second && first.id === second.id && entryHash(first) === entryHash(second));
  }

  function entryHash(entry) {
    return entry?.sha256 || entry?.asset?.sha256 || "";
  }

  function entrySize(entry) {
    return entry?.size ?? entry?.asset?.size ?? 0;
  }

  function canInstall() {
    return sameEntry(state.selected, state.prepared?.entry)
      && state.prepared?.analysis?.can_install === true;
  }

  function syncButtons() {
    const locked = state.working || state.catalogLoading;
    $("workshop-refresh").disabled = locked;
    $("workshop-prepare").disabled = !state.selected || locked;
    $("workshop-install").disabled = !canInstall() || locked;
    $("workshop-install-accept").disabled = !canInstall() || locked;
    $("workshop-install-cancel").disabled = locked;
    $("workshop-report").disabled = !state.selected || locked;
    $("workshop-choose-submission").disabled = locked;
    $("workshop-export-submission").disabled = !state.submission || locked;
    $("workshop-open-submission").disabled = locked;
    $("workshop-repository").disabled = locked;
    $("workshop-submit-form").querySelectorAll("input, select, textarea").forEach((input) => {
      input.disabled = locked;
    });
    $("workshop-results").querySelectorAll("button").forEach((button) => {
      button.disabled = locked;
    });
  }

  function hideConfirmation() {
    $("workshop-install-confirm").classList.add("is-hidden");
    $("workshop-install").setAttribute("aria-expanded", "false");
  }

  function showDetail(entry) {
    state.selected = entry;
    state.prepared = null;
    state.selectionVersion += 1;
    hideConfirmation();
    $("workshop-detail-empty").classList.toggle("is-hidden", Boolean(entry));
    $("workshop-detail-content").classList.toggle("is-hidden", !entry);
    if (entry) {
      $("workshop-detail-title").textContent = entry.name || "未命名作品";
      $("workshop-detail-author").textContent = entry.author || "未填写作者";
      $("workshop-detail-category").textContent = CATEGORY_LABELS[entry.category] || "其他";
      $("workshop-detail-version").textContent = entry.version || "未填写";
      $("workshop-detail-game-version").textContent = entry.game_version || "未填写";
      $("workshop-detail-size").textContent = formatSize(entrySize(entry));
      $("workshop-detail-description").textContent = entry.description || "作者暂未填写说明。";
      $("workshop-detail-sha256").textContent = entryHash(entry);
      message("workshop-analysis", "下载后先检查文件格式、哈希、游戏资源匹配和冲突。通过检查也不会自动安装。");
    }
    renderItems();
    syncButtons();
  }

  function renderItems() {
    const list = $("workshop-results");
    list.replaceChildren();
    const query = $("workshop-search").value.trim().toLocaleLowerCase();
    const category = $("workshop-category").value;
    const items = state.items.filter((entry) => {
      const text = [entry.name, entry.author, entry.description].join(" ").toLocaleLowerCase();
      return (!query || text.includes(query)) && (!category || entry.category === category);
    });
    $("workshop-count").textContent = `${items.length} / ${state.items.length} 个作品`;
    if (!items.length) {
      const empty = node("div", "workshop-empty");
      empty.appendChild(node("strong", "", state.items.length ? "没有找到匹配的作品" : "暂时没有已审核的作品"));
      empty.appendChild(node("span", "", state.items.length ? "换个关键词或分类试试。" : "可以先准备投稿包，作品通过人工审核后才会上架。"));
      list.appendChild(empty);
      return;
    }
    for (const entry of items) {
      const button = node("button", "workshop-item");
      button.type = "button";
      button.classList.toggle("is-selected", sameEntry(entry, state.selected));
      button.setAttribute("aria-pressed", String(sameEntry(entry, state.selected)));
      const heading = node("div", "workshop-item-heading");
      heading.appendChild(node("strong", "", entry.name || "未命名作品"));
      heading.appendChild(node("span", "workshop-category-tag", CATEGORY_LABELS[entry.category] || "其他"));
      button.appendChild(heading);
      button.appendChild(node("span", "workshop-item-meta", `${entry.author || "未填写作者"} · ${entry.version || "未填写版本"} · ${formatSize(entrySize(entry))}`));
      button.appendChild(node("span", "workshop-item-description", entry.description || "暂无说明"));
      button.addEventListener("click", () => {
        if (!state.working && !state.catalogLoading) showDetail(entry);
      });
      list.appendChild(button);
    }
  }

  async function refresh(force = false) {
    if (state.working || state.catalogLoading) return;
    state.catalogLoading = true;
    const request = ++state.catalogVersion;
    showDetail(null);
    message("workshop-catalog-status", "正在读取已审核作品目录…");
    syncButtons();
    try {
      const catalog = await bridge.call("workshop_catalog", { quiet: true }, force);
      if (request !== state.catalogVersion) return;
      state.items = Array.isArray(catalog?.mods) ? catalog.mods : [];
      state.loaded = catalog?.status !== "unavailable";
      message("workshop-catalog-status", catalog.message || `已读取 ${state.items.length} 个已审核作品。`, !state.loaded);
      renderItems();
    } catch (error) {
      if (request !== state.catalogVersion) return;
      state.items = [];
      state.loaded = false;
      message("workshop-catalog-status", `目录读取失败：${error.message || "网络暂不可用"}。可点击“刷新目录”重试。`, true);
      renderItems();
    } finally {
      if (request === state.catalogVersion) {
        state.catalogLoading = false;
        syncButtons();
      }
    }
  }

  function inspectionText(inspection) {
    if (!inspection) return "";
    const files = count(inspection.file_count ?? inspection.files);
    const size = inspection.total_size ?? inspection.size;
    const summary = [`格式检查通过 · ${files} 个文件 · 解压后 ${formatSize(size)}`];
    if (inspection.sha256) summary.push(`SHA-256：${inspection.sha256}`);
    const warnings = Array.isArray(inspection.warnings) ? inspection.warnings : [];
    for (const warning of warnings) summary.push(`提示：${String(warning)}`);
    return summary.join("\n");
  }

  function analysisText(result) {
    const analysis = result?.analysis || {};
    const lines = [inspectionText(result?.inspection)];
    lines.push(`当前游戏：可匹配 ${count(analysis.matched)} 个资源包，未匹配 ${count(analysis.unmatched)} 个，冲突 ${count(analysis.conflicts)} 个。`);
    const conflicts = Array.isArray(analysis.conflict_names) ? analysis.conflict_names : [];
    if (conflicts.length) lines.push(`冲突作品：${conflicts.map(String).join("、")}`);
    if (analysis.message) lines.push(String(analysis.message));
    lines.push(analysis.can_install === true
      ? "可以进入安装确认。安装前会再次核对审核目录、文件与兼容性，并尝试系统杀毒扫描。"
      : "本次检查未满足安装条件，请先处理兼容性或安全检查问题。");
    return lines.filter(Boolean).join("\n");
  }

  async function prepare() {
    if (!state.selected || state.working || state.catalogLoading) return;
    const selected = state.selected;
    const request = state.selectionVersion;
    state.working = true;
    state.prepared = null;
    hideConfirmation();
    syncButtons();
    message("workshop-analysis", "正在下载并检查资源包，请稍候…");
    try {
      const result = await bridge.call("workshop_prepare", {
        busy: true, busyText: "下载并检查工坊资源包…", quiet: true,
      }, selected.id, entryHash(selected));
      if (request !== state.selectionVersion || !sameEntry(state.selected, selected)) return;
      if (!sameEntry(result?.entry, selected)) throw new Error("检查结果与当前作品不一致，请重新刷新目录。");
      state.prepared = result;
      message("workshop-analysis", analysisText(result), result.analysis?.can_install !== true);
    } catch (error) {
      if (request === state.selectionVersion) {
        state.prepared = null;
        message("workshop-analysis", `下载或检查失败：${error.message || "请重试"}`, true);
      }
    } finally {
      state.working = false;
      syncButtons();
    }
  }

  function confirmInstall() {
    if (!canInstall() || state.working) return;
    const analysis = state.prepared.analysis;
    const conflictNote = count(analysis.conflicts) > 0
      ? `与现有 Mod 有 ${count(analysis.conflicts)} 个资源包冲突，继续会替换这些资源；已有 Mod 的效果可能受影响。`
      : "本次检查未发现资源包冲突。";
    message("workshop-install-confirm-text", `确认安装「${state.selected.name}」？${conflictNote}安装会自动备份，可在 Mod 管理中禁用或卸载。`);
    $("workshop-install-confirm").classList.remove("is-hidden");
    $("workshop-install").setAttribute("aria-expanded", "true");
    $("workshop-install-accept").focus();
  }

  async function install() {
    if (!canInstall() || state.working || $("workshop-install-confirm").classList.contains("is-hidden")) return;
    state.working = true;
    syncButtons();
    try {
      const result = await bridge.call("workshop_install", {
        busy: true, busyText: "复查并扫描资源包，安装中…", quiet: true,
      });
      state.prepared = null;
      hideConfirmation();
      bridge.onInstall?.(result);
      message("workshop-analysis", `已安装「${result.name || state.selected?.name || "作品"}」。可在“Mod 管理”禁用、卸载或还原。`);
      bridge.toast("工坊作品已安装，原文件已自动备份");
    } catch (error) {
      state.prepared = null;
      hideConfirmation();
      message("workshop-analysis", `安装未完成：${error.message || "检查未通过"}。请重新下载检查后再试。`, true);
    } finally {
      state.working = false;
      syncButtons();
    }
  }

  async function chooseSubmission() {
    if (state.working || state.catalogLoading) return;
    state.working = true;
    state.submission = null;
    message("workshop-submission-result", "");
    syncButtons();
    try {
      const result = await bridge.call("workshop_choose_submission", {
        busy: true, busyText: "选择并检查投稿 ZIP…", quiet: true,
      });
      if (result?.cancelled) {
        message("workshop-submission-file", "未选择 ZIP。可先到“我的作品集”导出作品。");
        return;
      }
      if (!result?.inspection) throw new Error("投稿包没有通过格式检查。");
      state.submission = result;
      message("workshop-submission-file", `已选择：${result.path_label || "投稿 ZIP"}\n${inspectionText(result.inspection)}`);
    } catch (error) {
      message("workshop-submission-file", `投稿包检查失败：${error.message || "请选择本工具导出的 ZIP"}`, true);
    } finally {
      state.working = false;
      syncButtons();
    }
  }

  async function exportSubmission(event) {
    event?.preventDefault();
    if (!state.submission || state.working || state.catalogLoading) return;
    const form = $("workshop-submit-form");
    if (!form.reportValidity()) return;
    const metadata = {};
    for (const field of ["name", "author", "description", "category", "version", "game_version"]) {
      metadata[field] = $(`workshop-submit-${field}`).value.trim();
    }
    state.working = true;
    syncButtons();
    try {
      const result = await bridge.call("workshop_export_submission", {
        busy: true, busyText: "复查并导出投稿包…", quiet: true,
      }, metadata);
      if (result?.cancelled) {
        message("workshop-submission-result", "已取消导出。选好的 ZIP 仍可继续使用。");
        return;
      }
      const reportLine = result.report_path ? `\n检查报告：${result.report_path}` : "";
      message("workshop-submission-result", `已导出投稿包：${result.path || "所选保存位置"}${reportLine}\n下一步：打开 GitHub 投稿页，手动上传生成的文件。提交后等待人工审核，通过审核才会上架。`);
      bridge.toast("投稿包已导出，还需到 GitHub 手动提交审核");
    } catch (error) {
      message("workshop-submission-result", `导出失败：${error.message || "请重新检查投稿包"}`, true);
    } finally {
      state.working = false;
      syncButtons();
    }
  }

  async function openLink(kind, id = "") {
    if (state.working || state.catalogLoading) return;
    try {
      const result = await bridge.call("workshop_open_link", { quiet: true }, kind, id);
      if (result === false) throw new Error("浏览器暂时无法打开，请稍后重试。");
    } catch (error) {
      bridge.toast(error.message || "链接打开失败", true);
    }
  }

  function init(context) {
    bridge = context;
    if (state.initialized) return;
    state.initialized = true;
    $("workshop-refresh").addEventListener("click", () => refresh(true));
    $("workshop-search").addEventListener("input", () => { renderItems(); syncButtons(); });
    $("workshop-category").addEventListener("change", () => { renderItems(); syncButtons(); });
    $("workshop-prepare").addEventListener("click", prepare);
    $("workshop-install").addEventListener("click", confirmInstall);
    $("workshop-install-accept").addEventListener("click", install);
    $("workshop-install-cancel").addEventListener("click", hideConfirmation);
    $("workshop-choose-submission").addEventListener("click", chooseSubmission);
    $("workshop-submit-form").addEventListener("submit", exportSubmission);
    $("workshop-open-submission").addEventListener("click", () => openLink("submission"));
    $("workshop-repository").addEventListener("click", () => openLink("repository"));
    $("workshop-report").addEventListener("click", () => {
      if (state.selected) openLink("report", state.selected.id);
    });
    syncButtons();
  }

  function enter() {
    if (!state.loaded && !state.catalogLoading) refresh(false);
  }

  function leave() {
    hideConfirmation();
  }

  window.ModWorkshop = { init, enter, leave };
})();
