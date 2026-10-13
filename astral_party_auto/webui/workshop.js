/* 点击安装先下载检查；只有用户再次确认才写入游戏。 */
(() => {
  "use strict";

  const CATEGORY_LABELS = {
    texture: "贴图", text: "文字", animation: "动画", emote: "表情", audio: "音频", mixed: "综合",
  };
  const REVIEW_LABELS = {
    pending: "待审核", approved: "审核通过", rejected: "未通过审核", published: "已上架",
  };
  const $ = (id) => document.getElementById(id);
  const state = {
    initialized: false, loaded: false, working: false, catalogLoading: false,
    items: [], selected: null, prepared: null, submission: null,
    uploadService: null, statusLoading: false, serviceError: false, receipts: [], uploadedFingerprint: "",
    selectionVersion: 0, catalogVersion: 0, pageVersion: 0,
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
    $("workshop-install").disabled = !state.selected || locked;
    $("workshop-install").textContent = state.working ? "处理中…" : canInstall() ? "继续安装" : "安装";
    $("workshop-install").classList.toggle("is-hidden", !$("workshop-install-confirm").classList.contains("is-hidden"));
    $("workshop-install-accept").disabled = !canInstall() || locked;
    $("workshop-install-cancel").disabled = locked;
    $("workshop-report").disabled = !state.selected || locked;
    $("workshop-choose-submission").disabled = locked;
    $("workshop-export-submission").disabled = !state.submission || locked;
    const uploaded = state.uploadedFingerprint && state.uploadedFingerprint === submissionFingerprint();
    $("workshop-upload-submission").disabled = !state.submission || locked || state.statusLoading
      || state.uploadService?.configured !== true || state.uploadService?.accepting_uploads !== true || Boolean(uploaded);
    $("workshop-upload-submission").textContent = uploaded ? "已上传" : "上传作品";
    $("workshop-refresh-submissions").disabled = locked || state.statusLoading;
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
    syncButtons();
  }

  function showDetail(entry) {
    state.selected = entry;
    state.prepared = null;
    state.selectionVersion += 1;
    hideConfirmation();
    $("workshop-layout").classList.toggle("has-selection", Boolean(entry));
    $("workshop-detail").classList.toggle("is-hidden", !entry);
    if (entry) {
      $("workshop-detail-title").textContent = entry.name || "未命名作品";
      $("workshop-detail-author").textContent = entry.author || "未填写作者";
      $("workshop-detail-category").textContent = CATEGORY_LABELS[entry.category] || "其他";
      $("workshop-detail-version").textContent = entry.version || "未填写";
      $("workshop-detail-game-version").textContent = entry.game_version || "未填写";
      $("workshop-detail-size").textContent = formatSize(entrySize(entry));
      $("workshop-detail-description").textContent = entry.description || "作者暂未填写说明。";
      $("workshop-detail-sha256").textContent = entryHash(entry);
      message("workshop-analysis", "确认前会先检查资源包。");
      message("workshop-check-details", "尚未下载检查。");
      message("workshop-conflicts", "");
      $("workshop-conflicts").classList.add("is-hidden");
      $("workshop-more-info").open = false;
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
    $("workshop-list-hint").classList.toggle("is-hidden", !state.items.length);
    if (!items.length) {
      const empty = node("div", "workshop-empty");
      empty.appendChild(node("strong", "", state.items.length ? "没有找到匹配的作品" : "暂时没有已审核的作品"));
      empty.appendChild(node("span", "", state.items.length ? "换个关键词或分类试试。" : "也可以在下方分享自己的作品。"));
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
      button.appendChild(node("span", "workshop-item-meta", entry.author || "未填写作者"));
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
    message("workshop-catalog-status", "加载作品中…");
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

  function showCheckResult(result) {
    const analysis = result?.analysis || {};
    let summary = "检查通过，确认后开始安装。";
    if (analysis.can_install !== true) {
      summary = count(analysis.unmatched) > 0
        ? "不适用当前游戏版本，请换一个作品。"
        : analysis.message || "暂时无法安装，请检查游戏是否安装。";
    }
    message("workshop-analysis", summary, analysis.can_install !== true);
    message("workshop-check-details", analysisText(result));
    const conflicts = Array.isArray(analysis.conflict_names) ? analysis.conflict_names : [];
    const warning = count(analysis.conflicts) > 0
      ? `将覆盖已有 Mod 的 ${count(analysis.conflicts)} 个资源包${conflicts.length ? `（${conflicts.map(String).join("、")}）` : ""}。原有效果可能改变。`
      : "";
    message("workshop-conflicts", warning);
    $("workshop-conflicts").classList.toggle("is-hidden", !warning);
  }

  async function prepare() {
    if (!state.selected || state.working || state.catalogLoading) return;
    const selected = state.selected;
    const request = state.selectionVersion;
    state.working = true;
    state.prepared = null;
    message("workshop-conflicts", "");
    $("workshop-conflicts").classList.add("is-hidden");
    hideConfirmation();
    syncButtons();
    message("workshop-analysis", "正在下载并检查…");
    try {
      const result = await bridge.call("workshop_prepare", {
        busy: true, busyText: "下载并检查工坊资源包…", quiet: true,
      }, selected.id, entryHash(selected));
      if (request !== state.selectionVersion || !sameEntry(state.selected, selected)) return;
      if (!sameEntry(result?.entry, selected)) throw new Error("检查结果与当前作品不一致，请重新刷新目录。");
      state.prepared = result;
      showCheckResult(result);
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

  async function beginInstall() {
    if (!state.selected || state.working || state.catalogLoading) return;
    const page = state.pageVersion;
    if (!canInstall()) await prepare();
    if (page === state.pageVersion && canInstall()) confirmInstall();
  }

  function confirmInstall() {
    if (!canInstall() || state.working) return;
    const analysis = state.prepared.analysis;
    const conflictNote = count(analysis.conflicts) > 0
      ? `与现有 Mod 有 ${count(analysis.conflicts)} 个资源包冲突，继续会替换这些资源；已有 Mod 的效果可能受影响。`
      : "";
    message("workshop-install-confirm-text", `安装「${state.selected.name}」？${conflictNote}确认后会扫描并安装，原文件会自动备份。`);
    $("workshop-install-confirm").classList.remove("is-hidden");
    $("workshop-install").setAttribute("aria-expanded", "true");
    syncButtons();
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
      $("workshop-conflicts").classList.add("is-hidden");
      bridge.onInstall?.(result);
      message("workshop-analysis", "已安装。可在“Mod 管理”禁用或卸载。");
      bridge.toast("工坊作品已安装，原文件已自动备份");
    } catch (error) {
      state.prepared = null;
      hideConfirmation();
      message("workshop-analysis", `安装未完成：${error.message || "检查未通过"}`, true);
    } finally {
      state.working = false;
      syncButtons();
    }
  }

  async function chooseSubmission() {
    if (state.working || state.catalogLoading) return;
    state.working = true;
    state.submission = null;
    state.uploadedFingerprint = "";
    message("workshop-submission-result", "");
    message("workshop-submission-check-details", "");
    message("workshop-submission-export-details", "");
    syncButtons();
    try {
      const result = await bridge.call("workshop_choose_submission", {
        busy: true, busyText: "选择并检查资源替换 ZIP…", quiet: true,
      });
      if (result?.cancelled) {
        message("workshop-submission-file", "未选择 ZIP。支持其他工具制作的资源替换包。");
        return;
      }
      if (!result?.inspection) throw new Error("投稿包没有通过格式检查。");
      state.submission = result;
      message("workshop-submission-file", `已选择 ${result.path_label || "资源替换 ZIP"}，检查通过。`);
      message("workshop-submission-check-details", inspectionText(result.inspection));
    } catch (error) {
      message("workshop-submission-file", `投稿包检查失败：${error.message || "请确认是有效的资源替换 ZIP"}`, true);
    } finally {
      state.working = false;
      syncButtons();
    }
  }

  function submissionMetadata() {
    const metadata = {};
    for (const field of ["name", "author", "description", "category", "version", "game_version"]) {
      metadata[field] = $(`workshop-submit-${field}`).value.trim();
    }
    metadata.author ||= "匿名作者";
    metadata.category ||= "mixed";
    metadata.version ||= "1.0.0";
    return metadata;
  }

  function submissionFingerprint() {
    return state.submission ? JSON.stringify([state.submission.inspection?.sha256 || "", submissionMetadata()]) : "";
  }

  function showUploadService() {
    let text;
    if (state.statusLoading) {
      text = "正在连接上传服务…";
    } else if (state.serviceError) {
      text = "暂无法确认上传服务状态，已暂停上传。请刷新审核状态重试。";
    } else if (state.uploadService?.configured !== true) {
      text = "上传服务暂未开通。可先选择 ZIP 和填写介绍，开通后再上传。";
    } else if (state.uploadService.accepting_uploads !== true) {
      text = state.uploadService.message || "上传服务暂未接收作品，请稍后刷新。";
    } else {
      text = "上传后进入人工审核，通过后才会出现在工坊。";
      if (Number(state.uploadService.max_package_bytes) > 0) {
        text += ` 单个 ZIP 最多 ${formatSize(state.uploadService.max_package_bytes)}。`;
      }
    }
    message("workshop-upload-service-status", text);
  }

  function renderReceipts() {
    const list = $("workshop-submission-receipts");
    list.replaceChildren();
    $("workshop-my-submissions").classList.toggle("is-hidden", !state.receipts.length);
    for (const receipt of state.receipts) {
      const item = node("div", "workshop-receipt");
      const heading = node("div", "workshop-item-heading");
      heading.appendChild(node("strong", "", receipt.name || "未命名作品"));
      heading.appendChild(node("span", "workshop-review-status", REVIEW_LABELS[receipt.status] || "等待更新"));
      item.appendChild(heading);
      item.appendChild(node("p", "workshop-receipt-number", `投稿编号：${receipt.submission_id || "未返回"}`));
      if (receipt.message) item.appendChild(node("p", "workshop-status", receipt.message));
      list.appendChild(item);
    }
  }

  async function loadUploadStatus(refresh = false) {
    if (state.statusLoading || state.working) return;
    state.statusLoading = true;
    showUploadService();
    syncButtons();
    try {
      const result = await bridge.call("get_workshop_upload_status", { quiet: true }, refresh);
      if (!result || typeof result.configured !== "boolean") throw new Error("上传服务状态无效");
      state.uploadService = result;
      state.serviceError = false;
      state.receipts = Array.isArray(result.submissions)
        ? result.submissions.filter((receipt) => receipt && typeof receipt.submission_id === "string").slice(0, 100)
        : [];
      renderReceipts();
      message("workshop-review-refresh-result", refresh && result.configured && result.accepting_uploads ? "审核状态已刷新。" : "");
    } catch (error) {
      // 状态检查失败时停止上传，保留已经收到的审核回执。
      state.uploadService = null;
      state.serviceError = true;
      message("workshop-review-refresh-result", `状态读取失败：${error.message || "网络暂不可用"}。可点击“刷新审核状态”重试。`, true);
    } finally {
      state.statusLoading = false;
      showUploadService();
      syncButtons();
    }
  }

  async function uploadSubmission(event) {
    event?.preventDefault();
    if ($("workshop-upload-submission").disabled || state.working || state.catalogLoading) return;
    if (!$("workshop-submit-form").reportValidity()) return;
    const metadata = submissionMetadata();
    const fingerprint = submissionFingerprint();
    state.working = true;
    syncButtons();
    message("workshop-submission-result", "正在复查并上传作品，请不要重复点击…");
    try {
      const result = await bridge.call("submit_workshop_upload", {
        busy: true, busyText: "安全检查并上传作品…", quiet: true,
      }, metadata);
      if (typeof result?.submission_id !== "string" || !result.submission_id
          || !["pending", "approved", "rejected", "published"].includes(result.status)) {
        throw new Error("服务器未返回有效的审核回执，请重试确认上传状态。");
      }
      state.uploadedFingerprint = fingerprint;
      state.receipts = [result, ...state.receipts.filter((receipt) => receipt.submission_id !== result.submission_id)];
      renderReceipts();
      const status = REVIEW_LABELS[result.status];
      message("workshop-submission-result", `上传完成，${result.status === "pending" ? "待人工审核" : status}。投稿编号：${result.submission_id}。可点击“刷新审核状态”查看进度。`);
      bridge.toast(`作品已上传，${status}`);
    } catch (error) {
      message("workshop-submission-result", `上传未确认：${error.message || "网络暂不可用"}。选好的 ZIP 和介绍已保留，可点击“上传作品”重试。`, true);
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
    const metadata = submissionMetadata();
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
      const alreadyUploaded = state.uploadedFingerprint && state.uploadedFingerprint === submissionFingerprint();
      message("workshop-submission-result", alreadyUploaded
        ? "本地副本已导出。此前上传的作品可在“我的投稿”查看审核状态。"
        : "本地副本已导出，尚未上传。要投稿，请点击“上传作品”。");
      message("workshop-submission-export-details", `投稿包：${result.path || "所选保存位置"}${reportLine}`);
      bridge.toast("本地副本已导出");
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
    $("workshop-install").addEventListener("click", beginInstall);
    $("workshop-install-accept").addEventListener("click", install);
    $("workshop-install-cancel").addEventListener("click", hideConfirmation);
    $("workshop-choose-submission").addEventListener("click", chooseSubmission);
    $("workshop-submit-form").addEventListener("submit", uploadSubmission);
    $("workshop-submit-form").addEventListener("input", syncButtons);
    $("workshop-submit-form").addEventListener("change", syncButtons);
    $("workshop-export-submission").addEventListener("click", exportSubmission);
    $("workshop-refresh-submissions").addEventListener("click", () => loadUploadStatus(true));
    $("workshop-repository").addEventListener("click", () => openLink("repository"));
    $("workshop-report").addEventListener("click", () => {
      if (state.selected) openLink("report", state.selected.id);
    });
    syncButtons();
    showUploadService();
  }

  function enter() {
    if (!state.loaded && !state.catalogLoading) refresh(false);
    loadUploadStatus(true);
  }

  function leave() {
    state.pageVersion += 1;
    hideConfirmation();
  }

  window.ModWorkshop = { init, enter, leave };
})();
