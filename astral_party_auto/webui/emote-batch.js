/* GIF 批量映射先核对再保存；列表只加载当前一项的原图和动图。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    initialized: false, active: false, token: "", sources: [], targets: [], characters: [], rows: [],
    working: "", operation: 0, revision: 0, previewRequest: 0, previewSource: "", previewLoading: false,
    confirmed: false, message: "选择多张 GIF，再为每张指定对应角色的原静态表情。", error: "", draftKey: null, gameKey: null,
  };
  let bridge;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function option(value, label) {
    const node = element("option", "", label);
    node.value = String(value);
    return node;
  }

  function targetFor(row) {
    return state.targets.find((target) => target.emote_id === row.emoteId && target.character_id === row.characterId);
  }

  function mappingState() {
    const occurrences = new Map();
    let mapped = 0;
    for (const row of state.rows) {
      if (!targetFor(row)) continue;
      mapped += 1;
      occurrences.set(row.emoteId, (occurrences.get(row.emoteId) || 0) + 1);
    }
    const duplicates = new Set([...occurrences].filter(([, count]) => count > 1).map(([id]) => id));
    return { mapped, duplicates, complete: Boolean(state.rows.length) && mapped === state.rows.length && !duplicates.size };
  }

  function characterLabel(id) {
    return state.characters.find((character) => character.character_id === id)?.label || `角色 ${id}`;
  }

  function clearPreview(message = "点击一行的「对照预览」，查看原静态表情与待导入 GIF。") {
    state.previewRequest += 1;
    state.previewSource = "";
    state.previewLoading = false;
    for (const id of ["emote-batch-original", "emote-batch-source"]) {
      const container = $(id);
      if (container) {
        container.replaceChildren(element("span", "", "尚未预览"));
        container.removeAttribute("aria-busy");
      }
    }
    if ($("emote-batch-preview-title")) $("emote-batch-preview-title").textContent = "原表情与 GIF 对照";
    if ($("emote-batch-preview-status")) $("emote-batch-preview-status").textContent = message;
  }

  function renderControls() {
    if (!bridge || !$("emote-batch-choose")) return;
    const locked = Boolean(state.working);
    const validation = mappingState();
    $("emote-batch-choose").disabled = locked;
    $("emote-batch-choose").textContent = state.working === "choosing" ? "正在检查 GIF…" : "选择 GIF 动图…";
    $("emote-batch-clear").disabled = locked || !state.token;
    $("emote-batch-clear").classList.toggle("is-hidden", !state.sources.length);
    $("emote-batch-content").classList.toggle("is-hidden", !state.sources.length);
    $("emote-batch-confirm-row").classList.toggle("is-hidden", !state.sources.length);
    $("emote-batch-confirm").disabled = locked || !validation.complete;
    $("emote-batch-confirm").checked = state.confirmed;
    $("emote-batch-save").disabled = locked || !state.token || !validation.complete || !state.confirmed;
    $("emote-batch-save").textContent = state.working === "saving" ? "正在保存整批表情…" : "保存这一批到作品集";
    $("emote-batch-status").textContent = state.error || state.message;
    $("emote-batch-status").classList.toggle("is-error", Boolean(state.error));
    $("emote-batch-progress").classList.toggle("is-hidden", state.working !== "saving");
    const counts = new Map();
    for (const row of state.rows) {
      const target = targetFor(row);
      if (target) counts.set(target.character_id, (counts.get(target.character_id) || 0) + 1);
      if (!row.controls) continue;
      row.controls.character.disabled = locked;
      row.controls.target.disabled = locked || !row.characterId;
      row.controls.preview.disabled = locked || !target || (state.previewLoading && state.previewSource === row.source.source_id);
      row.controls.preview.textContent = state.previewLoading && state.previewSource === row.source.source_id
        ? "正在读取…" : "对照预览";
      const duplicate = validation.duplicates.has(row.emoteId);
      row.controls.item.classList.toggle("is-error", duplicate);
      row.controls.notice.textContent = duplicate ? "同一个原表情被选了多次，请调整。"
        : target ? `${target.label || target.name} · ${target.emote_id}`
        : "请指定角色和原静态表情；未知编号不会自动猜测。";
    }
    const overview = [...counts].map(([id, count]) => `${characterLabel(id)} ${count} 个`).join(" · ");
    $("emote-batch-summary").textContent = !state.rows.length ? ""
      : `${state.rows.length} 张 GIF，已指定 ${validation.mapped} 张${validation.duplicates.size ? "，存在重复目标" : ""}。${overview}`;
  }

  function fillTargets(row) {
    const select = row.controls.target;
    select.replaceChildren(option("", "选择原静态表情"));
    for (const target of state.targets.filter((item) => item.character_id === row.characterId)) {
      select.appendChild(option(target.emote_id, `${target.label || target.name} · ${target.emote_id}`));
    }
    select.value = row.emoteId ? String(row.emoteId) : "";
  }

  function changedMapping() {
    state.confirmed = false;
    state.error = "";
    clearPreview("映射已改变，请重新查看这一项的原图与 GIF 对照。");
    renderControls();
  }

  function renderRows() {
    const list = $("emote-batch-list");
    if (!list) return;
    list.replaceChildren();
    state.rows.forEach((row, index) => {
      const item = element("li", "emote-batch-row");
      const heading = element("div", "emote-batch-file");
      const name = element("strong", "", `${index + 1}. ${row.source.name}`);
      name.title = row.source.name;
      const seconds = (Number(row.source.duration_ms || 0) / 1000).toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
      const size = (Number(row.source.size || 0) / 1024).toFixed(0);
      heading.appendChild(name);
      heading.appendChild(element("span", "", `${row.source.frames} 帧 · ${seconds} 秒 · ${row.source.width}×${row.source.height} · ${size} KiB`));
      item.appendChild(heading);
      const fields = element("div", "emote-batch-fields");
      const characterLabelNode = element("label", "", "对应角色");
      const character = element("select");
      character.setAttribute("aria-label", `${row.source.name}：对应角色`);
      character.appendChild(option("", "选择角色"));
      state.characters.forEach((entry) => character.appendChild(option(entry.character_id, entry.label)));
      character.value = row.characterId ? String(row.characterId) : "";
      characterLabelNode.appendChild(character);
      const targetLabelNode = element("label", "", "要替换的静态表情");
      const target = element("select");
      target.setAttribute("aria-label", `${row.source.name}：原静态表情`);
      targetLabelNode.appendChild(target);
      fields.appendChild(characterLabelNode);
      fields.appendChild(targetLabelNode);
      const preview = element("button", "button ghost compact", "对照预览");
      preview.type = "button";
      preview.setAttribute("aria-label", `对照预览 ${row.source.name}`);
      fields.appendChild(preview);
      item.appendChild(fields);
      const notice = element("p", "emote-batch-row-notice");
      item.appendChild(notice);
      row.controls = { item, character, target, preview, notice };
      fillTargets(row);
      character.addEventListener("change", () => {
        if (state.working) return;
        row.characterId = Number(character.value) || null;
        row.emoteId = null;
        fillTargets(row);
        changedMapping();
      });
      target.addEventListener("change", () => {
        if (state.working) return;
        const identifier = Number(target.value);
        row.emoteId = state.targets.some((entry) => entry.emote_id === identifier && entry.character_id === row.characterId)
          ? identifier : null;
        changedMapping();
      });
      preview.addEventListener("click", () => previewRow(row));
      list.appendChild(item);
    });
    renderControls();
  }

  function validatedBatch(batch) {
    if (!batch || typeof batch.token !== "string" || !batch.token || !Array.isArray(batch.sources)
      || !batch.sources.length || batch.sources.length > 64 || !Array.isArray(batch.targets) || !batch.targets.length) {
      throw new Error("GIF 预检结果不完整，请重新选择文件。");
    }
    const identifiers = new Set();
    for (const source of batch.sources) {
      if (typeof source.source_id !== "string" || !source.source_id || identifiers.has(source.source_id)
        || typeof source.name !== "string" || !Number.isInteger(source.frames) || source.frames < 2) {
        throw new Error("GIF 预检结果缺少可用动图，请重新选择文件。");
      }
      identifiers.add(source.source_id);
    }
    if (batch.targets.some((target) => !Number.isInteger(target.emote_id) || !Number.isInteger(target.character_id))) {
      throw new Error("原静态表情目录不完整，请重新检查游戏资源。");
    }
    const targets = batch.targets;
    const characters = Array.isArray(batch.characters) ? batch.characters.filter((entry) =>
      Number.isInteger(entry.character_id) && targets.some((target) => target.character_id === entry.character_id))
      .map((entry) => ({ ...entry, label: entry.label || `角色 ${entry.character_id}` })) : [];
    for (const target of targets) {
      if (!characters.some((entry) => entry.character_id === target.character_id)) {
        characters.push({ character_id: target.character_id, label: target.character || `角色 ${target.character_id}` });
      }
    }
    return { sources: batch.sources, targets, characters };
  }

  async function choose() {
    if (!bridge || state.working || !state.active) return;
    const operation = ++state.operation;
    const revision = state.revision;
    const view = state.previewRequest;
    state.working = "choosing";
    state.error = "";
    state.message = "正在逐张检查 GIF 和当前游戏的原静态表情…";
    renderControls();
    try {
      const batch = await bridge.api("choose_emote_gifs");
      if (operation !== state.operation || revision !== state.revision) return;
      if (!batch) {
        state.message = state.sources.length ? "已取消选择，原来的待保存列表仍保留。" : "已取消选择，尚未导入 GIF。";
        return;
      }
      if (!state.active || view !== state.previewRequest) {
        invalidate("文件选择期间已离开此页，请重新选择 GIF 后核对。");
        return;
      }
      const checked = validatedBatch(batch);
      state.token = batch.token;
      state.sources = checked.sources;
      state.targets = checked.targets;
      state.characters = checked.characters;
      state.rows = checked.sources.map((source) => {
        const suggested = checked.targets.find((target) => target.emote_id === source.suggested_emote_id);
        return { source, characterId: suggested?.character_id || null, emoteId: suggested?.emote_id || null, controls: null };
      });
      state.confirmed = false;
      state.message = "已读取 GIF。只有完整编号命中的文件会预选目标，请逐项对照并确认表达。";
      clearPreview();
      renderRows();
    } catch (error) {
      if (operation === state.operation && revision === state.revision && state.active) {
        state.error = error?.message || "GIF 未通过检查，请重新选择。";
        bridge.toast(state.error, true);
      }
    } finally {
      if (operation === state.operation) state.working = "";
      renderControls();
    }
  }

  function showImage(container, data, label) {
    if (typeof data !== "string" || !/^data:image\/(?:png|gif|webp|jpeg);base64,/.test(data)) {
      container.replaceChildren(element("span", "", "预览未能读取，请重试"));
      return false;
    }
    const image = element("img");
    image.alt = label;
    image.src = data;
    container.replaceChildren(image);
    return true;
  }

  async function previewRow(row) {
    const target = targetFor(row);
    if (!bridge || !state.active || state.working || !state.token || !target) return;
    if (state.previewLoading && state.previewSource === row.source.source_id) return;
    const request = ++state.previewRequest;
    const revision = state.revision;
    const token = state.token;
    const emoteId = row.emoteId;
    const current = () => request === state.previewRequest && revision === state.revision
      && state.active && token === state.token && row.emoteId === emoteId && targetFor(row)?.emote_id === emoteId;
    state.previewSource = row.source.source_id;
    state.previewLoading = true;
    $("emote-batch-preview-title").textContent = `${row.source.name} → ${target.label || target.name}`;
    $("emote-batch-preview-status").textContent = "正在读取当前这一项的原图和 GIF…";
    for (const id of ["emote-batch-original", "emote-batch-source"]) {
      $(id).replaceChildren(element("span", "", "正在读取…"));
      $(id).setAttribute("aria-busy", "true");
    }
    renderControls();
    try {
      const data = await bridge.api("preview_emote_gif_batch", token, row.source.source_id, emoteId);
      if (!current()) return;
      if (data?.source?.source_id !== row.source.source_id || data?.target?.emote_id !== emoteId) {
        throw new Error("预览对应关系不一致，请重新读取这一项");
      }
      const original = showImage($("emote-batch-original"), data?.original_data, `${target.label || target.name}：原静态表情`);
      const source = showImage($("emote-batch-source"), data?.source_data, `${row.source.name}：待导入 GIF`);
      $("emote-batch-preview-status").textContent = original && source
        ? `目标编号 ${emoteId}。请核对角色、情绪和动作含义；这里播放的是输入 GIF，保存后还可在作品集检查生成结果。`
        : "部分预览未能读取，请重试；不要用原静态图判断动态播放效果。";
    } catch (error) {
      if (!current()) return;
      for (const id of ["emote-batch-original", "emote-batch-source"]) {
        $(id).replaceChildren(element("span", "", "预览未完成"));
      }
      $("emote-batch-preview-status").textContent = `预览未完成：${error?.message || error}。可以重新点击对照预览。`;
    } finally {
      if (current()) {
        state.previewLoading = false;
        $("emote-batch-original").removeAttribute("aria-busy");
        $("emote-batch-source").removeAttribute("aria-busy");
        renderControls();
      }
    }
  }

  async function save() {
    if (!bridge || !state.active || state.working || !state.token || !mappingState().complete || !state.confirmed) return;
    const operation = ++state.operation;
    const revision = state.revision;
    const token = state.token;
    const mappings = state.rows.map((row) => ({ source_id: row.source.source_id, emote_id: row.emoteId }));
    state.working = "saving";
    state.error = "";
    clearPreview("正在转换、回读检查并保存整批表情…");
    state.message = "正在保存整批表情；全部通过后才会更新作品集，请等待处理完成。";
    $("emote-batch-progress").removeAttribute("value");
    renderControls();
    try {
      const result = await bridge.api("save_emote_gif_batch", token, mappings);
      if (operation !== state.operation || revision !== state.revision || token !== state.token) return;
      if (!result?.draft || result.count !== mappings.length) {
        throw new Error("保存结果不完整，请刷新作品集核对实际保存内容");
      }
      const count = mappings.length;
      invalidate(`已将 ${count} 个动态表情保存到作品集。尚未安装到游戏，可在下方预览、导出或手动安装。`);
      bridge.onSaved(result);
      bridge.toast(`已保存 ${count} 个动态表情，尚未安装到游戏`);
    } catch (error) {
      if (operation === state.operation && revision === state.revision && token === state.token) {
        state.confirmed = false;
        state.error = `${error?.message || "本批保存未完成"}。请按提示处理，再重新核对或选择 GIF。`;
        bridge.toast(state.error, true);
      }
    } finally {
      if (operation === state.operation) state.working = "";
      renderControls();
    }
  }

  function progress(payload) {
    if (state.working !== "saving" || payload?.token && payload.token !== state.token) return false;
    const total = Number(payload?.total);
    const done = Number(payload?.done);
    if (Number.isFinite(total) && total > 0 && Number.isFinite(done)) {
      $("emote-batch-progress").max = total;
      $("emote-batch-progress").value = Math.max(0, Math.min(done, total));
    }
    const stage = { validating: "检查映射", convert: "制作动画", converting: "制作动画",
      verify: "回读校验", verifying: "回读校验", commit: "保存作品集", committing: "保存作品集" }[payload?.stage] || "处理表情";
    const count = total > 0 && Number.isFinite(done) ? ` ${Math.max(0, Math.min(done, total))}/${total}` : "";
    state.message = `${stage}${count}${payload?.label ? ` · ${String(payload.label).slice(0, 160)}` : ""}`;
    renderControls();
    return state.message;
  }

  function invalidate(message = "游戏位置或作品集已变化，请重新选择 GIF 后核对映射。") {
    state.revision += 1;
    state.token = "";
    state.sources = [];
    state.targets = [];
    state.characters = [];
    state.rows = [];
    state.confirmed = false;
    state.error = "";
    state.message = message;
    clearPreview();
    renderRows();
  }

  function updateDraft(draft) {
    const key = JSON.stringify(draft || {});
    if (state.draftKey !== null && key !== state.draftKey && state.token) invalidate("作品集已变化，请重新选择 GIF 后核对映射。");
    state.draftKey = key;
  }

  function updateGame(executable) {
    const key = String(executable || "");
    if (state.gameKey !== null && key !== state.gameKey && (state.token || state.working)) {
      invalidate("游戏连接已变化，请重新选择 GIF 后核对映射。");
    }
    state.gameKey = key;
  }

  function enter() {
    state.active = true;
    renderControls();
  }

  function leave() {
    state.active = false;
    clearPreview("回到此页后可重新查看对照；保存前请确认映射。");
    renderControls();
  }

  function open() {
    enter();
    $("emote-batch")?.scrollIntoView({ block: "start" });
    $("emote-batch-choose")?.focus();
  }

  function init(context) {
    bridge = context;
    if (!state.initialized) {
      state.initialized = true;
      $("emote-batch-choose")?.addEventListener("click", choose);
      $("emote-batch-clear")?.addEventListener("click", () => {
        if (!state.working) invalidate("已取消这一批待保存的 GIF，作品集保持原样。");
      });
      $("emote-batch-confirm")?.addEventListener("change", (event) => {
        state.confirmed = !state.working && mappingState().complete && event.target.checked;
        renderControls();
      });
      $("emote-batch-save")?.addEventListener("click", save);
    }
    renderControls();
  }

  window.EmoteBatch = { init, enter, leave, open, invalidate, updateDraft, updateGame, progress };
})();
