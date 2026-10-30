/* 自动检测之外保留明确的客户端选择，取消或失败不改当前工作台。 */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let bridge = null;
  let initialized = false;
  let working = false;
  let dashboard = {};

  function render() {
    for (const id of ["choose-game-folder", "choose-game-program", "auto-game-location"]) {
      const button = $(id);
      if (button) button.disabled = !bridge || working;
    }
    const status = $("game-location-status");
    if (status) status.textContent = working ? "正在选择并检查游戏位置…"
      : dashboard.game_detection_note || (dashboard.game_location_mode === "manual"
      ? "当前使用手动选择的客户端；可恢复自动检测。"
      : "自动检测不到，或装了多个客户端时，可以手动选择游戏程序。");
  }

  function update(value) {
    dashboard = value || {};
    render();
  }

  async function change(kind) {
    if (!bridge || working) return;
    working = true;
    render();
    try {
      const result = kind === "auto" ? await bridge.api("restore_automatic_game_location")
        : await bridge.api("choose_game_location", kind);
      if (!result) return;
      dashboard = result.dashboard || {};
      bridge.onChanged(result);
      bridge.toast(kind === "auto" ? "已恢复自动检测" : "已记住所选游戏客户端");
    } catch (error) {
      bridge.toast(error?.message || "游戏位置未更改，请重新选择。", true);
    } finally {
      working = false;
      render();
    }
  }

  function init(context) {
    bridge = context;
    if (!initialized) {
      initialized = true;
      $("choose-game-folder")?.addEventListener("click", () => change("folder"));
      $("choose-game-program")?.addEventListener("click", () => change("program"));
      $("auto-game-location")?.addEventListener("click", () => change("auto"));
    }
    render();
  }

  window.GameLocation = { init, update };
})();
