/* 角色动画直接导出独立帧包，制作状态与资源替换作品集分别保存。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { active: false, working: false, request: 0, previewRequest: 0, data: null };
  let bridge;

  function roleId() {
    const value = Number($("character-role-id").value);
    if (!Number.isInteger(value) || value < 1 || value > 2147483647) throw new Error("请填写有效的游戏内部角色编号。");
    return value;
  }

  function status(message, error = false) {
    $("character-animation-status").textContent = message;
    $("character-animation-status").classList.toggle("is-error", error);
  }

  function request(name, ...args) {
    return bridge.call(name, { quiet: true }, ...args);
  }

  function selectedAction() {
    return $("character-action").value;
  }

  function syncButtons() {
    const locked = state.working;
    ["character-role-id", "character-action", "character-animation-fps", "character-animation-loop"].forEach((id) => {
      $(id).disabled = locked;
    });
    const sameRole = state.data?.role_id === Number($("character-role-id").value);
    const action = state.data?.actions?.find((row) => row.action === selectedAction());
    $("character-import-media").disabled = locked || !sameRole;
    $("character-import-folder").disabled = locked || !sameRole;
    $("character-remove-action").disabled = locked || !sameRole || !action;
    $("character-export-animation").disabled = locked || !sameRole || !state.data?.actions?.length;
    $("character-animation-actions").querySelectorAll("button").forEach((button) => { button.disabled = locked; });
  }

  function render() {
    const host = $("character-animation-actions");
    host.replaceChildren();
    const actions = state.data?.actions || [];
    if (!actions.length) {
      const hint = document.createElement("p");
      hint.className = "notice";
      hint.textContent = "选择动作后导入素材。可以依次保存待机、行走、战斗等多个动作。";
      host.appendChild(hint);
    }
    actions.forEach((row) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button ghost compact";
      button.textContent = `${row.label || row.action}，${row.frame_count} 帧，${(row.duration_ms / 1000).toFixed(2)} 秒`;
      button.setAttribute("aria-pressed", String(row.action === selectedAction()));
      button.addEventListener("click", () => {
        if (state.working) return;
        $("character-action").value = row.action;
        render();
        loadPreview();
      });
      host.appendChild(button);
    });
    $("character-animation-notice").textContent = state.data?.notice
      || "缺失动作会回退到待机或上一张自定义帧，建议补齐待机、行走与战斗。";
    syncButtons();
  }

  async function loadPreview() {
    const sequence = ++state.previewRequest;
    let currentRole;
    try {
      currentRole = roleId();
    } catch (error) {
      status(error.message, true);
      return;
    }
    const action = selectedAction();
    const media = $("character-animation-preview");
    bridge.disposeAnimation?.("character-animation-preview");
    if (!state.data?.actions?.some((row) => row.action === action)) {
      bridge.renderAnimationState(media, "当前动作还没有素材，导入后可播放和逐帧检查。");
      return;
    }
    bridge.renderAnimationState(media, "正在读取已保存的动作帧…");
    try {
      const animation = await request("character_animation_preview", currentRole, action);
      if (!state.active || sequence !== state.previewRequest || currentRole !== roleId() || action !== selectedAction()) return;
      bridge.renderAnimation(media, animation);
    } catch (error) {
      if (!state.active || sequence !== state.previewRequest) return;
      bridge.renderAnimationState(media, error.message || "动作预览读取失败。");
      status(error.message || "动作预览读取失败。", true);
    }
  }

  async function run(action, message) {
    if (state.working) return;
    state.working = true;
    const sequence = ++state.request;
    state.previewRequest += 1;
    status(message);
    syncButtons();
    try {
      await action(sequence);
    } catch (error) {
      if (state.active && sequence === state.request) status(error.message || "角色动画操作失败。", true);
    } finally {
      state.working = false;
      syncButtons();
      if (state.active && sequence !== state.request) await refresh();
    }
  }

  async function refresh() {
    return run(async (sequence) => {
      const currentRole = roleId();
      const data = await request("character_animation_state", currentRole);
      if (!state.active || sequence !== state.request || currentRole !== roleId()) return;
      state.data = data;
      render();
      status("已读取角色动作工程，导入后会直接保存到本机。");
      await loadPreview();
    }, "正在读取角色动作工程…");
  }

  function importMedia(folder) {
    return run(async (sequence) => {
      const currentRole = roleId();
      const data = await request("character_animation_import", currentRole, selectedAction(), {
        fps: Number($("character-animation-fps").value), loop: $("character-animation-loop").checked,
      }, folder);
      if (!state.active || sequence !== state.request || currentRole !== roleId()) return;
      if (!data) {
        status("已取消导入，已有动作保留。");
        return;
      }
      state.data = data;
      render();
      status("动作已保存，预览读取的是保存后的实际帧。");
      await loadPreview();
    }, "选择并导入动作素材，视频转换可能需要一些时间…");
  }

  function init(dependencies) {
    bridge = dependencies;
    $("character-role-id").addEventListener("input", syncButtons);
    $("character-role-id").addEventListener("change", refresh);
    $("character-action").addEventListener("change", () => { render(); loadPreview(); });
    $("character-import-media").addEventListener("click", () => importMedia(false));
    $("character-import-folder").addEventListener("click", () => importMedia(true));
    $("character-remove-action").addEventListener("click", () => run(async (sequence) => {
      const data = await request("character_animation_remove", roleId(), selectedAction());
      if (!state.active || sequence !== state.request) return;
      state.data = data;
      render();
      status("当前动作已移除，其它动作保留。");
      await loadPreview();
    }, "正在移除当前动作…"));
    $("character-export-animation").addEventListener("click", () => run(async (sequence) => {
      const result = await request("character_animation_export", roleId(),
        $("character-animation-name").value, $("character-animation-author").value);
      if (!state.active || sequence !== state.request) return;
      status(`已导出 ${result.action_count} 个动作、${result.frame_count} 帧。保存位置 ${result.package || result.path}。在 Toys 角色素材库导入 ZIP 后加入方案。`);
    }, "正在回读并导出角色动画包…"));
    syncButtons();
  }

  async function enter() {
    if (!bridge) return;
    state.active = true;
    if (!state.working) await refresh();
  }

  function leave() {
    state.active = false;
    state.request += 1;
    state.previewRequest += 1;
    bridge.disposeAnimation?.("character-animation-preview");
  }

  window.CharacterAnimation = { init, enter, leave };
})();
