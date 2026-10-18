/* 固定裁剪框的几何、整像素提交与异步取消；不启动真实文件对话框。 */
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

function eventTarget() {
  const handlers = new Map();
  return {
    addEventListener(name, handler) {
      if (!handlers.has(name)) handlers.set(name, new Set());
      handlers.get(name).add(handler);
    },
    removeEventListener(name, handler) { handlers.get(name)?.delete(handler); },
    async trigger(name, event = {}) {
      for (const handler of [...(handlers.get(name) || [])]) await handler(event);
    },
    listenerCount(name) { return handlers.get(name)?.size || 0; },
  };
}

function makeElement() {
  const classes = new Set();
  return {
    ...eventTarget(), style: {}, dataset: {}, value: "", disabled: false,
    clientWidth: 656, clientHeight: 456,
    classList: {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name), toggle() {},
    },
    setAttribute() {}, removeAttribute(name) { delete this[name]; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    appendChild() {}, remove() {}, focus() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight }; },
  };
}

function loadUi() {
  const elements = new Map();
  const find = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };
  const loaders = [];
  const requests = [];
  const responses = [];
  const document = {
    ...eventTarget(), readyState: "loading", body: makeElement(),
    querySelector: (selector) => find(selector.slice(1)), querySelectorAll: () => [],
    getElementById: find, createElement: makeElement,
  };
  const window = { ...eventTarget(), location: { hash: "" } };
  const context = vm.createContext({
    window, document, URLSearchParams, console, setTimeout: () => 0, clearTimeout() {},
    Image: class {
      constructor() { loaders.push(this); }
      finish(width, height) { this.naturalWidth = width; this.naturalHeight = height; this.onload?.(); }
    },
    fetch: async (url, options) => {
      requests.push({ url, args: JSON.parse(options.body) });
      assert.ok(responses.length, `Unexpected API: ${url}`);
      const response = responses.shift();
      return { json: async () => ({ ok: true, data: await response }) };
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/app.js"), "utf8");
  const hooks = "state, bindEvents, openCropModal, closeCropModal, cropFrameGeometry, cropBoxInPixels, setCropZoom, resetCropFull, resizeCropStage, startCropDrag, onCropPointerMove, cropReplacement, cropReplaceDraft";
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, `globalThis.ui = { ${hooks}, get geometry() { return cropState; }, get pending() { return cropPending; } };\n})();`), context);
  const ui = context.ui;
  ui.bindEvents();
  return { ui, find, loaders, requests, responses, document, window };
}

function box(ui) { return Array.from(ui.cropBoxInPixels(ui.geometry)); }
function near(actual, expected, tolerance = 1e-7) { assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} != ${expected}`); }
function checkCovered(ui) {
  const g = ui.geometry;
  assert.ok(g.imageX <= g.frame.left + 1e-7);
  assert.ok(g.imageY <= g.frame.top + 1e-7);
  assert.ok(g.imageX + g.natW * g.scale >= g.frame.left + g.frame.width - 1e-7);
  assert.ok(g.imageY + g.natH * g.scale >= g.frame.top + g.frame.height - 1e-7);
  const [l, t, r, b] = box(ui);
  assert.ok([l, t, r, b].every(Number.isInteger));
  assert.ok(l >= 0 && t >= 0 && r <= g.natW && b <= g.natH && r > l && b > t);
}

async function checkGeometryAndGestures() {
  const { ui, find, loaders, document } = loadUi();
  ui.openCropModal({ url: "landscape", targetW: 400, targetH: 400, onConfirm() {} });
  loaders.at(-1).finish(1600, 900);
  assert.deepEqual(box(ui), [350, 0, 1250, 900]);
  const frame = { ...ui.geometry.frame };
  ui.setCropZoom(2);
  assert.deepEqual(box(ui), [575, 225, 1025, 675]);
  assert.deepEqual({ ...ui.geometry.frame }, frame, "Zoom resized the fixed frame");
  ui.startCropDrag({ pointerId: 1, clientX: 0, clientY: 0, button: 0, preventDefault() {} });
  ui.onCropPointerMove({ pointerId: 1, clientX: 1e6, clientY: 1e6 });
  assert.deepEqual(box(ui), [0, 0, 450, 450]);
  ui.onCropPointerMove({ pointerId: 1, clientX: -1e6, clientY: -1e6 });
  assert.deepEqual(box(ui), [1150, 450, 1600, 900]);
  checkCovered(ui);
  assert.deepEqual({ ...ui.geometry.frame }, frame, "Dragging moved the fixed frame");
  const beforeResize = box(ui);
  find("crop-stage").clientWidth = 376;
  find("crop-stage").clientHeight = 300;
  ui.resizeCropStage();
  assert.deepEqual(box(ui), beforeResize, "Viewport resize changed the selected source pixels");
  assert.equal(document.listenerCount("pointermove"), 0, "Resize left an active drag");
  ui.resetCropFull();
  assert.deepEqual(box(ui), [350, 0, 1250, 900]);
  await find("crop-zoom").trigger("input", { target: { value: "300" } });
  near(ui.geometry.zoom, 3);
  await find("crop-stage").trigger("wheel", { clientX: 180, clientY: 140, deltaY: -120, preventDefault() {} });
  assert.ok(ui.geometry.zoom > 3);
  checkCovered(ui);
  ui.closeCropModal();
  assert.equal(ui.geometry, null);
  assert.equal(document.listenerCount("pointermove"), 0);
}

function checkAspectMatrix() {
  const { ui, loaders, find } = loadUi();
  for (const [width, height] of [[1600, 900], [900, 1600], [12000, 40], [40, 12000], [1, 100], [100, 1], [1, 1], [11, 7]]) {
    for (const [targetW, targetH] of [[400, 400], [1920, 1080], [200, 900], [0, 0]]) {
      ui.openCropModal({ url: `${width}-${height}`, targetW, targetH, onConfirm() {} });
      loaders.at(-1).finish(width, height);
      const ratio = targetW ? targetW / targetH : width / height;
      near(ui.geometry.frame.width / ui.geometry.frame.height, ratio);
      if (!targetW) assert.deepEqual(box(ui), [0, 0, width, height]);
      for (const zoom of [0.1, 1, 1.25, 4, 800]) {
        ui.setCropZoom(zoom);
        checkCovered(ui);
        assert.ok(ui.geometry.zoom >= 1 && ui.geometry.zoom <= ui.geometry.maxZoom);
        for (const value of [-1e9, 1e9]) {
          ui.geometry.imageX = value;
          ui.geometry.imageY = value;
          // A zoom update also clamps deliberately excessive translations.
          ui.setCropZoom(ui.geometry.zoom);
          checkCovered(ui);
        }
      }
      if (Math.min(width, height) === 1) {
        assert.equal(ui.geometry.zoom, 1);
        assert.equal(find("crop-zoom").disabled, true);
        assert.match(find("crop-info").textContent, /整像素/);
      }
    }
  }
}

async function checkLoadingAndCancellation() {
  const { ui, loaders, find, document } = loadUi();
  let canceled = 0;
  ui.openCropModal({ url: "old", onConfirm() {}, onCancel() { canceled += 1; } });
  const staleLoad = loaders.at(-1).onload;
  ui.closeCropModal();
  ui.openCropModal({ url: "new", onConfirm() {} });
  staleLoad();
  assert.equal(ui.geometry, null, "Canceled load initialized a new dialog");
  loaders.at(-1).finish(1024, 768);
  assert.equal(find("crop-image").src, "new");
  ui.startCropDrag({ pointerId: 1, clientX: 0, clientY: 0, preventDefault() {} });
  await document.trigger("keydown", { key: "Escape" });
  assert.equal(ui.pending, null);
  assert.equal(document.listenerCount("pointermove"), 0);
  assert.equal(canceled, 1);
  ui.openCropModal({ url: "broken", onConfirm() {} });
  loaders.at(-1).onerror();
  assert.equal(ui.pending, null);
  assert.equal(find("crop-modal").classList.contains("is-hidden"), true);
  let current = true;
  ui.openCropModal({ url: "moved", onConfirm() {}, isCurrent: () => current });
  current = false;
  loaders.at(-1).finish(100, 100);
  assert.equal(ui.pending, null, "Resource changed while image loaded");
}

async function checkEntryPointSessions() {
  const { ui, find, loaders, requests, responses } = loadUi();
  ui.state.page = "studio";
  ui.state.selection = { kind: "texture", width: 400, height: 400 };
  ui.state.replacement = { name: "candidate.png", preview_data: "candidate", crop_token: "replace-token" };
  await ui.cropReplacement();
  loaders.at(-1).finish(1600, 900);
  // Hold the backend response, leave/re-enter the same resource, then finish.
  const lateCrop = deferred();
  responses.push(lateCrop.promise);
  const confirming = find("crop-confirm").trigger("click");
  assert.deepEqual(requests.at(-1).args, [[350, 0, 1250, 900], "replace-token"]);
  ui.state.studioRequest += 1;
  ui.state.replacement = { name: "new-candidate.png" };
  lateCrop.resolve({ name: "stale.png", crop_token: "stale-token" });
  await confirming;
  assert.equal(ui.state.replacement.name, "new-candidate.png");
  ui.state.replacement = { name: "same.png", preview_data: "same", crop_token: "same-token" };
  await ui.cropReplacement();
  loaders.at(-1).finish(1600, 900);
  const savedAway = deferred();
  responses.push(savedAway.promise);
  const finishAway = find("crop-confirm").trigger("click");
  ui.state.page = "dashboard";
  ui.state.studioRequest += 1;
  savedAway.resolve({ name: "same-cropped.png", preview_data: "saved", crop_token: "saved-token" });
  await finishAway;
  assert.equal(ui.state.replacement.name, "same-cropped.png", "Successful crop was lost when leaving the page");
  assert.equal(ui.state.replacement.crop_token, "saved-token");
  ui.state.page = "pack";
  ui.state.draftIndex = 0;
  ui.state.draft = { items: [{ kind: "texture", name: "A" }, { kind: "texture", name: "B" }] };
  const latePicker = deferred();
  responses.push(latePicker.promise);
  const picking = ui.cropReplaceDraft();
  ui.state.draftIndex = 1;
  responses.push(true);
  latePicker.resolve({ preview_data: "stale", crop_token: "draft-old" });
  await picking;
  assert.equal(ui.pending, null);
  assert.equal(requests.at(-1).url, "/api/cancel_draft_crop");
  assert.deepEqual(requests.at(-1).args, ["draft-old"]);
  responses.push({ preview_data: "draft", target_width: 400, target_height: 400, crop_token: "draft-current" });
  await ui.cropReplaceDraft();
  loaders.at(-1).finish(1600, 900);
  responses.push(true);
  await find("crop-cancel").trigger("click");
  assert.equal(requests.at(-1).url, "/api/cancel_draft_crop");
  assert.deepEqual(requests.at(-1).args, ["draft-current"]);
  responses.push({ preview_data: "draft-commit", target_width: 400, target_height: 400, crop_token: "draft-commit" });
  await ui.cropReplaceDraft();
  loaders.at(-1).finish(1600, 900);
  const savedDraft = deferred();
  responses.push(savedDraft.promise);
  const finishDraft = find("crop-confirm").trigger("click");
  assert.deepEqual(requests.at(-1).args, [[350, 0, 1250, 900], "draft-commit"]);
  ui.state.page = "dashboard";
  ui.state.selection = null;
  savedDraft.resolve({ draft: { items: [{ kind: "texture", name: "saved-after-leave" }] } });
  await finishDraft;
  assert.equal(ui.state.draft.items[0].name, "saved-after-leave", "Saved draft was discarded after navigation");
}

(async () => {
  await checkGeometryAndGestures();
  checkAspectMatrix();
  await checkLoadingAndCancellation();
  await checkEntryPointSessions();
  console.log("PASS: 固定比例、拖图/缩放边界、32组横竖/小图、像素坐标、缩放/视窗重置、取消加载、两入口令牌与迟到回调。");
})().catch((error) => { console.error(error); process.exitCode = 1; });
