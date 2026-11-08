/* 角色动画导入参数、取消、异步预览与离页恢复回归。 */
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

function element(id, tag = "div") {
  const handlers = new Map();
  const classes = new Set();
  return {
    id, tag, children: [], textContent: "", value: "", disabled: false, checked: false,
    classList: {
      contains: (name) => classes.has(name),
      toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
    },
    appendChild(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    querySelectorAll(selector) { return selector === "button" ? this.children.filter((child) => child.tag === "button") : []; },
    setAttribute(name, value) { this[name] = value; },
    addEventListener(name, handler) { handlers.set(name, handler); },
    trigger(event = "click") { return this.disabled ? undefined : handlers.get(event)?.(); },
    triggerRaw(event = "click") { return handlers.get(event)?.(); },
  };
}

function project(actions = [], role = 115) {
  return {
    role_id: role, notice: "试用，缺失动作会回退到待机。",
    actions: actions.map((action) => ({ action, label: { Idle: "待机", Walk: "行走", Fight: "战斗" }[action],
      frame_count: 3, duration_ms: 300, fps: 10, loop: true })),
  };
}

function animation(action) {
  return { action, frames: [`${action}-frame-1`, `${action}-frame-2`], durations: [100, 200], total: 2 };
}

function loadUi() {
  const elements = new Map();
  const find = (id) => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  find("character-role-id").value = "115";
  find("character-action").value = "Idle";
  find("character-animation-fps").value = "15";
  find("character-animation-loop").checked = true;
  find("character-animation-name").value = "测试角色动画";
  find("character-animation-author").value = "测试作者";
  const requests = [], responses = [], rendered = [], disposals = [];
  const context = vm.createContext({
    window: {}, console,
    document: { getElementById: find, createElement: (tag) => element("", tag) },
  });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/character-animation.js"), "utf8");
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, "globalThis.ui = { state, refresh, loadPreview, importMedia };\n})();"), context);
  const editor = context.window.CharacterAnimation;
  const media = { current: null };
  editor.init({
    async call(name, options, ...args) {
      requests.push({ name, args: JSON.parse(JSON.stringify(args)) });
      assert.equal(options.quiet, true);
      assert.ok(responses.length, `Unexpected request ${name}`);
      const response = await responses.shift();
      if (response instanceof Error) throw response;
      return response;
    },
    disposeAnimation(id) { disposals.push(id); },
    renderAnimation(host, value) { media.current = { kind: "animation", value }; rendered.push(value); },
    renderAnimationState(host, message) { media.current = { kind: "state", message }; },
  });
  return {
    editor, find, requests, responses, rendered, disposals, media, ...context.ui,
    async enter(value = project(), preview = animation("Idle")) {
      responses.push(value);
      if (value.actions.some((row) => row.action === find("character-action").value)) responses.push(preview);
      await editor.enter();
    },
  };
}

function checkMarkup() {
  const html = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/index.html"), "utf8");
  for (const id of ["character-role-id", "character-action", "character-animation-fps", "character-animation-loop",
    "character-import-media", "character-import-folder", "character-remove-action", "character-export-animation",
    "character-animation-actions", "character-animation-preview", "character-animation-status"])
    assert.equal((html.match(new RegExp(`id="${id}"`, "g")) || []).length, 1, `${id} missing or duplicated`);
  assert.ok(html.indexOf('src="character-animation.js"') < html.indexOf('src="app.js"'));
}

async function checkEnterLoadsSavedActionsAndActualPreview() {
  const ui = loadUi();
  await ui.enter(project(["Idle", "Walk"]));
  assert.deepEqual(ui.requests.map((row) => row.name), ["character_animation_state", "character_animation_preview"]);
  assert.deepEqual(ui.requests[0].args, [115]);
  assert.equal(ui.state.active, true);
  assert.equal(ui.state.working, false);
  assert.equal(ui.find("character-animation-actions").children.length, 2);
  assert.match(ui.find("character-animation-actions").children[0].textContent, /待机/);
  assert.equal(ui.media.current.value.action, "Idle");
  assert.equal(ui.find("character-export-animation").disabled, false);
}

async function checkImportForwardsFolderFpsAndLoopOptions() {
  const ui = loadUi();
  await ui.enter();
  ui.find("character-action").value = "Walk";
  ui.find("character-animation-fps").value = "24";
  ui.find("character-animation-loop").checked = false;
  ui.responses.push(project(["Walk"]), animation("Walk"));
  await ui.find("character-import-media").trigger();
  assert.deepEqual(ui.requests.find((row) => row.name === "character_animation_import").args,
    [115, "Walk", { fps: 24, loop: false }, false]);
  assert.equal(ui.media.current.value.action, "Walk");
  ui.find("character-action").value = "Fight";
  ui.find("character-animation-fps").value = "30";
  ui.find("character-animation-loop").checked = true;
  ui.responses.push(project(["Walk", "Fight"]), animation("Fight"));
  await ui.find("character-import-folder").trigger();
  assert.deepEqual(ui.requests.filter((row) => row.name === "character_animation_import").at(-1).args,
    [115, "Fight", { fps: 30, loop: true }, true]);
}

async function checkCancelPreservesSavedActionsAndPlayingPreview() {
  const ui = loadUi();
  const saved = project(["Idle"]);
  await ui.enter(saved);
  const previousPreview = ui.media.current;
  const previousRenderCount = ui.rendered.length;
  ui.responses.push(null);
  await ui.find("character-import-media").trigger();
  assert.equal(ui.state.data, saved);
  assert.equal(ui.media.current, previousPreview);
  assert.equal(ui.rendered.length, previousRenderCount);
  assert.match(ui.find("character-animation-status").textContent, /取消.*保留/);
  assert.equal(ui.state.working, false);
}

async function checkLateActionPreviewCannotReplaceCurrentAction() {
  const ui = loadUi();
  await ui.enter(project(["Idle", "Walk", "Fight"]));
  const old = deferred(), current = deferred();
  ui.responses.push(old.promise, current.promise);
  ui.find("character-action").value = "Walk";
  const oldRequest = ui.loadPreview();
  ui.find("character-action").value = "Fight";
  const currentRequest = ui.loadPreview();
  current.resolve(animation("Fight"));
  await currentRequest;
  old.resolve(animation("Walk"));
  await oldRequest;
  assert.equal(ui.media.current.value.action, "Fight");
  assert.equal(ui.rendered.length, 2);
}

async function checkLeaveStopsPlayerAndDiscardsLatePreview() {
  const ui = loadUi();
  await ui.enter(project(["Idle", "Walk"]));
  const waiting = deferred();
  ui.responses.push(waiting.promise);
  ui.find("character-action").value = "Walk";
  const request = ui.loadPreview();
  const renderCount = ui.rendered.length;
  const disposeCount = ui.disposals.length;
  ui.editor.leave();
  assert.equal(ui.disposals.length, disposeCount + 1);
  assert.equal(ui.disposals.at(-1), "character-animation-preview");
  waiting.resolve(animation("Walk"));
  await request;
  assert.equal(ui.rendered.length, renderCount);
  assert.equal(ui.state.active, false);
}

async function checkBusyReentryRefreshesAfterCompletionAndBlocksDuplicateActions() {
  const ui = loadUi();
  await ui.enter();
  const waiting = deferred();
  ui.responses.push(waiting.promise);
  const pendingImport = ui.find("character-import-media").trigger();
  const requestCount = ui.requests.length;
  assert.equal(ui.state.working, true);
  await ui.find("character-import-folder").triggerRaw();
  await ui.find("character-export-animation").triggerRaw();
  assert.equal(ui.requests.length, requestCount);
  ui.editor.leave();
  await ui.editor.enter();
  assert.equal(ui.requests.length, requestCount);
  ui.responses.push(project(["Idle"]), animation("Idle"));
  waiting.resolve(project(["Walk"]));
  await pendingImport;
  assert.equal(ui.state.working, false);
  assert.deepEqual(ui.state.data.actions.map((row) => row.action), ["Idle"]);
  assert.deepEqual(ui.requests.slice(-2).map((row) => row.name), ["character_animation_state", "character_animation_preview"]);
  assert.equal(ui.media.current.value.action, "Idle");
}

async function checkEmptyExportDisabledAndExportFailurePreservesWork() {
  const ui = loadUi();
  await ui.enter();
  assert.equal(ui.find("character-export-animation").disabled, true);
  const requestCount = ui.requests.length;
  await ui.find("character-export-animation").trigger();
  assert.equal(ui.requests.length, requestCount);
  const saved = project(["Idle"]);
  ui.responses.push(saved, animation("Idle"));
  await ui.refresh();
  const previousPreview = ui.media.current;
  ui.responses.push(new Error("磁盘空间不足"));
  await ui.find("character-export-animation").trigger();
  assert.equal(ui.state.data, saved);
  assert.equal(ui.media.current, previousPreview);
  assert.equal(ui.state.working, false);
  assert.match(ui.find("character-animation-status").textContent, /磁盘空间不足/);
  assert.equal(ui.find("character-animation-status").classList.contains("is-error"), true);
  assert.deepEqual(ui.requests.at(-1).args, [115, "测试角色动画", "测试作者"]);
}

async function checkRemoveOnlySendsCurrentAction() {
  const ui = loadUi();
  await ui.enter(project(["Idle", "Walk"]));
  ui.find("character-action").value = "Walk";
  ui.responses.push(project(["Idle"]));
  await ui.find("character-remove-action").trigger();
  assert.deepEqual(ui.requests.at(-1), { name: "character_animation_remove", args: [115, "Walk"] });
  assert.deepEqual(ui.state.data.actions.map((row) => row.action), ["Idle"]);
  assert.match(ui.media.current.message, /当前动作还没有素材/);
}

async function checkInvalidRoleIsHandledWithoutUnhandledRejections() {
  const ui = loadUi();
  ui.find("character-role-id").value = "not-a-number";
  await ui.editor.enter();
  assert.equal(ui.requests.length, 0);
  assert.equal(ui.state.working, false);
  assert.match(ui.find("character-animation-status").textContent, /编号/);
  ui.find("character-role-id").value = "0";
  await ui.refresh();
  await ui.loadPreview();
  ui.find("character-action").triggerRaw("change");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.requests.length, 0);
  assert.equal(ui.find("character-import-media").disabled, true);
}

async function main() {
  checkMarkup();
  const checks = [checkEnterLoadsSavedActionsAndActualPreview, checkImportForwardsFolderFpsAndLoopOptions,
    checkCancelPreservesSavedActionsAndPlayingPreview, checkLateActionPreviewCannotReplaceCurrentAction,
    checkLeaveStopsPlayerAndDiscardsLatePreview, checkBusyReentryRefreshesAfterCompletionAndBlocksDuplicateActions,
    checkEmptyExportDisabledAndExportFailurePreservesWork, checkRemoveOnlySendsCurrentAction,
    checkInvalidRoleIsHandledWithoutUnhandledRejections];
  for (const check of checks) await check();
  process.stdout.write(`角色动画界面回归通过，共 ${checks.length} 项。\n`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
