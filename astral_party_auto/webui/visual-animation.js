/* 卡面和立绘的静态转动态试用入口，候选与已保存效果分开处理。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    initialized: false, selection: null, key: "", request: 0,
    candidate: null, savedAnimation: null, working: false, loading: false, error: "",
  };
  let bridge;

  function selectionKey(selection) {
    return selection ? `${selection.asset_type || selection.kind || ""}\n${selection.bundle || ""}\n${selection.name || ""}` : "";
  }

  function supported(selection = state.selection) {
    return (selection?.asset_type || selection?.kind) === "texture" && !selection?.emote
      && selection?.visual_animation?.editable === true;
  }

  function current(request, key) {
    return request === state.request && key === state.key && supported()
      && (!bridge.getSelection || key === selectionKey(bridge.getSelection()))
      && (!bridge.isStudio || bridge.isStudio());
  }

  function hasFrames(animation) {
    return Array.isArray(animation?.frames) && animation.frames.length > 0;
  }

  function render() {
    const panel = $("visual-animation-settings");
    if (!panel || !bridge) return;
    panel.classList.toggle("is-hidden", !supported());
    const locked = state.working || state.loading;
    $("visual-animation-import").disabled = !supported() || locked;
    $("visual-animation-save").disabled = !supported() || locked || !state.candidate;
    $("visual-animation-reason").textContent = state.selection?.visual_animation?.reason || "";
    const status = $("visual-animation-status");
    status.textContent = state.error || (state.working ? "正在制作并检查动画，请稍等…"
      : state.loading ? "正在读取作品集里的动画…"
      : state.candidate ? `${state.candidate.name || "已导入动画"}。${state.candidate.notice || "可以先播放，再保存到作品集。"}`
      : state.savedAnimation ? "正在显示作品集保存后的实际动态效果。"
      : "导入后先检查播放效果，保存只更新作品集。安装后请在游戏里测试。" );
    status.classList.toggle("is-error", Boolean(state.error));
    const media = $("visual-animation-preview");
    const animation = state.candidate?.animation || state.savedAnimation;
    $("visual-animation-preview-label").textContent = state.candidate ? "待保存的动态效果"
      : state.savedAnimation ? "作品集中的动态效果" : "你的动态效果";
    if (hasFrames(animation)) bridge.renderAnimation(media, animation);
    else bridge.renderAnimationState(media, state.working ? "正在制作动画…"
      : state.loading ? "正在读取动画…" : state.error || "导入动图或视频后，在这里播放和逐帧检查。");
  }

  function leave() {
    state.request += 1;
    state.selection = null;
    state.key = "";
    state.candidate = null;
    state.savedAnimation = null;
    state.working = false;
    state.loading = false;
    state.error = "";
    render();
  }

  async function refresh(selection) {
    const key = selectionKey(selection);
    if (key !== state.key || !supported(selection)) leave();
    state.selection = selection;
    state.key = key;
    if (!selection?.visual_animation?.draft && state.savedAnimation) {
      state.savedAnimation = null;
      state.error = "";
    }
    render();
    if (!supported() || !selection.visual_animation.draft || state.candidate || state.savedAnimation || state.working || state.loading) return;
    const request = ++state.request;
    state.loading = true;
    state.error = "";
    render();
    try {
      const animation = await bridge.call("get_visual_animation_preview", { quiet: true });
      if (!current(request, key)) return;
      if (!hasFrames(animation)) throw new Error(animation?.notice || "作品集动画没有可播放的帧");
      state.savedAnimation = animation;
    } catch (error) {
      if (current(request, key)) {
        state.savedAnimation = null;
        state.error = `作品集动画未加载：${error.message || error}。可以重新导入。`;
      }
    } finally {
      if (current(request, key)) {
        state.loading = false;
        render();
      }
    }
  }

  async function choose() {
    if (!supported() || state.working || state.loading) return;
    const key = state.key;
    const request = ++state.request;
    state.candidate = null;
    state.savedAnimation = null;
    state.error = "";
    state.working = true;
    render();
    try {
      const candidate = await bridge.call("choose_visual_animation", { busy: true, busyText: "制作并检查动态卡面 / 立绘…" });
      if (!current(request, key)) return;
      if (candidate) {
        if (!hasFrames(candidate.animation)) throw new Error("转换后的动画无法预览，请重新导入");
        state.candidate = { ...candidate, sourceKey: key };
        bridge.toast("已生成动态效果，先预览，再保存到作品集");
      }
    } catch (error) {
      if (current(request, key)) {
        state.candidate = null;
        state.error = `导入没有通过：${error.message || error}。请重新选择动图或视频。`;
      }
    } finally {
      if (current(request, key)) {
        state.working = false;
        render();
      }
    }
  }

  async function save() {
    if (!supported() || state.working || state.loading || state.candidate?.sourceKey !== state.key) return;
    const key = state.key;
    const request = ++state.request;
    state.working = true;
    state.error = "";
    render();
    try {
      const data = await bridge.call("save_visual_animation", { busy: true, busyText: "保存动态卡面 / 立绘到作品集…" });
      if (!current(request, key)) return;
      state.candidate = null;
      bridge.onSave?.(data);
      state.selection.visual_animation.draft = true;
      state.savedAnimation = data?.animation || { frames: [] };
      if (!hasFrames(state.savedAnimation)) {
        state.error = `已保存，但预览未加载。${state.savedAnimation.notice || "保存后的动画暂时无法播放。"}可打开作品集重新加载。`;
        bridge.toast("动态作品已保存，预览暂未加载；可以打开作品集重新检查");
        return;
      }
      bridge.toast("已保存到作品集。当前显示保存后的实际效果，安装后请在游戏里测试");
    } catch (error) {
      if (current(request, key)) {
        state.candidate = null;
        state.savedAnimation = null;
        state.error = `保存未能完成检查：${error.message || error}。请重新导入或查看作品集。`;
      }
    } finally {
      if (current(request, key)) {
        state.working = false;
        render();
      }
    }
  }

  function init(options) {
    bridge = options;
    if (state.initialized) return;
    state.initialized = true;
    $("visual-animation-import")?.addEventListener("click", choose);
    $("visual-animation-save")?.addEventListener("click", save);
    render();
  }

  window.VisualAnimation = { init, refresh, leave };
})();
