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
    get options() { return this.children.filter((child) => child.tag === "option"); },
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

function originalData(items) {
  return {
    items,
    action_choices: [...new Set(items.map((row) => row.name))].map((action) => ({ action, label: action })),
    reason: items.length ? "" : "没有对应的原动作，可手工制作。",
  };
}

function originalAnimation(rawAction, runtimeAction = rawAction) {
  return { ...animation(rawAction), runtime_action: runtimeAction, source_animation: rawAction };
}

function loadUi() {
  const elements = new Map();
  const find = (id) => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  find("character-role-id").value = "115";
  find("character-action").value = "Idle";
  for (const action of ["Idle", "Walk", "Fight"]) {
    const option = element("", "option");
    option.value = action;
    find("character-action").appendChild(option);
  }
  find("character-animation-fps").value = "15";
  find("character-animation-loop").checked = true;
  find("character-animation-name").value = "测试角色动画";
  find("character-animation-author").value = "测试作者";
  const requests = [], responses = [], rendered = [], disposals = [];
  const originalRequests = [], originalResponses = [], originalPreviewResponses = [], originalRendered = [];
  const context = vm.createContext({
    window: {}, console,
    document: { getElementById: find, createElement: (tag) => element("", tag) },
  });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/character-animation.js"), "utf8");
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, "globalThis.ui = { state, refresh, loadPreview, importMedia, loadOriginals, loadOriginalPreview, loadComparison };\n})();"), context);
  const editor = context.window.CharacterAnimation;
  const media = { current: null, original: null };
  editor.init({
    async call(name, options, ...args) {
      const record = { name, args: JSON.parse(JSON.stringify(args)) };
      assert.equal(options.quiet, true);
      let response;
      if (name === "character_animation_originals") {
        originalRequests.push(record);
        response = await (originalResponses.length ? originalResponses.shift() : { items: [], action_choices: [] });
      } else if (name === "character_animation_original_preview") {
        originalRequests.push(record);
        assert.ok(originalPreviewResponses.length, `Unexpected original preview ${JSON.stringify(args)}`);
        response = await originalPreviewResponses.shift();
      } else {
        requests.push(record);
        assert.ok(responses.length, `Unexpected request ${name}`);
        response = await responses.shift();
      }
      if (response instanceof Error) throw response;
      return response;
    },
    disposeAnimation(id) { disposals.push(id); },
    renderAnimation(host, value) {
      if (host.id === "character-animation-original") {
        media.original = { kind: "animation", value };
        originalRendered.push(value);
      } else {
        media.current = { kind: "animation", value };
        rendered.push(value);
      }
    },
    renderAnimationState(host, message) {
      if (host.id === "character-animation-original") media.original = { kind: "state", message };
      else media.current = { kind: "state", message };
    },
  });
  return {
    editor, find, requests, responses, rendered, disposals, media, ...context.ui,
    originalRequests, originalResponses, originalPreviewResponses, originalRendered,
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
    "character-animation-actions", "character-animation-preview", "character-animation-status",
    "character-animation-original", "character-original-variant", "character-original-status"])
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
  assert.equal(ui.disposals.length, disposeCount + 2);
  assert.deepEqual(ui.disposals.slice(-2), ["character-animation-original", "character-animation-preview"]);
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

async function checkBrowseTargetDuringEnterDropsOldRoleAndLoadsExactOriginalAction() {
  const ui = loadUi();
  ui.find("character-role-id").value = "101";
  const oldRole = deferred();
  ui.responses.push(oldRole.promise);
  const entering = ui.editor.enter();
  assert.deepEqual(ui.requests[0], { name: "character_animation_state", args: [101] });
  await ui.editor.selectTarget({ roleId: 304, action: "Fight-attack", fps: 30, loop: false });
  assert.equal(ui.find("character-role-id").value, "304");
  assert.equal(ui.find("character-action").value, "Fight-attack");
  assert.equal(ui.find("character-animation-fps").value, "30");
  assert.equal(ui.find("character-animation-loop").checked, false);
  assert.equal(ui.find("character-animation-name").value, "测试角色动画");
  assert.equal(ui.find("character-animation-author").value, "测试作者");
  assert.equal(ui.state.working, true);
  const saved304 = project(["Idle", "Walk", "Fight-attack"], 304);
  const originalActions = JSON.stringify(saved304.actions);
  ui.responses.push(saved304, animation("Fight-attack"));
  oldRole.resolve(project(["Idle", "Walk"], 101));
  await entering;
  assert.deepEqual(ui.requests.map((row) => [row.name, ...row.args]), [
    ["character_animation_state", 101],
    ["character_animation_state", 304],
    ["character_animation_preview", 304, "Fight-attack"],
  ]);
  assert.equal(ui.state.data, saved304);
  assert.equal(ui.media.current.value.action, "Fight-attack");
  assert.equal(ui.state.working, false);
  assert.equal(JSON.stringify(saved304.actions), originalActions);
  assert.equal(ui.find("character-action").options.filter((option) => option.value === "Fight-attack").length, 1);
}

async function checkBrowseTargetDuringOldPreviewCannotRenderPreviousRole() {
  const ui = loadUi();
  ui.find("character-role-id").value = "101";
  const oldPreview = deferred();
  ui.responses.push(project(["Idle", "Walk"], 101), oldPreview.promise);
  const entering = ui.editor.enter();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(ui.requests.at(-1), { name: "character_animation_preview", args: [101, "Idle"] });
  await ui.editor.selectTarget({ roleId: 304, action: "Fight-attack", fps: 24, loop: true });
  ui.responses.push(project(["Idle", "Fight-attack"], 304), animation("Fight-attack"));
  oldPreview.resolve(animation("old-role-101-Idle"));
  await entering;
  assert.equal(ui.media.current.value.action, "Fight-attack");
  assert.equal(ui.rendered.some((row) => row.action === "old-role-101-Idle"), false);
  assert.equal(ui.find("character-role-id").value, "304");
  assert.equal(ui.find("character-action").value, "Fight-attack");
  assert.equal(ui.find("character-animation-fps").value, "24");
  assert.equal(ui.find("character-animation-loop").checked, true);
}

async function checkInactiveTargetIsLoadedOnEnterWithoutGuessingDefaultAction() {
  const ui = loadUi();
  await ui.editor.selectTarget({ roleId: 304, action: "Fight-attack", fps: 30, loop: false });
  assert.equal(ui.requests.length, 0);
  const saved = project(["Walk", "Fight-attack"], 304);
  ui.responses.push(saved, animation("Fight-attack"));
  await ui.editor.enter();
  assert.deepEqual(ui.requests.map((row) => [row.name, ...row.args]), [
    ["character_animation_state", 304], ["character_animation_preview", 304, "Fight-attack"],
  ]);
  assert.equal(ui.state.data, saved);
  assert.equal(ui.find("character-action").value, "Fight-attack");
}

async function checkUnverifiedTargetKeepsDraftAndPromptsForActualRoleOrAction() {
  const ui = loadUi();
  await ui.enter(project(["Idle", "Walk"]));
  ui.find("character-action").value = "Walk";
  const saved = ui.state.data;
  const requests = ui.requests.length;
  await ui.editor.selectTarget({ roleId: null, action: "Fight-attack", fps: 30 });
  assert.match(ui.find("character-animation-status").textContent, /角色编号/);
  assert.equal(ui.state.data, saved);
  assert.equal(ui.find("character-role-id").value, "115");
  assert.equal(ui.find("character-action").value, "Walk");
  await ui.editor.selectTarget({ roleId: 304, fps: 30 });
  assert.match(ui.find("character-animation-status").textContent, /动作/);
  assert.equal(ui.state.data, saved);
  assert.equal(ui.find("character-role-id").value, "115");
  assert.equal(ui.find("character-action").value, "Walk", "Missing original action was silently changed to Idle");
  assert.equal(ui.requests.length, requests);
  assert.equal(ui.find("character-animation-status").classList.contains("is-error"), true);
}

async function checkOriginalAndSavedPreviewUseIndependentPlayers() {
  const ui = loadUi();
  const source = { bundle: "default.bundle", name: "Idle", label: "默认外观" };
  const original = originalAnimation("Idle");
  const saved = { ...animation("Idle"), frames: ["saved-first", "saved-second"] };
  ui.originalResponses.push(originalData([source]));
  ui.originalPreviewResponses.push(original);
  await ui.enter(project(["Idle"]), saved);
  assert.deepEqual(ui.originalRequests, [
    { name: "character_animation_originals", args: [115, "Idle", ""] },
    { name: "character_animation_original_preview", args: [115, "Idle", "default.bundle"] },
  ]);
  assert.equal(ui.media.original.value, original);
  assert.equal(ui.media.current.value, saved);
  assert.equal(ui.originalRendered.length, 1);
  assert.equal(ui.rendered.length, 1);
  assert.equal(ui.find("character-original-variant").value, "default.bundle");
  assert.equal(ui.find("character-import-media").disabled, false);
  const disposeCount = ui.disposals.length;
  ui.editor.leave();
  assert.deepEqual(ui.disposals.slice(disposeCount), ["character-animation-original", "character-animation-preview"]);
}

async function checkRawOriginalAndRuntimeSavedKeysStaySeparateThroughImportAndRemove() {
  const ui = loadUi();
  const raw = "Fight-attack-effect", runtime = "Fight-effect";
  const source = { bundle: "effect.bundle", name: raw, label: "外观 03" };
  await ui.editor.selectTarget({ roleId: 112, action: runtime, preferred_source: { bundle: source.bundle, name: raw } });
  ui.originalResponses.push(originalData([source]));
  ui.originalPreviewResponses.push(originalAnimation(raw, runtime));
  await ui.enter(project([runtime], 112), animation(runtime));
  assert.deepEqual(ui.originalRequests.map((row) => row.args), [[112, raw, "effect.bundle"], [112, raw, "effect.bundle"]]);
  assert.deepEqual(ui.requests.at(-1).args, [112, runtime]);
  assert.equal(ui.state.preferredSource.name, raw);
  assert.equal(ui.state.preferredSource.action, runtime);
  assert.equal(ui.find("character-action").value, runtime);
  assert.ok(ui.find("character-action").options.some((option) => option.value === raw));
  assert.ok(ui.find("character-action").options.some((option) => option.value === runtime));

  ui.find("character-action").value = raw;
  ui.originalResponses.push(originalData([source]));
  ui.originalPreviewResponses.push(originalAnimation(raw, runtime));
  ui.responses.push(animation(runtime));
  await ui.find("character-action").trigger("change");
  assert.equal(ui.media.original.value.action, raw);
  assert.equal(ui.media.current.value.action, runtime);
  ui.responses.push(project([runtime], 112), animation(runtime));
  await ui.find("character-import-folder").trigger();
  assert.deepEqual(ui.requests.findLast((row) => row.name === "character_animation_import").args,
    [112, runtime, { fps: 15, loop: true }, true]);
  ui.responses.push(project([], 112));
  await ui.find("character-remove-action").trigger();
  assert.deepEqual(ui.requests.at(-1), { name: "character_animation_remove", args: [112, runtime] });
}

async function checkUnsupportedOriginalAndAmbiguousRuntimeDisableImportWithReason() {
  for (const original of [new Error("原动画包含无法播放的骨骼轨。"),
    { ...originalAnimation("Idle", null), runtime_binding_reason: "此原动作包含多个精灵动作前缀。" }]) {
    const ui = loadUi();
    ui.originalResponses.push(originalData([{ bundle: "source.bundle", name: "Idle", label: "默认外观" }]));
    ui.originalPreviewResponses.push(original);
    await ui.enter(project(["Idle"]));
    assert.equal(ui.find("character-import-media").disabled, true);
    assert.equal(ui.find("character-import-folder").disabled, true);
    assert.match(ui.find("character-original-status").textContent, original instanceof Error ? /骨骼轨/ : /多个/);
    const requestCount = ui.requests.length;
    await ui.find("character-import-media").trigger();
    assert.equal(ui.requests.length, requestCount);
    assert.equal(ui.media.current.value.action, "Idle", "Saved replacement preview should remain inspectable");
  }
  const manual = loadUi();
  manual.originalResponses.push(originalData([]));
  await manual.enter();
  assert.equal(manual.find("character-import-media").disabled, false);
  assert.match(manual.find("character-original-status").textContent, /手工制作/);
}

async function checkVariantChangesClearRightAndRejectLateOriginalAndReplacementRequests() {
  const ui = loadUi();
  const variants = [{ bundle: "base.bundle", name: "Idle", label: "默认外观" },
    { bundle: "second.bundle", name: "Idle", label: "外观 02" }];
  ui.originalResponses.push(originalData(variants));
  ui.originalPreviewResponses.push(originalAnimation("Idle", "Idle"));
  await ui.enter(project(["Idle", "Idle-02"]));
  const old = deferred();
  ui.originalPreviewResponses.push(old.promise);
  ui.find("character-original-variant").value = "second.bundle";
  const oldChange = ui.find("character-original-variant").trigger("change");
  assert.equal(ui.media.current.kind, "state");
  assert.match(ui.media.current.message, /核对/);
  assert.equal(ui.find("character-import-media").disabled, true);

  ui.originalPreviewResponses.push(originalAnimation("Idle", "Idle"));
  ui.responses.push(animation("current-base-replacement"));
  ui.find("character-original-variant").value = "base.bundle";
  await ui.find("character-original-variant").trigger("change");
  old.resolve(originalAnimation("Idle", "Idle-02"));
  await oldChange;
  assert.equal(ui.state.originalBinding.runtime_action, "Idle");
  assert.equal(ui.media.current.value.action, "current-base-replacement");
  assert.equal(ui.requests.filter((row) => row.name === "character_animation_preview").length, 2);
  assert.equal(ui.originalRendered.length, 2);
  assert.equal(ui.find("character-original-variant").value, "base.bundle");
}

async function checkLateSameTargetOriginalListDoesNotIssueAnotherSavedPreview() {
  const ui = loadUi();
  await ui.enter(project(["Idle"]));
  const old = deferred(), current = deferred();
  ui.originalResponses.push(old.promise, current.promise);
  const oldComparison = ui.loadComparison();
  const currentComparison = ui.loadComparison();
  ui.originalPreviewResponses.push(originalAnimation("Idle"));
  ui.responses.push(animation("current-replacement"));
  current.resolve(originalData([{ bundle: "new.bundle", name: "Idle", label: "新参考" }]));
  await currentComparison;
  old.resolve(originalData([{ bundle: "old.bundle", name: "Idle", label: "旧参考" }]));
  await oldComparison;
  assert.equal(ui.find("character-original-variant").value, "new.bundle");
  assert.equal(ui.media.current.value.action, "current-replacement");
  assert.equal(ui.requests.filter((row) => row.name === "character_animation_preview").length, 2);
  assert.equal(ui.originalRequests.filter((row) => row.name === "character_animation_original_preview").length, 1);
}

async function checkRoleAndActionInputsImmediatelyClearBothOldPlayers() {
  const ui = loadUi();
  ui.originalResponses.push(originalData([{ bundle: "source.bundle", name: "Idle", label: "默认外观" }]));
  ui.originalPreviewResponses.push(originalAnimation("Idle"));
  await ui.enter(project(["Idle", "Walk"]));
  ui.find("character-role-id").value = "304";
  ui.find("character-role-id").triggerRaw("input");
  assert.equal(ui.media.original.kind, "state");
  assert.equal(ui.media.current.kind, "state");
  assert.match(ui.media.current.message, /角色编号/);
  assert.equal(ui.find("character-import-media").disabled, true);
  assert.equal(ui.find("character-export-animation").disabled, true);

  ui.find("character-role-id").value = "115";
  ui.find("character-role-id").triggerRaw("input");
  ui.find("character-action").value = "Walk";
  const waiting = deferred();
  ui.originalResponses.push(waiting.promise);
  const change = ui.find("character-action").trigger("change");
  assert.equal(ui.media.original.kind, "state");
  assert.equal(ui.media.current.kind, "state");
  ui.editor.leave();
  waiting.resolve(originalData([]));
  await change;
  assert.equal(ui.rendered.length, 1);
  assert.equal(ui.originalRendered.length, 1);
}

async function checkSelectingNewBrowseTargetClearsOldRightPreviewBeforeReload() {
  const ui = loadUi();
  await ui.enter(project(["Idle"]));
  const waiting = deferred();
  ui.responses.push(waiting.promise);
  const selected = ui.editor.selectTarget({ roleId: 304, action: "Walk", preferred_source: { bundle: "walk.bundle", name: "Walk" } });
  assert.equal(ui.media.original.kind, "state");
  assert.equal(ui.media.current.kind, "state", "Browse target changed while old role replacement stayed visible");
  ui.editor.leave();
  waiting.resolve(project(["Walk"], 304));
  await selected;
}

async function main() {
  checkMarkup();
  const checks = [checkEnterLoadsSavedActionsAndActualPreview, checkImportForwardsFolderFpsAndLoopOptions,
    checkCancelPreservesSavedActionsAndPlayingPreview, checkLateActionPreviewCannotReplaceCurrentAction,
    checkLeaveStopsPlayerAndDiscardsLatePreview, checkBusyReentryRefreshesAfterCompletionAndBlocksDuplicateActions,
    checkEmptyExportDisabledAndExportFailurePreservesWork, checkRemoveOnlySendsCurrentAction,
    checkInvalidRoleIsHandledWithoutUnhandledRejections,
    checkBrowseTargetDuringEnterDropsOldRoleAndLoadsExactOriginalAction,
    checkBrowseTargetDuringOldPreviewCannotRenderPreviousRole,
    checkInactiveTargetIsLoadedOnEnterWithoutGuessingDefaultAction,
    checkUnverifiedTargetKeepsDraftAndPromptsForActualRoleOrAction,
    checkOriginalAndSavedPreviewUseIndependentPlayers,
    checkRawOriginalAndRuntimeSavedKeysStaySeparateThroughImportAndRemove,
    checkUnsupportedOriginalAndAmbiguousRuntimeDisableImportWithReason,
    checkVariantChangesClearRightAndRejectLateOriginalAndReplacementRequests,
    checkLateSameTargetOriginalListDoesNotIssueAnotherSavedPreview,
    checkRoleAndActionInputsImmediatelyClearBothOldPlayers,
    checkSelectingNewBrowseTargetClearsOldRightPreviewBeforeReload];
  for (const check of checks) await check();
  process.stdout.write(`角色动画界面回归通过，共 ${checks.length} 项。\n`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
