/* 直接静态→独立动态；真实成品回读、异步切换和旧作品兼容边界。 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function makeElement(id = "") {
  const classes = new Set();
  const handlers = new Map();
  const parts = new Map();
  let markup = "";
  return {
    id, children: [], disabled: false, textContent: "", value: "", dataset: {}, isConnected: true,
    get innerHTML() { return markup; },
    set innerHTML(value) { markup = value; this.children = []; parts.clear(); },
    classList: {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
    },
    appendChild(child) { this.children.push(child); },
    querySelector(selector) {
      if (!parts.has(selector)) {
        const child = makeElement();
        const action = selector.match(/data-animation="([^"]+)"/);
        if (action) child.dataset.animation = action[1];
        parts.set(selector, child);
      }
      return parts.get(selector);
    },
    querySelectorAll(selector) {
      return selector === "[data-animation]" ? ["previous", "play", "next"].map((action) =>
        this.querySelector(`[data-animation="${action}"]`)) : [];
    },
    addEventListener(name, handler) { handlers.set(name, handler); },
    trigger(name, event = {}) { return handlers.get(name)?.(event); },
    setAttribute() {}, removeAttribute() {}, remove() {},
  };
}

function loadFrontend() {
  const elements = new Map();
  const find = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  };
  const requests = [];
  const responses = [];
  const previewResponses = [];
  const context = vm.createContext({
    URLSearchParams, console, setTimeout: () => 0, clearTimeout() {},
    window: { location: { hash: "" }, addEventListener() {} },
    document: {
      readyState: "loading", addEventListener() {}, querySelectorAll: () => [],
      querySelector: (selector) => find(selector.slice(1)), getElementById: find,
      createElement: () => makeElement(),
    },
    fetch: async (url, options) => {
      requests.push({ url, args: JSON.parse(options.body) });
      if (url === "/api/get_emote_preview") {
        const response = previewResponses.length ? previewResponses.shift()
          : { ok: true, data: animation("official") };
        return { json: async () => await response };
      }
      assert.ok(responses.length, `Unexpected API request: ${url}`);
      const response = responses.shift();
      return { json: async () => await response };
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/app.js"), "utf8");
  const hooks = "state, renderEmoteSettings, onEmoteVideoChange, loadEmotePreview, renderPreview, loadBrowseAnimation, refreshStudio, chooseEmoteFile, exportEmoteVideo, saveEmote, selectResource, syncStudioImportControls, showDraftDetail, cropReplaceDraft, bindEvents";
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, `globalThis.ui = { ${hooks} };\n})();`), context);
  assert.ok(context.ui, "Frontend test hook was not installed");
  return { ...context.ui, find, requests, responses, previewResponses };
}

function emoteSelection(name = "UT_Item_Emoji_101") {
  return {
    asset_type: "texture", editable: true, bundle: "emoji.bundle", name,
    preview_data: "data:image/png;base64,cover",
    emote: {
      editable: true, emote_id: 101, video_name: "", default_video_name: "VEmoji_A", independent_supported: true,
      choices: [
        { name: "VEmoji_A", label: "动态 A", affected_ids: [201, 202] },
        { name: "VEmoji_B", label: "动态 B", affected_ids: [301] },
      ],
    },
  };
}

async function selectVideo(ui, name) {
  ui.find("emote-video").value = name;
  await ui.onEmoteVideoChange();
}

function animation(prefix) {
  return { frames: [`${prefix}-frame-1`, `${prefix}-frame-2`], durations: [100, 150],
    total: 2, total_duration_ms: 250, width: 512, height: 512, fps: 8 };
}

function validFile(name = "new.usm") {
  return {
    name, independent: true, video_name: "VEmoji_A", source_name: "我的表情.gif", size: 2097152, animation: animation("converted"),
    notice: "已转换并回读实际动态表情。",
    video_info: {
      format: "usm", width: 512, height: 512, codec: "MPEG-1", has_alpha: true,
      streams: [{ stream: "@SFV", codec_id: 1 }, { stream: "@ALP", codec_id: 1 }],
      preview_available: true,
    },
  };
}

function checkMarkupContract() {
  const html = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/index.html"), "utf8");
  for (const id of ["emote-settings", "emote-video", "emote-original", "emote-replacement", "emote-shared-ids",
    "emote-import", "emote-save", "emote-export", "emote-native-options", "emote-shared-details",
    "studio-original-heading", "studio-replacement-heading"]) {
    assert.equal((html.match(new RegExp(`id="${id}"`, "g")) || []).length, 1, `Missing or duplicate UI control: ${id}`);
  }
  for (const phrase of ["自定义动态表情", "直接导入动图或视频", "自动识别并转换", "普通视频背景会保留，不会自动抠图", "也可使用游戏已有动图", "当前表情原版"]) {
    assert.ok(html.includes(phrase), `Missing product guidance: ${phrase}`);
  }
  assert.ok(!/动态视频槽位|不能把 GIF 直接转换|暂不支持在工具内播放 USM|先选择一个游戏动态表情/.test(html), "An import prerequisite remained in product guidance");
  const nativeDetails = html.match(/<details id="emote-native-options"[^>]*>/)?.[0];
  assert.ok(nativeDetails && !/\sopen(?:\s|=|>)/.test(nativeDetails), "Existing emote options are not initially collapsed");
  assert.ok(html.indexOf('id="emote-settings"') < html.indexOf('id="studio-image-mode"'),
    "Static covers appeared before the actual dynamic emote workbench");
}

async function checkNativeSelectionAndOrdinaryTexture() {
  const ui = loadFrontend();
  ui.state.selection = { asset_type: "texture", editable: true, bundle: "portrait.bundle", name: "Portrait" };
  ui.state.replacement = { name: "portrait.png", preview_data: "portrait" };
  ui.renderEmoteSettings();
  ui.syncStudioImportControls();
  assert.equal(ui.find("emote-settings").classList.contains("is-hidden"), true);
  assert.equal(ui.find("crop-replacement").disabled, false);
  await ui.saveEmote();
  assert.equal(ui.requests.length, 0, "Ordinary texture reached the emote API");

  ui.state.selection = emoteSelection();
  ui.renderEmoteSettings();
  assert.equal(ui.find("emote-settings").classList.contains("is-hidden"), false);
  assert.equal(ui.find("emote-video").children.length, 3, "Native options were not built");
  assert.equal(ui.find("emote-video").value, "");
  assert.match(ui.find("emote-current").textContent, /原来的表情：静态图片/);
  assert.doesNotMatch(ui.find("emote-current").textContent, /VEmoji|表情 ID/);
  assert.equal(ui.find("emote-save").disabled, true);
  assert.equal(ui.find("emote-import").disabled, false, "Static emote required a native choice before importing");
  assert.ok(ui.find("emote-original").innerHTML.includes("cover"));
  await selectVideo(ui, "VEmoji_A");
  assert.equal(ui.find("emote-save").disabled, false, "Native reuse required an imported file");
  assert.match(ui.find("emote-shared").textContent, /仅修改当前表情.*原生动图保留/);
  assert.match(ui.find("emote-shared-ids").textContent, /201、202/);
  ui.responses.push({ ok: true, data: { draft: { items: [{ kind: "emote", name: "Emoji" }] } } });
  await ui.saveEmote();
  const save = ui.requests.find((request) => request.url === "/api/save_emote");
  assert.deepEqual(save.args, ["VEmoji_A", false]);
  assert.equal(ui.state.draft.items[0].kind, "emote");
  assert.equal(ui.state.replacement.name, "portrait.png", "Emote save cleared the ordinary texture candidate");

  ui.state.selection = { ...emoteSelection(), emote: { ...emoteSelection().emote, editable: false, reason: "此表情没有可改的配置" } };
  ui.renderEmoteSettings();
  ui.syncStudioImportControls();
  for (const id of ["emote-video", "emote-export", "emote-import", "emote-save"]) assert.equal(ui.find(id).disabled, true);
  assert.match(ui.find("emote-reason").textContent, /没有可改/);
  assert.equal(ui.find("crop-replacement").disabled, false, "Read-only emote disabled ordinary texture editing");
  const count = ui.requests.length;
  await ui.chooseEmoteFile();
  await ui.exportEmoteVideo();
  await ui.saveEmote();
  assert.equal(ui.requests.length, count, "Read-only emote reached a mutation API");
}

async function checkCandidateResetAndSave() {
  const ui = loadFrontend();
  ui.state.selection = emoteSelection();
  ui.renderEmoteSettings();
  await selectVideo(ui, "VEmoji_A");
  ui.responses.push({ ok: true, data: validFile() });
  await ui.chooseEmoteFile();
  assert.equal(ui.state.emoteReplacement.name, "new.usm");
  assert.match(ui.find("emote-file-status").textContent, /我的表情.gif.*2.00 MiB.*512 × 512/);
  assert.doesNotMatch(ui.find("emote-file-status").textContent, /MPEG-2|编码 ID|new.usm/);
  assert.equal(ui.state.animationPlayers.get("emote-replacement").animation.frames[0], "converted-frame-1",
    "Imported preview did not use the actual decoded replacement frames");
  ui.responses.push({ ok: true, data: null });
  await ui.chooseEmoteFile();
  assert.equal(ui.state.emoteReplacement, null, "Cancel retained the previous USM candidate");
  ui.responses.push({ ok: true, data: validFile() });
  await ui.chooseEmoteFile();
  ui.responses.push({ ok: false, error: "视频结构不兼容" });
  await ui.chooseEmoteFile();
  assert.equal(ui.state.emoteReplacement, null, "Failed import retained the previous USM candidate");
  ui.responses.push({ ok: true, data: validFile() });
  await ui.chooseEmoteFile();
  ui.responses.push({ ok: false, error: "候选文件内容已改变" });
  await ui.saveEmote();
  assert.deepEqual(ui.requests.filter((request) => request.url === "/api/save_emote").at(-1).args, ["VEmoji_A", true]);
  assert.equal(ui.state.emoteReplacement, null, "Failed save retained a stale USM candidate");
  ui.responses.push({ ok: true, data: validFile() });
  await ui.chooseEmoteFile();
  ui.responses.push({ ok: true, data: { item: { video_name: "VEmoji_Mod_101" }, draft: { items: [] } } });
  await ui.saveEmote();
  assert.equal(ui.state.emoteReplacement, null, "Successful save kept the old candidate");
  await selectVideo(ui, "VEmoji_A");
  ui.responses.push({ ok: true, data: { path: "D:/exports/VEmoji_A.usm" } });
  await ui.exportEmoteVideo();
  assert.deepEqual(ui.requests.at(-1).args, ["VEmoji_A"]);
  ui.responses.push({ ok: true, data: { name: "codec-id.usm", size: 1024, video_info: { codec_id: 1 } } });
  await ui.chooseEmoteFile();
  assert.doesNotMatch(ui.find("emote-file-status").textContent, /编码 ID|MPEG-2/,
    "Internal codec details remained prominent in the import workflow");
}

async function checkLateImports() {
  const ui = loadFrontend();
  ui.state.selection = emoteSelection();
  ui.renderEmoteSettings();
  await selectVideo(ui, "VEmoji_A");
  const oldSlot = deferred();
  ui.responses.push(oldSlot.promise);
  const importingSlot = ui.chooseEmoteFile();
  await selectVideo(ui, "VEmoji_B");
  oldSlot.resolve({ ok: true, data: validFile("old-slot.usm") });
  await importingSlot;
  assert.equal(ui.state.emoteReplacement, null, "Late candidate crossed into a different video slot");

  const first = deferred();
  ui.responses.push(first.promise, { ok: true, data: validFile("latest.usm") });
  const importingFirst = ui.chooseEmoteFile();
  await ui.chooseEmoteFile();
  first.resolve({ ok: true, data: validFile("first.usm") });
  await importingFirst;
  assert.equal(ui.state.emoteReplacement.name, "latest.usm", "Older same-slot import replaced the latest candidate");

  const oldResource = deferred();
  ui.responses.push(oldResource.promise, { ok: true, data: emoteSelection("UT_Item_Emoji_102") });
  const importingResource = ui.chooseEmoteFile();
  await ui.selectResource("emoji.bundle", "UT_Item_Emoji_102");
  oldResource.resolve({ ok: true, data: validFile("old-resource.usm") });
  await importingResource;
  assert.equal(ui.state.selection.name, "UT_Item_Emoji_102");
  assert.equal(ui.state.emoteReplacement, null, "Late candidate crossed into a different resource");
  ui.renderEmoteSettings();
  assert.equal(ui.state.emoteVideoName, "", "New resource inherited the previous slot");
}

async function checkEmoteDraftControls() {
  const ui = loadFrontend();
  ui.bindEvents();
  ui.state.page = "pack";
  ui.state.draft = { items: [{ kind: "emote", name: "Emoji" }] };
  const detail = { item: ui.state.draft.items[0], original_data: "cover", modified_data: "cover",
    emote_info: { video_name: "VEmoji_A", video_label: "芬妮 · 动态表情 1", custom_video: false },
    emote_animation: animation("saved"), emote_original_animation: animation("original") };
  ui.responses.push({ ok: true, data: detail });
  await ui.showDraftDetail(0);
  assert.equal(ui.find("draft-replace").disabled, true);
  assert.equal(ui.find("draft-crop").disabled, true);
  assert.equal(ui.find("draft-remove").disabled, false);
  assert.match(ui.find("draft-mode-notice").textContent, /芬妮.*只更改当前表情/);
  assert.doesNotMatch(ui.find("draft-mode-notice").textContent, /VEmoji_A/);
  assert.equal(ui.state.animationPlayers.get("draft-modified").animation.frames[0], "saved-frame-1");
  assert.equal(ui.state.animationPlayers.get("draft-original").animation.frames[0], "original-frame-1");
  const count = ui.requests.length;
  await ui.cropReplaceDraft();
  await ui.find("draft-replace").trigger("click");
  assert.equal(ui.requests.length, count, "Emote draft reached the ordinary crop/image replacement API");
  ui.responses.push({ ok: true, data: { ...detail, emote_info: { video_name: "VEmoji_A", custom_video: true } } });
  await ui.showDraftDetail(0);
  assert.match(ui.find("draft-mode-notice").textContent, /其他.*也会一起变/);
  ui.responses.push({ ok: true, data: { ...detail, item: { ...detail.item, independent_video: true },
    emote_info: { custom_video: true, independent_video: true } } });
  await ui.showDraftDetail(0);
  assert.match(ui.find("draft-mode-notice").textContent, /仅修改当前表情.*原生动图保留/);
  assert.doesNotMatch(ui.find("draft-mode-notice").textContent, /也会一起变|VEmoji/);
  ui.state.draft = { items: [{ kind: "texture", name: "Portrait" }] };
  ui.responses.push({ ok: true, data: { item: ui.state.draft.items[0] } });
  await ui.showDraftDetail(0);
  assert.equal(ui.find("draft-replace").disabled, false);
  assert.equal(ui.find("draft-crop").disabled, false);
  assert.equal(ui.find("draft-mode-notice").classList.contains("is-hidden"), true);
}

async function checkOfficialAutoSelectionAndPlayback() {
  const ui = loadFrontend();
  const official = emoteSelection();
  official.emote.video_name = "VEmoji_A";
  official.emote.choices[0].label = "芬妮 · 动态表情 1";
  ui.state.page = "studio";
  ui.state.selection = official;
  ui.responses.push({ ok: true, data: official });
  ui.previewResponses.push({ ok: true, data: animation("official-original") },
    { ok: true, data: animation("official-draft") });
  await ui.refreshStudio();
  assert.equal(ui.find("emote-video").value, "", "The optional native picker implicitly prepared a replacement");
  assert.equal(ui.find("emote-save").disabled, true, "Original playback implicitly enabled native binding save");
  assert.match(ui.find("emote-current").textContent, /芬妮 · 动态表情 1/);
  assert.equal(ui.find("emote-original").querySelector("img").src, "official-original-frame-1");
  assert.equal(ui.find("emote-replacement").querySelector("img").src, "official-draft-frame-1");
  assert.match(ui.find("emote-original").querySelector(".animation-position").textContent, /0.25 秒/,
    "Official preview ignored the decoded frame durations");
  const media = ui.find("emote-original");
  await media.querySelector('[data-animation="play"]').trigger("click");
  assert.equal(ui.state.animationPlayers.get("emote-original").playing, false);
  await media.querySelector('[data-animation="next"]').trigger("click");
  assert.equal(media.querySelector("img").src, "official-original-frame-2", "Frame stepping did not use decoded frames");
  assert.equal(ui.find("studio-original-heading").textContent, "原静态封面");
  assert.equal(ui.find("choose-replacement").textContent, "更换静态封面…");

  const browse = loadFrontend();
  browse.state.page = "browse";
  browse.responses.push({ ok: true, data: official });
  browse.previewResponses.push({ ok: true, data: animation("browse-original") });
  await browse.selectResource(official.bundle, official.name);
  assert.equal(browse.find("preview-media").querySelector("img").src, "browse-original-frame-1");
  assert.equal(browse.find("preview-title").textContent, "芬妮 · 动态表情 1");
  assert.ok(browse.requests.some((request) => request.url === "/api/get_emote_preview" && request.args[1] === "original"));
  assert.equal(browse.requests.some((request) => request.url === "/api/get_animation_preview"), false,
    "Native video preview used the unrelated Unity animation endpoint");
}

async function checkLateOriginalAndDraftPreviews() {
  const ui = loadFrontend();
  ui.state.page = "studio";
  ui.state.selection = emoteSelection();
  ui.state.selection.emote.video_name = "VEmoji_A";
  ui.renderEmoteSettings();
  const oldOriginal = deferred();
  const oldDraft = deferred();
  ui.previewResponses.push(oldOriginal.promise, oldDraft.promise);
  const oldPreview = selectVideo(ui, "VEmoji_A");
  ui.previewResponses.push({ ok: true, data: animation("latest-original") },
    { ok: true, data: animation("latest-draft") });
  await selectVideo(ui, "VEmoji_B");
  oldOriginal.resolve({ ok: true, data: animation("old-original") });
  oldDraft.resolve({ ok: true, data: animation("old-draft") });
  await oldPreview;
  assert.equal(ui.state.emotePreview.frames[0], "latest-original-frame-1");
  assert.equal(ui.state.emoteDraftPreview.frames[0], "latest-draft-frame-1");
  assert.equal(ui.find("emote-original").querySelector("img").src, "latest-original-frame-1",
    "Late official preview crossed into another selected game emote");
  assert.equal(ui.requests.some((request) => request.url === "/api/get_emote_preview" && request.args[1] === "original" && request.args[0] !== "VEmoji_A"), false,
    "Changing the optional native choice replaced the original emote identity");

  const staleResource = deferred();
  const staleDraft = deferred();
  ui.previewResponses.push(staleResource.promise, staleDraft.promise);
  const loadingResource = ui.loadEmotePreview();
  ui.state.selectionRequest += 1;
  ui.state.selection = emoteSelection("UT_Item_Emoji_102");
  ui.renderEmoteSettings();
  staleResource.resolve({ ok: true, data: animation("stale-resource") });
  staleDraft.resolve({ ok: true, data: animation("stale-draft") });
  await loadingResource;
  assert.equal(ui.state.emotePreview, null);
  assert.equal(ui.state.emoteDraftPreview, null);
  assert.equal(ui.state.animationPlayers.has("emote-original"), false,
    "Late video preview appeared after the resource changed");
}

async function checkConvertedPlaybackAndSavedReadback() {
  const ui = loadFrontend();
  ui.state.page = "studio";
  ui.state.selection = emoteSelection();
  ui.renderEmoteSettings();
  await selectVideo(ui, "VEmoji_A");
  ui.responses.push({ ok: true, data: validFile() });
  await ui.chooseEmoteFile();
  const media = ui.find("emote-replacement");
  assert.equal(media.querySelector("img").src, "converted-frame-1");
  await media.querySelector('[data-animation="next"]').trigger("click");
  assert.equal(media.querySelector("img").src, "converted-frame-2");
  assert.equal(ui.state.animationPlayers.get("emote-replacement").playing, false);
  ui.renderEmoteSettings();
  assert.equal(media.querySelector("img").src, "converted-frame-2", "Status refresh restarted the inspected replacement frame");
  assert.equal(ui.state.animationPlayers.get("emote-replacement").playing, false,
    "Status refresh resumed a preview that the user had paused");
  ui.responses.push({ ok: true, data: { item: { video_name: "VEmoji_Mod_101" }, draft: { items: [{ kind: "emote", name: "Emoji" }] } } });
  ui.previewResponses.push({ ok: true, data: animation("verified-saved") });
  await ui.saveEmote();
  assert.equal(ui.state.emoteReplacement, null);
  assert.equal(ui.state.emoteDraftPreview.frames[0], "verified-saved-frame-1",
    "Saved preview kept the old candidate instead of reading the real draft");
  assert.equal(media.querySelector("img").src, "verified-saved-frame-1");
  assert.deepEqual(ui.requests.at(-1), { url: "/api/get_emote_preview", args: ["VEmoji_Mod_101", "draft"] });

  assert.equal(ui.find("emote-save").disabled, true, "A repeated save would silently overwrite the independent draft with a native animation");
  await selectVideo(ui, "VEmoji_A");
  ui.responses.push({ ok: true, data: { draft: { items: [] } } });
  ui.previewResponses.push({ ok: false, error: "保存内容暂时无法解码" });
  await ui.saveEmote();
  assert.equal(ui.state.emoteReplacement, null);
  assert.equal(ui.state.emoteDraftPreview, null, "Failed readback kept the previously displayed success preview");
  assert.match(ui.state.emoteDraftError, /无法解码/);
  assert.equal(ui.state.animationPlayers.has("emote-replacement"), false);
}

async function checkSaveWhileOriginalPreviewLoads() {
  const ui = loadFrontend();
  ui.state.page = "studio";
  ui.state.selection = emoteSelection();
  ui.state.selection.emote.video_name = "VEmoji_A";
  ui.renderEmoteSettings();
  const original = deferred();
  const oldDraft = deferred();
  ui.previewResponses.push(original.promise, oldDraft.promise);
  const loadingOriginal = selectVideo(ui, "VEmoji_A");
  ui.responses.push({ ok: true, data: { draft: { items: [] } } });
  ui.previewResponses.push({ ok: true, data: animation("just-saved") });
  await ui.saveEmote();
  assert.equal(ui.state.emoteDraftPreview.frames[0], "just-saved-frame-1");
  assert.equal(ui.state.emotePreviewLoading, true);
  original.resolve({ ok: true, data: animation("late-current-original") });
  oldDraft.resolve({ ok: true, data: animation("old-unsaved-draft") });
  await loadingOriginal;
  assert.equal(ui.state.emotePreview.frames[0], "late-current-original-frame-1",
    "Draft readback discarded the original preview for the same selected emote");
  assert.equal(ui.state.emoteDraftPreview.frames[0], "just-saved-frame-1",
    "Late draft preview replaced the result read back after saving");
  assert.equal(ui.state.emotePreviewLoading, false);
}

async function checkDirectIndependentImportWithoutNativeChoice() {
  const ui = loadFrontend();
  ui.state.page = "studio";
  ui.state.selection = emoteSelection();
  ui.state.selection.emote.choices = [];
  ui.renderEmoteSettings();
  assert.equal(ui.find("emote-import").disabled, false);
  assert.equal(ui.find("emote-video").value, "");
  assert.equal(ui.find("emote-original").classList.contains("has-animation"), false);
  assert.ok(ui.find("emote-original").innerHTML.includes("cover"), "Static original was replaced by a reference animation");
  ui.responses.push({ ok: true, data: validFile() });
  await ui.chooseEmoteFile();
  assert.deepEqual(ui.requests.at(-1), { url: "/api/choose_emote_file", args: ["VEmoji_A"] });
  assert.equal(ui.state.emoteReplacement.templateName, "VEmoji_A");
  assert.equal(ui.find("emote-save").disabled, false, "Independent candidate still required the native dropdown");
  assert.equal(ui.find("emote-shared-details").classList.contains("is-hidden"), true);
  assert.equal(ui.state.animationPlayers.get("emote-replacement").animation.frames[0], "converted-frame-1");
  assert.equal(ui.requests.some((request) => request.url === "/api/get_emote_preview"), false,
    "Import loaded the reference animation as the original or candidate");
  ui.responses.push({ ok: true, data: { item: { video_name: "VEmoji_Mod_Independent", independent_video: true }, draft: { items: [] } } });
  ui.previewResponses.push({ ok: true, data: animation("independent-saved") });
  await ui.saveEmote();
  assert.deepEqual(ui.requests.find((request) => request.url === "/api/save_emote").args, ["VEmoji_A", true]);
  assert.deepEqual(ui.requests.at(-1), { url: "/api/get_emote_preview", args: ["VEmoji_Mod_Independent", "draft"] });
  assert.equal(ui.state.emoteReplacement, null);
  assert.equal(ui.state.emoteDraftVideoName, "VEmoji_Mod_Independent");
  assert.equal(ui.state.emoteVideoName, "");
  assert.equal(ui.state.emoteDraftPreview.frames[0], "independent-saved-frame-1");
  assert.ok(ui.find("emote-original").innerHTML.includes("cover"));
  assert.match(ui.find("emote-file-status").textContent, /已保存到作品集/);
}

async function checkCandidateUsesItsOwnTemplateAndSource() {
  const ui = loadFrontend();
  ui.state.page = "studio";
  ui.state.selection = emoteSelection();
  ui.state.selection.emote.default_video_name = "VEmoji_B";
  ui.renderEmoteSettings();
  await selectVideo(ui, "VEmoji_A");
  ui.responses.push({ ok: true, data: { ...validFile(), video_name: "VEmoji_B" } });
  await ui.chooseEmoteFile();
  assert.deepEqual(ui.requests.at(-1), { url: "/api/choose_emote_file", args: ["VEmoji_B"] });
  ui.renderEmoteSettings();
  assert.equal(ui.state.emoteVideoName, "VEmoji_A");
  assert.equal(ui.state.emoteReplacement.templateName, "VEmoji_B", "Dropdown value invalidated an independent template");
  ui.responses.push({ ok: true, data: { item: { video_name: "VEmoji_Mod_FromB" }, draft: { items: [] } } });
  await ui.saveEmote();
  assert.deepEqual(ui.requests.find((request) => request.url === "/api/save_emote").args, ["VEmoji_B", true]);
  assert.deepEqual(ui.requests.at(-1), { url: "/api/get_emote_preview", args: ["VEmoji_Mod_FromB", "draft"] });
  assert.equal(ui.state.selection.emote.video_name, "", "Conversion changed the original emote identity");
  assert.equal(ui.requests.some((request) => request.url === "/api/get_emote_preview" && request.args[1] === "original"), false,
    "Static original displayed the native dropdown or template video");
}

async function checkIndependentReadbackAfterReentryAndRemoval() {
  const ui = loadFrontend();
  ui.state.page = "studio";
  const selection = emoteSelection();
  selection.emote.draft_video_name = "VEmoji_Mod_Reopened";
  ui.state.selection = selection;
  ui.responses.push({ ok: true, data: selection });
  ui.previewResponses.push({ ok: true, data: animation("reopened") });
  await ui.refreshStudio();
  assert.equal(ui.state.emoteVideoName, "");
  assert.deepEqual(ui.requests.at(-1), { url: "/api/get_emote_preview", args: ["VEmoji_Mod_Reopened", "draft"] });
  assert.equal(ui.state.emoteDraftPreview.frames[0], "reopened-frame-1");
  ui.responses.push({ ok: true, data: { ...selection, emote: { ...selection.emote, draft_video_name: "" } } });
  await ui.refreshStudio();
  assert.equal(ui.state.emoteDraftVideoName, "");
  assert.equal(ui.state.emoteDraftPreview, null, "Removed independent draft retained an old animation");
  assert.equal(ui.state.animationPlayers.has("emote-replacement"), false);
  assert.equal(ui.requests.at(-1).url, "/api/get_studio_state", "Removed static emote loaded a template preview");
}

async function checkLateIndependentReadbackCannotCrossResources() {
  const ui = loadFrontend();
  ui.state.page = "studio";
  ui.state.selection = emoteSelection();
  ui.renderEmoteSettings();
  ui.responses.push({ ok: true, data: validFile() });
  await ui.chooseEmoteFile();
  ui.responses.push({ ok: true, data: { item: { video_name: "VEmoji_Mod_Late" }, draft: { items: [] } } });
  const oldReadback = deferred();
  ui.previewResponses.push(oldReadback.promise);
  const saving = ui.saveEmote();
  await new Promise(setImmediate);
  assert.equal(ui.requests.at(-1).args[0], "VEmoji_Mod_Late");
  ui.state.page = "browse";
  ui.responses.push({ ok: true, data: emoteSelection("UT_Item_Emoji_102") });
  await ui.selectResource("emoji.bundle", "UT_Item_Emoji_102");
  oldReadback.resolve({ ok: true, data: animation("wrong-resource") });
  await saving;
  assert.equal(ui.state.selection.name, "UT_Item_Emoji_102");
  assert.equal(ui.state.emoteReplacement, null);
  assert.equal(ui.state.emoteDraftVideoName, "");
  assert.equal(ui.state.emoteDraftPreview, null);
  assert.equal(ui.state.animationPlayers.has("emote-replacement"), false);
}

async function checkExplicitNativeChoicePreviewSurvivesStudioRefresh() {
  const ui = loadFrontend();
  ui.state.page = "studio";
  const selection = emoteSelection();
  selection.emote.draft_video_name = "VEmoji_Mod_Previous";
  ui.state.selection = selection;
  ui.renderEmoteSettings();
  await selectVideo(ui, "VEmoji_B");
  ui.responses.push({ ok: true, data: selection });
  ui.previewResponses.push({ ok: true, data: animation("native-pending") });
  await ui.refreshStudio();
  assert.equal(ui.state.emoteVideoName, "VEmoji_B");
  assert.deepEqual(ui.requests.at(-1), { url: "/api/get_emote_preview", args: ["VEmoji_B", "draft"] });
  assert.equal(ui.state.emoteDraftPreview.frames[0], "native-pending-frame-1");
  assert.match(ui.find("emote-file-status").textContent, /保存后使用所选游戏动图/);
  assert.doesNotMatch(ui.find("emote-file-status").textContent, /已保存/);
}

async function main() {
  checkMarkupContract();
  await checkNativeSelectionAndOrdinaryTexture();
  await checkCandidateResetAndSave();
  await checkLateImports();
  await checkEmoteDraftControls();
  await checkOfficialAutoSelectionAndPlayback();
  await checkLateOriginalAndDraftPreviews();
  await checkConvertedPlaybackAndSavedReadback();
  await checkSaveWhileOriginalPreviewLoads();
  await checkDirectIndependentImportWithoutNativeChoice();
  await checkCandidateUsesItsOwnTemplateAndSource();
  await checkIndependentReadbackAfterReentryAndRemoval();
  await checkLateIndependentReadbackCannotCrossResources();
  await checkExplicitNativeChoicePreviewSurvivesStudioRefresh();
  console.log("PASS: 静态表情无须原生下拉即可直接导入独立动图；原版仍显示当前封面/真实原动态；候选按自身来源与模板保存；独立别名保存回读/重新进入/删除/迟到请求正确；暂停逐帧、旧作品共享提醒及普通贴图边界保持可用。");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
