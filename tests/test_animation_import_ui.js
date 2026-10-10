/* 验证失败的动画导入不能把上一份候选留给保存按钮。 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function makeElement(id = "") {
  const classes = new Set();
  return {
    id, children: [], disabled: false, textContent: "", innerHTML: "",
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
    },
    appendChild(child) { this.children.push(child); },
    addEventListener() {},
    setAttribute() {},
    remove() {},
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
    URLSearchParams, console,
    setTimeout: () => 0, clearTimeout() {},
    window: { location: { hash: "" } },
    document: {
      readyState: "loading", addEventListener() {},
      querySelector: (selector) => find(selector.slice(1)),
      getElementById: find,
      createElement: () => makeElement(),
    },
    fetch: async (url, options) => {
      requests.push({ url, args: JSON.parse(options.body) });
      assert.ok(responses.length, `Unexpected API request: ${url}`);
      return { json: async () => responses.shift() };
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/app.js"), "utf8");
  // 在内存中暴露闭包，不修改生产入口，也不启动页面轮询。
  const instrumented = source.replace(/\}\)\(\);\s*$/, "globalThis.ui = { state, animationImportNotice, syncStudioImportControls, chooseReplacement, commitReplacement, cropReplacement, showDraftDetail, cropReplaceDraft };\n})();");
  vm.runInContext(instrumented, context);
  assert.ok(context.ui, "Frontend test hook was not installed");
  return { ...context.ui, elements, find, requests, responses };
}

async function main() {
  const ui = loadFrontend();
  const clip = {
    asset_type: "anim", animation_kind: "sprite_clip", editable: true,
    bundle: "sample.bundle", name: "Idle", width: 425, height: 470,
    atlas_name: "Idle-atlas", atlas_width: 2048, atlas_height: 2048, atlas_editable: true,
  };
  ui.state.selection = clip;
  ui.state.page = "studio";
  ui.state.replacement = { name: "old-atlas.png", preview_data: "old preview" };
  ui.syncStudioImportControls();
  assert.equal(ui.find("crop-replacement").disabled, true);
  assert.equal(ui.find("crop-replacement").classList.contains("is-hidden"), true);
  assert.equal(ui.find("commit-replacement").disabled, false);
  const notice = ui.animationImportNotice(clip);
  assert.match(notice, /Idle-atlas.*2048 × 2048/);
  assert.doesNotMatch(notice, /425 × 470/);
  assert.match(notice, /碎片布局/);

  ui.responses.push({ ok: false, error: "图片尺寸不匹配，请选择整张原图集" });
  await ui.chooseReplacement();
  assert.equal(ui.state.replacement, null, "Failed import retained the old candidate");
  assert.equal(ui.find("commit-replacement").disabled, true);
  await ui.commitReplacement();
  assert.equal(ui.requests.length, 1, "Saving after rejection still reached the backend");

  ui.responses.push({ ok: true, data: { name: "Idle.animbin", path: "Idle.animbin", preview_data: "" } });
  await ui.chooseReplacement();
  assert.equal(ui.state.replacement.name, "Idle.animbin");
  assert.equal(ui.find("commit-replacement").disabled, false);
  assert.equal(ui.find("crop-replacement").disabled, true);
  await ui.cropReplacement();
  assert.equal(ui.requests.length, 2, "Clip crop reached the backend");

  ui.responses.push({ ok: true, data: null });
  await ui.chooseReplacement();
  assert.equal(ui.state.replacement, null, "Cancelled import retained the old candidate");
  assert.equal(ui.find("commit-replacement").disabled, true);

  ui.state.replacement = { name: "Idle.animbin", path: "Idle.animbin", preview_data: "" };
  ui.responses.push({ ok: false, error: "候选文件已改变" });
  await ui.commitReplacement();
  assert.equal(ui.state.replacement, null, "Rejected save retained a stale candidate");
  assert.equal(ui.find("commit-replacement").disabled, true);

  ui.state.selection = { ...clip, atlas_editable: false, atlas_reason: "此动画使用多张图集" };
  assert.match(ui.animationImportNotice(ui.state.selection), /多张图集.*只能导入同源/);
  ui.state.selection = { asset_type: "texture", editable: true, bundle: "sample.bundle", name: "Portrait" };
  ui.state.replacement = { name: "portrait.png", preview_data: "image data" };
  ui.syncStudioImportControls();
  assert.equal(ui.find("crop-replacement").disabled, false);
  assert.equal(ui.find("crop-replacement").classList.contains("is-hidden"), false);
  ui.responses.push({ ok: false, error: "素材读取失败" });
  await ui.chooseReplacement();
  assert.equal(ui.state.replacement.name, "portrait.png", "Ordinary texture import behavior changed");
  ui.state.selection = { asset_type: "dynamic", animation_kind: "sequence", editable: true };
  ui.syncStudioImportControls();
  assert.equal(ui.find("crop-replacement").classList.contains("is-hidden"), true);
  assert.equal(ui.find("commit-replacement").disabled, false);
  ui.state.page = "pack";
  ui.state.draft = { items: [{ kind: "texture", name: "Idle-atlas", from_anim: "Idle" }] };
  ui.responses.push({ ok: true, data: { item: ui.state.draft.items[0] } });
  await ui.showDraftDetail(0);
  assert.equal(ui.find("draft-crop").disabled, true, "Saved animation atlas still allowed crop");
  assert.match(ui.find("draft-replace").textContent, /整张图集/);
  const requestCount = ui.requests.length;
  await ui.cropReplaceDraft();
  assert.equal(ui.requests.length, requestCount, "Saved animation atlas crop reached the backend");
  ui.state.draft = { items: [{ kind: "texture", name: "Portrait" }] };
  ui.responses.push({ ok: true, data: { item: ui.state.draft.items[0] } });
  await ui.showDraftDetail(0);
  assert.equal(ui.find("draft-crop").disabled, false, "Ordinary saved texture crop became unavailable");
  assert.equal(ui.find("draft-replace").textContent, "换图并保存…");
  console.log("PASS: 动画候选失败/取消/保存失败均清空；无候选不能保存；图集裁剪被拦截；同源动画、普通贴图及独立序列帧控制保持可用。");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
