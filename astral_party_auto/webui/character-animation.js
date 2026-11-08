/* 角色动画直接导出独立帧包，制作状态与资源替换作品集分别保存。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    active: false, working: false, request: 0, previewRequest: 0, data: null,
    originalRequest: 0, originalPreviewRequest: 0, originalLoading: false,
    originals: [], originalBinding: null, preferredSource: null, defaultActions: [],
  };
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

  function effectiveAction() {
    return state.originalBinding?.runtime_action || selectedAction();
  }

  function sameTarget(currentRole, action) {
    return state.active && currentRole === Number($("character-role-id").value) && action === selectedAction();
  }

  function syncButtons() {
    const locked = state.working;
    ["character-role-id", "character-action", "character-animation-fps", "character-animation-loop"].forEach((id) => {
      $(id).disabled = locked;
    });
    const sameRole = state.data?.role_id === Number($("character-role-id").value);
    const action = state.data?.actions?.find((row) => row.action === effectiveAction());
    const bindingUnavailable = state.originalLoading || (state.originals.length > 0 && !state.originalBinding?.runtime_action);
    $("character-import-media").disabled = locked || !sameRole || bindingUnavailable;
    $("character-import-folder").disabled = locked || !sameRole || bindingUnavailable;
    $("character-remove-action").disabled = locked || !sameRole || !action;
    $("character-export-animation").disabled = locked || !sameRole || !state.data?.actions?.length;
    $("character-animation-actions").querySelectorAll("button").forEach((button) => { button.disabled = locked; });
    $("character-original-variant").disabled = locked || !state.originals.length;
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
      button.setAttribute("aria-pressed", String(row.action === effectiveAction()));
      button.addEventListener("click", () => {
        if (state.working) return;
        $("character-action").value = row.action;
        state.preferredSource = null;
        render();
        loadComparison();
      });
      host.appendChild(button);
    });
    $("character-animation-notice").textContent = state.data?.notice
      || "缺失动作会回退到待机或上一张自定义帧，建议补齐待机、行走与战斗。";
    syncButtons();
  }

  function syncActionChoices(choices) {
    const selected = selectedAction();
    const actions = new Map((choices.length ? choices : state.defaultActions).map((row) => [row.action, row.label]));
    for (const row of state.data?.actions || []) actions.set(row.action, row.label || row.action);
    if (!actions.has(selected)) actions.set(selected, selected);
    const select = $("character-action");
    select.replaceChildren();
    for (const [action, label] of actions) {
      const option = document.createElement("option");
      option.value = action;
      option.textContent = label === action ? action : `${label}（${action}）`;
      select.appendChild(option);
    }
    select.value = selected;
  }

  function clearOriginal(message) {
    state.originalRequest += 1;
    state.originalPreviewRequest += 1;
    state.originalLoading = false;
    state.originals = [];
    state.originalBinding = null;
    $("character-original-variant").replaceChildren();
    $("character-original-status").textContent = message;
    bridge.disposeAnimation?.("character-animation-original");
    bridge.renderAnimationState($("character-animation-original"), message);
    syncButtons();
  }

  async function loadOriginalPreview() {
    const sequence = ++state.originalPreviewRequest;
    const currentRole = Number($("character-role-id").value);
    const action = selectedAction();
    const bundle = $("character-original-variant").value;
    const target = state.originals.find((row) => row.bundle === bundle);
    if (!target) return;
    state.originalLoading = true;
    state.originalBinding = null;
    syncButtons();
    const media = $("character-animation-original");
    bridge.disposeAnimation?.("character-animation-original");
    bridge.renderAnimationState(media, "正在读取游戏原动作…");
    $("character-original-status").textContent = "正在核对原版帧与运行时动作键…";
    try {
      const animation = await request("character_animation_original_preview", currentRole, target.name, bundle);
      if (sequence !== state.originalPreviewRequest || !sameTarget(currentRole, action)) return;
      state.originalBinding = animation;
      bridge.renderAnimation(media, animation);
      $("character-original-status").textContent = animation.runtime_action
        ? `原片段 ${target.name} · 替换动作键 ${animation.runtime_action}`
        : animation.runtime_binding_reason || "原动作可以播放，但没有唯一的运行时动作键，无法自动绑定替换。";
    } catch (error) {
      if (sequence !== state.originalPreviewRequest || !sameTarget(currentRole, action)) return;
      const message = error.message || "游戏原动作暂时无法播放。";
      bridge.renderAnimationState(media, message);
      $("character-original-status").textContent = message;
    } finally {
      if (sequence === state.originalPreviewRequest && sameTarget(currentRole, action)) {
        state.originalLoading = false;
        render();
      }
    }
  }

  async function loadOriginals() {
    clearOriginal("正在查找对应的游戏原动作…");
    const sequence = state.originalRequest;
    let currentRole;
    try { currentRole = roleId(); } catch { return; }
    const action = selectedAction();
    const preferred = state.preferredSource;
    const sourceAction = preferred?.action === action ? preferred.name : action;
    state.originalLoading = true;
    syncButtons();
    try {
      const data = await request("character_animation_originals", currentRole, sourceAction, preferred?.bundle || "");
      if (sequence !== state.originalRequest || !sameTarget(currentRole, action)) return;
      state.originals = data.items || [];
      syncActionChoices(data.action_choices || []);
      const select = $("character-original-variant");
      for (const row of state.originals) {
        const option = document.createElement("option");
        option.value = row.bundle;
        option.textContent = `${row.label || "原版外观"} · ${row.name}`;
        select.appendChild(option);
      }
      select.value = state.originals.find((row) => row.bundle === preferred?.bundle)?.bundle || state.originals[0]?.bundle || "";
      if (state.originals.length) {
        await loadOriginalPreview();
      } else {
        const message = data.reason || "未找到游戏原动作，仍可导入手动指定的动作。";
        bridge.renderAnimationState($("character-animation-original"), message);
        $("character-original-status").textContent = message;
      }
    } catch (error) {
      if (sequence !== state.originalRequest || !sameTarget(currentRole, action)) return;
      const message = error.message || "原版动作查询失败，请核对游戏与资源索引。";
      bridge.renderAnimationState($("character-animation-original"), message);
      $("character-original-status").textContent = message;
    } finally {
      if (sequence === state.originalRequest && sameTarget(currentRole, action)) {
        state.originalLoading = false;
        syncButtons();
      }
    }
  }

  async function loadComparison() {
    const currentRole = Number($("character-role-id").value);
    const action = selectedAction();
    state.previewRequest += 1;
    bridge.disposeAnimation?.("character-animation-preview");
    bridge.renderAnimationState($("character-animation-preview"), "正在读取对应的替换动作…");
    const pending = loadOriginals();
    const sequence = state.originalRequest;
    await pending;
    if (sequence === state.originalRequest && sameTarget(currentRole, action)) await loadPreview();
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
    const action = effectiveAction();
    const media = $("character-animation-preview");
    bridge.disposeAnimation?.("character-animation-preview");
    if (!state.data?.actions?.some((row) => row.action === action)) {
      bridge.renderAnimationState(media, "当前动作还没有素材，导入后可播放和逐帧检查。");
      return;
    }
    bridge.renderAnimationState(media, "正在读取已保存的动作帧…");
    try {
      const animation = await request("character_animation_preview", currentRole, action);
      if (!state.active || sequence !== state.previewRequest || currentRole !== Number($("character-role-id").value) || action !== effectiveAction()) return;
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
      syncActionChoices([]);
      render();
      status("已读取角色动作工程，导入后会直接保存到本机。");
      await loadComparison();
    }, "正在读取角色动作工程…");
  }

  function importMedia(folder) {
    return run(async (sequence) => {
      const currentRole = roleId();
      const data = await request("character_animation_import", currentRole, effectiveAction(), {
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
    state.defaultActions = Array.from($("character-action").options).map((option) => ({ action: option.value, label: option.textContent }));
    $("character-role-id").addEventListener("input", () => {
      state.request += 1;
      state.previewRequest += 1;
      state.preferredSource = null;
      clearOriginal("角色编号已改变，确认后重新读取原动作。");
      bridge.disposeAnimation?.("character-animation-preview");
      bridge.renderAnimationState($("character-animation-preview"), "确认角色编号后读取对应的替换动作。");
    });
    $("character-role-id").addEventListener("change", refresh);
    $("character-action").addEventListener("change", () => {
      state.preferredSource = null;
      render();
      return loadComparison();
    });
    $("character-original-variant").addEventListener("change", async () => {
      state.previewRequest += 1;
      bridge.disposeAnimation?.("character-animation-preview");
      bridge.renderAnimationState($("character-animation-preview"), "正在核对所选原版外观…");
      const pending = loadOriginalPreview();
      const sequence = state.originalPreviewRequest;
      await pending;
      if (state.active && sequence === state.originalPreviewRequest) await loadPreview();
    });
    $("character-import-media").addEventListener("click", () => importMedia(false));
    $("character-import-folder").addEventListener("click", () => importMedia(true));
    $("character-remove-action").addEventListener("click", () => run(async (sequence) => {
      const data = await request("character_animation_remove", roleId(), effectiveAction());
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
    state.originalRequest += 1;
    state.originalPreviewRequest += 1;
    bridge.disposeAnimation?.("character-animation-original");
    bridge.disposeAnimation?.("character-animation-preview");
  }

  async function selectTarget(target) {
    const selectedRole = Number(target?.roleId);
    if (!Number.isInteger(selectedRole) || selectedRole < 1 || selectedRole > 2147483647) {
      status("当前动作还没有经过核对的角色编号，请填写实际游戏角色编号。", true);
      return;
    }
    const action = String(target.action || "");
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,79}$/.test(action)) {
      status("当前资源的动作名称无法核对，请选择实际动作。", true);
      return;
    }
    state.request += 1;
    state.previewRequest += 1;
    clearOriginal("正在切换原动作…");
    bridge.disposeAnimation?.("character-animation-preview");
    bridge.renderAnimationState($("character-animation-preview"), "正在读取所选角色的替换动作…");
    state.data = null;
    state.preferredSource = target.preferred_source?.name
      ? { ...target.preferred_source, action } : null;
    $("character-role-id").value = String(selectedRole);
    const choices = $("character-action");
    if (!Array.from(choices.options || []).some((option) => option.value === action)) {
      const option = document.createElement("option");
      option.value = action;
      option.textContent = `原动作（${action}）`;
      choices.appendChild(option);
    }
    choices.value = action;
    const fps = Number(target.fps);
    if (Number.isFinite(fps) && fps >= 1 && fps <= 60) $("character-animation-fps").value = String(Math.round(fps));
    if (typeof target.loop === "boolean") $("character-animation-loop").checked = target.loop;
    render();
    if (state.active && !state.working) await refresh();
  }

  window.CharacterAnimation = { init, enter, leave, selectTarget };
})();
