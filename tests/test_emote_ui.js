/* 故意让导入晚返回，检查静态表情切换槽位后不会保存旧 USM。 */
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
  let markup = "";
  return {
    id, children: [], disabled: false, textContent: "", value: "",
    get innerHTML() { return markup; },
    set innerHTML(value) { markup = value; this.children = []; },
    classList: {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
    },
    appendChild(child) { this.children.push(child); },
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
      assert.ok(responses.length, `Unexpected API request: ${url}`);
      const response = responses.shift();
      return { json: async () => await response };
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/app.js"), "utf8");
  const hooks = "state, renderEmoteSettings, onEmoteVideoChange, chooseEmoteFile, exportEmoteVideo, saveEmote, selectResource, syncStudioImportControls, showDraftDetail, cropReplaceDraft, bindEvents";
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, `globalThis.ui = { ${hooks} };\n})();`), context);
  assert.ok(context.ui, "Frontend test hook was not installed");
  return { ...context.ui, find, requests, responses };
}

function emoteSelection(name = "UT_Item_Emoji_101") {
  return {
    asset_type: "texture", editable: true, bundle: "emoji.bundle", name,
    preview_data: "data:image/png;base64,cover",
    emote: {
      editable: true, emote_id: 101, video_name: "",
      choices: [
        { name: "VEmoji_A", label: "动态 A", affected_ids: [201, 202] },
        { name: "VEmoji_B", label: "动态 B", affected_ids: [301] },
      ],
    },
  };
}

function selectVideo(ui, name) {
  ui.find("emote-video").value = name;
  ui.onEmoteVideoChange();
}

function validFile(name = "new.usm") {
  return {
    name, size: 2097152,
    video_info: {
      format: "usm", width: 512, height: 512, codec: "MPEG-2", has_alpha: true,
      streams: [{ stream: "@SFV", codec_id: 1 }, { stream: "@ALP", codec_id: 1 }],
      preview_available: false,
      notice: "USM 结构和双流布局已校验；当前工具没有 CRI 解码预览，游戏内播放效果仍需测试。",
    },
  };
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
  assert.match(ui.find("emote-current").textContent, /表情 ID：101/);
  assert.equal(ui.find("emote-save").disabled, true);
  selectVideo(ui, "VEmoji_A");
  assert.equal(ui.find("emote-save").disabled, false, "Native reuse required an imported file");
  assert.match(ui.find("emote-shared").textContent, /201、202.*导入 USM.*同时影响/);
  ui.responses.push({ ok: true, data: { draft: { items: [{ kind: "emote", name: "Emoji" }] } } });
  await ui.saveEmote();
  assert.equal(ui.requests[0].url, "/api/save_emote");
  assert.deepEqual(ui.requests[0].args, ["VEmoji_A", false]);
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
  selectVideo(ui, "VEmoji_A");
  ui.responses.push({ ok: true, data: validFile() });
  await ui.chooseEmoteFile();
  assert.equal(ui.state.emoteReplacement.name, "new.usm");
  assert.match(ui.find("emote-file-status").textContent, /new.usm.*2.00 MiB.*512 × 512/);
  assert.match(ui.find("emote-file-status").textContent, /MPEG-2.*编码 ID：1/);
  assert.match(ui.find("emote-file-status").textContent, /双流布局已校验.*没有 CRI 解码预览/);
  assert.equal(ui.state.animationPlayers.size, 0, "USM import rendered a fake animation");
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
  assert.deepEqual(ui.requests.at(-1).args, ["VEmoji_A", true]);
  assert.equal(ui.state.emoteReplacement, null, "Failed save retained a stale USM candidate");
  ui.responses.push({ ok: true, data: validFile() });
  await ui.chooseEmoteFile();
  ui.responses.push({ ok: true, data: { draft: { items: [] } } });
  await ui.saveEmote();
  assert.equal(ui.state.emoteReplacement, null, "Successful save kept the old candidate");
  ui.responses.push({ ok: true, data: { path: "D:/exports/VEmoji_A.usm" } });
  await ui.exportEmoteVideo();
  assert.deepEqual(ui.requests.at(-1).args, ["VEmoji_A"]);
  ui.responses.push({ ok: true, data: { name: "codec-id.usm", size: 1024, video_info: { codec_id: 1 } } });
  await ui.chooseEmoteFile();
  assert.match(ui.find("emote-file-status").textContent, /编码 ID：1/,
    "Top-level codec_id was hidden when a codec label was absent");
}

async function checkLateImports() {
  const ui = loadFrontend();
  ui.state.selection = emoteSelection();
  ui.renderEmoteSettings();
  selectVideo(ui, "VEmoji_A");
  const oldSlot = deferred();
  ui.responses.push(oldSlot.promise);
  const importingSlot = ui.chooseEmoteFile();
  selectVideo(ui, "VEmoji_B");
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
  const detail = { item: ui.state.draft.items[0], original_data: "cover", modified_data: "cover", emote_info: { video_name: "VEmoji_A", custom_video: false } };
  ui.responses.push({ ok: true, data: detail });
  await ui.showDraftDetail(0);
  assert.equal(ui.find("draft-replace").disabled, true);
  assert.equal(ui.find("draft-crop").disabled, true);
  assert.equal(ui.find("draft-remove").disabled, false);
  assert.match(ui.find("draft-mode-notice").textContent, /VEmoji_A.*仅修改当前表情.*封面不是视频预览/);
  const count = ui.requests.length;
  await ui.cropReplaceDraft();
  await ui.find("draft-replace").trigger("click");
  assert.equal(ui.requests.length, count, "Emote draft reached the ordinary crop/image replacement API");
  ui.responses.push({ ok: true, data: { ...detail, emote_info: { video_name: "VEmoji_A", custom_video: true } } });
  await ui.showDraftDetail(0);
  assert.match(ui.find("draft-mode-notice").textContent, /同时替换.*绑定的表情/);
  ui.state.draft = { items: [{ kind: "texture", name: "Portrait" }] };
  ui.responses.push({ ok: true, data: { item: ui.state.draft.items[0] } });
  await ui.showDraftDetail(0);
  assert.equal(ui.find("draft-replace").disabled, false);
  assert.equal(ui.find("draft-crop").disabled, false);
  assert.equal(ui.find("draft-mode-notice").classList.contains("is-hidden"), true);
}

async function main() {
  await checkNativeSelectionAndOrdinaryTexture();
  await checkCandidateResetAndSave();
  await checkLateImports();
  await checkEmoteDraftControls();
  console.log("PASS: 原生动态表情无需导入即可保存；USM 候选取消/失败/切换/保存后清空；迟到导入被丢弃；共享提醒明确；表情作品禁止普通换图裁剪；普通贴图保持可用。");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
