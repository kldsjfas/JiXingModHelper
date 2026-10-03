/* 更新检查独立于资源操作，网络请求不会锁住工作台。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const labels = {
    idle: "检查更新", checking: "检查中…", available: "发现新版本",
    up_to_date: "检查更新", ahead: "检查更新", error: "检查失败", no_release: "检查更新",
  };
  let bridge;
  let initialized = false;
  let pollingTimer = null;
  let requestVersion = 0;
  let status = { status: "idle", checking: false };

  function showPanel(open) {
    $("update-panel").classList.toggle("is-hidden", !open);
    $("check-updates").setAttribute("aria-expanded", String(open));
  }

  function render(result) {
    status = result || status;
    const checking = Boolean(status.checking);
    const kind = checking ? "checking" : status.status;
    const button = $("check-updates");
    const current = status.current_version || bridge.appInfo.version || "未知";
    $("app-version").textContent = current.startsWith("v") ? current : `v${current}`;
    $("app-version").title = `当前助手版本：${current}`;
    $("update-button-label").textContent = labels[kind] || "检查更新";
    button.classList.toggle("has-update", kind === "available");
    button.classList.toggle("has-error", kind === "error");
    button.classList.toggle("is-checking", checking);
    button.setAttribute("aria-busy", String(checking));
    button.title = status.message || "启动后自动检查，也可以点击手动检查更新";
    $("update-message").textContent = status.message || "启动后会自动检查新版本。";
    $("update-versions").textContent = `当前 ${current}${status.latest_version ? ` · 最新正式版 ${status.latest_version}` : ""}`;
    $("retry-updates").disabled = checking;
    $("retry-updates").textContent = kind === "error" ? "重试" : checking ? "检查中…" : "重新检查";
    $("open-release").textContent = kind === "available" ? "查看新版本 ↗" : "查看发布页 ↗";
  }

  function showError(error) {
    render({ ...status, checking: false, status: "error", message: error?.message || "暂时无法检查更新，请重试或查看发布页。" });
  }

  function pollResult(request) {
    pollingTimer = setTimeout(async () => {
      try {
        const result = await bridge.api("get_update_status");
        if (request !== requestVersion) return;
        render(result);
        if (result.checking) pollResult(request);
      } catch (error) {
        if (request === requestVersion) showError(error);
      }
    }, 800);
  }

  async function check(force = false) {
    clearTimeout(pollingTimer);
    const request = ++requestVersion;
    render({ ...status, checking: true, message: "正在检查 GitHub 上的正式版本…" });
    try {
      const result = await bridge.api("check_for_updates", force);
      if (request !== requestVersion) return;
      render(result);
      if (result.checking) pollResult(request);
    } catch (error) {
      if (request === requestVersion) showError(error);
    }
  }

  async function openLink(kind) {
    try {
      const opened = await bridge.api("open_project_link", kind);
      if (opened === false) throw new Error("未能打开浏览器，请稍后重试。");
    } catch (error) {
      bridge.toast(error.message || "未能打开浏览器", true);
    }
  }

  function init(context) {
    bridge = context;
    if (initialized) return;
    initialized = true;
    $("check-updates").disabled = false;
    $("open-repository").disabled = false;
    $("open-community").disabled = false;
    $("open-repository").title = context.appInfo.repository_url || "在浏览器中打开项目 GitHub 仓库";
    $("check-updates").addEventListener("click", () => {
      showPanel(true);
      if (!status.checking) check(true);
    });
    $("retry-updates").addEventListener("click", () => check(true));
    $("close-update-panel").addEventListener("click", () => {
      showPanel(false);
      $("check-updates").focus();
    });
    $("open-repository").addEventListener("click", () => openLink("repository"));
    $("open-community").addEventListener("click", () => openLink("community"));
    $("open-release").addEventListener("click", () => openLink("release"));
    document.addEventListener("click", (event) => {
      if (!$("project-tools").contains(event.target)) showPanel(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("update-panel").classList.contains("is-hidden")) {
        showPanel(false);
        $("check-updates").focus();
      }
    });
    check(false);
  }

  window.ProjectUpdates = { init };
})();
