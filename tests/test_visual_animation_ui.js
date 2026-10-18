/* 静态卡面 / 立绘转动态：真实回读、候选失效及异步返回隔离。 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function element(id) {
  const classes = new Set();
  const handlers = new Map();
  const parts = new Map();
  let markup = "";
  return {
    id, textContent: "", disabled: false, value: "", dataset: {}, children: [], isConnected: true,
    get innerHTML() { return markup; },
    set innerHTML(value) { markup = value; this.children = []; parts.clear(); },
    classList: {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
    },
    addEventListener: (name, handler) => handlers.set(name, handler),
    trigger: (name) => handlers.get(name)?.(),
    appendChild(child) { this.children.push(child); },
    querySelector(selector) {
      if (!parts.has(selector)) {
        const child = element("");
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
    setAttribute() {}, removeAttribute() {}, remove() {},
  };
}

function animation(name) {
  return { frames: [`${name}-1`, `${name}-2`], durations: [50, 150], width: 64, height: 64 };
}

function selection(name = "Card_A", changes = {}) {
  return { asset_type: "texture", bundle: "cards.bundle", name,
    visual_animation: { editable: true, experimental: true, label: "动态卡面", tables: ["Card"], draft: false, reason: "共享此图片的界面会一起改变。" },
    ...changes };
}

function loadUi() {
  const elements = new Map();
  const find = (id) => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  const requests = [], responses = [], rendered = [], saved = [], toasts = [];
  let selected = null, active = true;
  const context = vm.createContext({ window: {}, document: { getElementById: find }, console });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/visual-animation.js"), "utf8");
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, "globalThis.ui = { state, choose, save };\n})();"), context);
  const editor = context.window.VisualAnimation;
  editor.init({
    async call(name, options) {
      requests.push(name);
      assert.ok(responses.length, `Unexpected request: ${name}`);
      const response = await responses.shift();
      if (response instanceof Error) throw response;
      return response;
    },
    getSelection: () => selected, isStudio: () => active,
    renderAnimation: (media, value) => rendered.push({ id: media.id, animation: value }),
    renderAnimationState: (media, message) => rendered.push({ id: media.id, message }),
    onSave: (data) => saved.push(data), toast: (message) => toasts.push(message),
  });
  return { ...context.ui, editor, find, requests, responses, rendered, saved, toasts,
    async refresh(value) { selected = value; return editor.refresh(value); },
    leave() { active = false; editor.leave(); },
    enter() { active = true; },
  };
}

function checkMarkup() {
  const html = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/index.html"), "utf8");
  for (const id of ["visual-animation-settings", "visual-animation-preview-label", "visual-animation-preview", "visual-animation-reason",
    "visual-animation-status", "visual-animation-import", "visual-animation-save"]) {
    assert.equal((html.match(new RegExp(`id="${id}"`, "g")) || []).length, 1, `${id} is missing or duplicated`);
  }
  for (const phrase of ["让这张图片动起来（试用）", "游戏内效果尚未验证", "GIF、APNG、WebP、MP4、WebM", "手动安装测试"]) {
    assert.ok(html.includes(phrase), `Missing guidance: ${phrase}`);
  }
  assert.ok(html.indexOf('src="visual-animation.js"') < html.indexOf('src="app.js"'));
  const app = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/app.js"), "utf8");
  assert.match(app, /window\.VisualAnimation\?\.refresh\(sel\)/);
  assert.match(app, /window\.VisualAnimation\?\.leave\(\)/);
  assert.match(app, /window\.VisualAnimation\?\.init\(/);
}

async function checkSupportedScope() {
  const ui = loadUi();
  for (const value of [null, selection("map", { visual_animation: { editable: false } }),
    selection("text", { asset_type: "text" }), selection("mesh", { asset_type: "mesh" }), selection("emoji", { emote: { editable: true } })]) {
    await ui.refresh(value);
    assert.equal(ui.find("visual-animation-settings").classList.contains("is-hidden"), true);
    await ui.choose();
    await ui.save();
  }
  assert.deepEqual(ui.requests, []);
  await ui.refresh(selection());
  assert.equal(ui.find("visual-animation-settings").classList.contains("is-hidden"), false);
  assert.equal(ui.find("visual-animation-import").disabled, false);
  assert.equal(ui.find("visual-animation-save").disabled, true);
}

async function checkCandidateInvalidation() {
  const ui = loadUi();
  await ui.refresh(selection());
  ui.responses.push({ name: "good.gif", animation: animation("converted"), notice: "已从生成资源回读。" });
  await ui.choose();
  assert.equal(ui.state.candidate.name, "good.gif");
  assert.equal(ui.find("visual-animation-save").disabled, false);
  ui.responses.push(new Error("解码失败"));
  await ui.choose();
  assert.equal(ui.state.candidate, null);
  assert.equal(ui.find("visual-animation-save").disabled, true);
  const calls = ui.requests.length;
  await ui.save();
  assert.equal(ui.requests.length, calls, "Invalid candidate reached save API");
  assert.match(ui.find("visual-animation-status").textContent, /解码失败/);
  for (const bad of [null, { name: "empty.gif", animation: { frames: [] } }]) {
    ui.responses.push({ name: "second.gif", animation: animation("second") });
    await ui.choose();
    ui.responses.push(bad);
    await ui.choose();
    assert.equal(ui.state.candidate, null);
    assert.equal(ui.find("visual-animation-save").disabled, true);
  }
}

async function checkSaveReadsActualOutput() {
  const ui = loadUi();
  await ui.refresh(selection());
  ui.responses.push({ name: '<img src=x onerror="bad()">.gif', animation: animation("candidate") });
  await ui.choose();
  assert.match(ui.find("visual-animation-status").textContent, /<img src=x/);
  const data = { item: { kind: "visual_animation" }, animation: animation("saved-readback"), draft: { items: [{ kind: "visual_animation" }] } };
  ui.responses.push(data);
  await ui.save();
  assert.equal(ui.state.candidate, null);
  assert.equal(ui.state.savedAnimation, data.animation);
  assert.equal(ui.rendered.at(-1).animation, data.animation);
  assert.equal(ui.saved[0], data);
  assert.equal(ui.find("visual-animation-save").disabled, true);
  assert.match(ui.find("visual-animation-preview-label").textContent, /作品集/);
}

async function checkSaveFailureCannotReuseCandidate() {
  const ui = loadUi();
  await ui.refresh(selection());
  ui.responses.push({ name: "old.gif", animation: animation("candidate") });
  await ui.choose();
  ui.responses.push(new Error("文件已改变"));
  await ui.save();
  assert.equal(ui.state.candidate, null);
  assert.equal(ui.state.savedAnimation, null);
  assert.equal(ui.find("visual-animation-save").disabled, true);
  const calls = ui.requests.length;
  await ui.save();
  assert.equal(ui.requests.length, calls);
}

async function checkCommittedSaveWithUnavailablePreview() {
  const ui = loadUi();
  const source = selection();
  await ui.refresh(source);
  ui.responses.push({ name: "good.webp", animation: animation("candidate") });
  await ui.choose();
  const saved = { item: { kind: "visual_animation" }, draft: { items: [{ kind: "visual_animation" }] },
    animation: { frames: [], notice: "动态作品已经保存，重新加载预览失败：解码超时。" } };
  ui.responses.push(saved);
  await ui.save();
  assert.equal(ui.saved[0], saved, "Committed draft did not reach the app");
  assert.equal(source.visual_animation.draft, true);
  assert.equal(ui.state.candidate, null);
  assert.equal(ui.state.savedAnimation, saved.animation);
  assert.equal(ui.find("visual-animation-save").disabled, true);
  assert.match(ui.find("visual-animation-status").textContent, /已保存，但预览未加载/);
  assert.match(ui.find("visual-animation-status").textContent, /解码超时/);
  assert.doesNotMatch(ui.find("visual-animation-status").textContent, /保存未能完成检查/);
  assert.match(ui.toasts.at(-1), /已保存/);
  const calls = ui.requests.length;
  await ui.save();
  assert.equal(ui.requests.length, calls, "Committed candidate could be saved twice");
}

async function checkRemovedSavedAnimationIsCleared() {
  const ui = loadUi();
  ui.responses.push(animation("saved"));
  await ui.refresh(selection("Card_A", { visual_animation: { editable: true, draft: true } }));
  assert.equal(ui.state.savedAnimation.frames[0], "saved-1");
  await ui.refresh(selection("Card_A", { visual_animation: { editable: true, draft: false } }));
  assert.equal(ui.state.savedAnimation, null);
  assert.equal(ui.rendered.at(-1).animation, undefined, "Removed animation still rendered");
  assert.equal(ui.find("visual-animation-save").disabled, true);
  assert.equal(ui.find("visual-animation-import").disabled, false);
}

async function checkSavedPreviewAndFailure() {
  const ui = loadUi();
  const savedSelection = selection("Card_A", { visual_animation: { editable: true, draft: true } });
  ui.responses.push(animation("existing"));
  await ui.refresh(savedSelection);
  assert.equal(ui.state.savedAnimation.frames[0], "existing-1");
  assert.equal(ui.find("visual-animation-save").disabled, true);
  const calls = ui.requests.length;
  await ui.refresh(savedSelection);
  assert.equal(ui.requests.length, calls, "Refresh loaded the same saved preview repeatedly");
  ui.responses.push(new Error("损坏的草稿"));
  await ui.refresh(selection("Card_B", { visual_animation: { editable: true, draft: true } }));
  assert.equal(ui.state.savedAnimation, null);
  assert.match(ui.find("visual-animation-status").textContent, /损坏的草稿/);
  assert.equal(ui.find("visual-animation-import").disabled, false);
}

async function checkLateImportAndPageLeave() {
  const ui = loadUi();
  await ui.refresh(selection("Card_A"));
  const late = deferred();
  ui.responses.push(late.promise);
  const importA = ui.choose();
  assert.equal(ui.find("visual-animation-save").disabled, true);
  assert.equal(ui.find("visual-animation-import").disabled, true);
  await ui.refresh(selection("Card_B"));
  late.resolve({ name: "A.gif", animation: animation("A") });
  await importA;
  assert.equal(ui.state.candidate, null);
  assert.equal(ui.state.key.includes("Card_B"), true);
  const latePage = deferred();
  ui.responses.push(latePage.promise);
  const importing = ui.choose();
  ui.leave();
  latePage.resolve({ name: "B.gif", animation: animation("B") });
  await importing;
  assert.equal(ui.state.candidate, null);
  assert.equal(ui.find("visual-animation-settings").classList.contains("is-hidden"), true);
}

async function checkLateSavedPreviewCannotReplaceNewCandidate() {
  const ui = loadUi();
  const late = deferred();
  ui.responses.push(late.promise);
  const loading = ui.refresh(selection("Card_A", { visual_animation: { editable: true, draft: true } }));
  await ui.refresh(selection("Card_B"));
  ui.responses.push({ name: "B.gif", animation: animation("B") });
  await ui.choose();
  late.resolve(animation("A-saved"));
  await loading;
  assert.equal(ui.state.candidate.name, "B.gif");
  assert.equal(ui.state.savedAnimation, null);
  assert.equal(ui.rendered.at(-1).animation.frames[0], "B-1");
}

async function checkLateSaveCannotReplaceNewSelection() {
  const ui = loadUi();
  await ui.refresh(selection("Card_A"));
  ui.responses.push({ name: "A.gif", animation: animation("A") });
  await ui.choose();
  const late = deferred();
  ui.responses.push(late.promise);
  const saving = ui.save();
  await ui.refresh(selection("Card_B"));
  late.resolve({ animation: animation("A-saved"), draft: { items: [{ name: "A" }] } });
  await saving;
  assert.equal(ui.state.candidate, null);
  assert.equal(ui.state.savedAnimation, null);
  assert.equal(ui.saved.length, 0);
}

async function checkProductionAppIntegration() {
  const elements = new Map();
  const find = (id) => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  const responses = new Map();
  const answer = (name, value) => responses.set(name, [...(responses.get(name) || []), value]);
  const context = vm.createContext({
    console, URLSearchParams, setTimeout: () => 0, clearTimeout() {},
    window: { location: { hash: "" }, addEventListener() {}, removeEventListener() {} },
    document: {
      readyState: "loading", addEventListener() {}, removeEventListener() {}, body: element("body"),
      querySelector: (selector) => find(selector.slice(1)), querySelectorAll: () => [],
      getElementById: find, createElement: () => element(""),
    },
    async fetch(url) {
      const name = url.split("/").at(-1);
      const queue = responses.get(name);
      assert.ok(queue?.length, `Unexpected API request: ${name}`);
      return { json: async () => ({ ok: true, data: await queue.shift() }) };
    },
  });
  const folder = path.join(__dirname, "../astral_party_auto/webui");
  vm.runInContext(fs.readFileSync(path.join(folder, "visual-animation.js"), "utf8"), context);
  const app = fs.readFileSync(path.join(folder, "app.js"), "utf8");
  vm.runInContext(app.replace(/\}\)\(\);\s*$/, "globalThis.appUi = { state, refreshStudio, showDraftDetail, selectResource, bindEvents };\n})();"), context);
  const ui = context.appUi;
  ui.bindEvents();
  ui.state.page = "studio";
  const source = selection("Card_A", { editable: true, preview_data: "data:image/png;base64,official", caption: "卡面" });
  answer("get_studio_state", source);
  await ui.refreshStudio();
  assert.equal(find("visual-animation-settings").classList.contains("is-hidden"), false);
  assert.equal(find("choose-replacement").classList.contains("is-hidden"), false, "Static replacement entry disappeared");
  assert.equal(find("emote-settings").classList.contains("is-hidden"), true, "Card import used the emote chain");
  answer("choose_visual_animation", { name: "test.webp", animation: animation("production-candidate") });
  await find("visual-animation-import").trigger("click");
  assert.equal(ui.state.animationPlayers.get("visual-animation-preview").animation.frames[0], "production-candidate-1");
  const actual = animation("production-saved");
  const item = { kind: "visual_animation", name: "Card_A", bundle: "config.bundle", note: "test.webp" };
  answer("save_visual_animation", { item, animation: actual, draft: { name: "测试作品", items: [item] } });
  await find("visual-animation-save").trigger("click");
  assert.equal(ui.state.draft.items[0], item);
  assert.equal(ui.state.animationPlayers.get("visual-animation-preview").animation, actual);
  assert.match(find("draft-list").children[0].innerHTML, /\[动\]/);
  context.window.VisualAnimation.leave();
  ui.state.page = "pack";
  answer("get_draft_detail", { item, original_data: source.preview_data, modified_animation: actual,
    visual_info: { label: "动态卡面（试用）", notice: "从作品集回读，游戏内效果需测试。" } });
  await ui.showDraftDetail(0);
  assert.match(find("draft-detail-title").textContent, /动态卡面（试用）/);
  assert.ok(find("draft-original").innerHTML.includes(source.preview_data));
  assert.equal(ui.state.animationPlayers.get("draft-modified").animation, actual);
  assert.equal(find("draft-replace").disabled, true);
  assert.equal(find("draft-crop").disabled, true);
  assert.equal(find("draft-remove").disabled, false);
  assert.match(find("draft-mode-notice").textContent, /回读/);
  ui.state.page = "studio";
  const nextSelection = deferred();
  answer("select_asset", nextSelection.promise);
  const selecting = ui.selectResource("cards.bundle", "Card_B");
  assert.equal(find("visual-animation-save").disabled, true, "New resource request did not clear the animation candidate");
  nextSelection.resolve(selection("Card_B", { editable: true }));
  await selecting;
  assert.equal(find("visual-animation-settings").classList.contains("is-hidden"), true);
}

async function main() {
  checkMarkup();
  for (const check of [checkSupportedScope, checkCandidateInvalidation, checkSaveReadsActualOutput, checkSaveFailureCannotReuseCandidate,
    checkCommittedSaveWithUnavailablePreview, checkRemovedSavedAnimationIsCleared,
    checkSavedPreviewAndFailure, checkLateImportAndPageLeave, checkLateSavedPreviewCannotReplaceNewCandidate, checkLateSaveCannotReplaceNewSelection,
    checkProductionAppIntegration]) {
    await check();
  }
  console.log("PASS: 动态卡面/立绘仅在已核对资源显示；取消、失败、切资源、切页和迟到响应清空候选；保存显示实际回读并更新作品集。");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
