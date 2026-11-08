/* 功能插件工程的未保存源码、构建失败和重复操作回归。 */
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

function element(id) {
  const handlers = new Map();
  const classes = new Set();
  return {
    id, textContent: "", value: "", disabled: false,
    classList: {
      contains: (name) => classes.has(name),
      toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
    },
    addEventListener: (event, handler) => handlers.set(event, handler),
    trigger(event = "click") { return this.disabled ? undefined : handlers.get(event)?.(); },
    triggerRaw(event = "click") { return handlers.get(event)?.(); },
  };
}

function project(name = "FirstMod", source = "// original source", hash = "original-hash") {
  return { project: `D:/projects/${name}`, source, source_hash: hash };
}

function loadUi() {
  const elements = new Map();
  const find = (id) => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  find("plugin-template").value = "frame_rate";
  find("plugin-id").value = "FirstMod";
  find("plugin-fps").value = "144";
  const requests = [], responses = [];
  const context = vm.createContext({ window: {}, document: { getElementById: find }, console });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/plugins.js"), "utf8");
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, "globalThis.ui = { state };\n})();"), context);
  const editor = context.window.PluginStudio;
  editor.init({
    async call(name, options, ...args) {
      requests.push({ name, args });
      assert.ok(responses.length, `Unexpected request ${name}`);
      const response = await responses.shift();
      if (response instanceof Error) throw response;
      return response;
    },
  });
  return { editor, state: context.ui.state, find, requests, responses,
    async create(value = project()) {
      responses.push(value);
      await find("plugin-create").trigger();
    },
    async environment(changes = {}) {
      responses.push({ sdk: "D:/sdk", dotnet: true, loader_present: false, ...changes });
      await editor.enter();
    },
  };
}

function checkMarkup() {
  const html = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/index.html"), "utf8");
  for (const id of ["plugin-template", "plugin-create", "plugin-open-project", "plugin-source", "plugin-save",
    "plugin-build", "plugin-build-log", "plugin-choose-sdk", "plugin-fetch-sdk", "plugin-status"]) {
    assert.equal((html.match(new RegExp(`id="${id}"`, "g")) || []).length, 1, `${id} missing or duplicated`);
  }
  assert.ok(html.indexOf('src="plugins.js"') < html.indexOf('src="app.js"'));
  const app = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/app.js"), "utf8");
  assert.match(app, /window\.PluginStudio\?\.init\(/);
  assert.match(app, /window\.PluginStudio\?\.enter\(/);
}

async function checkDependenciesDisableBuild() {
  const ui = loadUi();
  assert.equal(ui.find("plugin-build").disabled, true);
  await ui.environment();
  assert.equal(ui.find("plugin-build").disabled, true, "Build enabled without a project");
  await ui.create();
  assert.equal(ui.find("plugin-build").disabled, false);
  await ui.environment({ sdk: "" });
  assert.equal(ui.find("plugin-build").disabled, true);
  await ui.environment({ dotnet: false });
  assert.equal(ui.find("plugin-build").disabled, true);
  await ui.environment();
  assert.equal(ui.find("plugin-build").disabled, false);
}

async function checkCreateSavesPreviousEditsFirst() {
  const ui = loadUi();
  await ui.create();
  ui.find("plugin-source").value = "// unsaved first project";
  ui.responses.push(project("FirstMod", "// unsaved first project", "saved-hash"), project("SecondMod", "// second"));
  await ui.find("plugin-create").trigger();
  assert.deepEqual(ui.requests.map((request) => request.name), [
    "plugin_create_project", "plugin_save_source", "plugin_create_project"]);
  assert.deepEqual(ui.requests[1].args, ["// unsaved first project", "original-hash"]);
  assert.equal(ui.state.project, "D:/projects/SecondMod");
  assert.equal(ui.find("plugin-source").value, "// second");
}

async function checkOpenSavesPreviousEditsAndHandlesCancellation() {
  const ui = loadUi();
  await ui.create();
  ui.find("plugin-source").value = "// keep before open";
  ui.responses.push(project("FirstMod", "// keep before open", "saved-hash"), null);
  await ui.find("plugin-open-project").trigger();
  assert.deepEqual(ui.requests.map((request) => request.name), [
    "plugin_create_project", "plugin_save_source", "plugin_open_project"]);
  assert.equal(ui.find("plugin-source").value, "// keep before open");
  assert.equal(ui.state.project, "D:/projects/FirstMod");
  assert.equal(ui.state.sourceHash, "saved-hash");
  ui.responses.push(project("SecondMod", "// opened"));
  await ui.find("plugin-open-project").trigger();
  assert.equal(ui.state.project, "D:/projects/SecondMod");
  assert.equal(ui.find("plugin-source").value, "// opened");
}

async function checkExternalChangeFailureKeepsEditsAndPreventsSwitching() {
  for (const action of ["plugin-save", "plugin-create", "plugin-open-project"]) {
    const ui = loadUi();
    await ui.create();
    ui.find("plugin-source").value = "// valuable unsaved changes";
    ui.responses.push(new Error("源码已被其他编辑器修改"));
    await ui.find(action).trigger();
    assert.equal(ui.find("plugin-source").value, "// valuable unsaved changes");
    assert.equal(ui.state.savedSource, "// original source");
    assert.equal(ui.state.sourceHash, "original-hash");
    assert.equal(ui.state.project, "D:/projects/FirstMod");
    assert.equal(ui.requests.length, 2, "Switch continued after source save failed");
    assert.match(ui.find("plugin-status").textContent, /其他编辑器/);
    assert.equal(ui.find("plugin-status").classList.contains("is-error"), true);
    assert.equal(ui.state.working, false);
  }
}

async function checkBuildFailureKeepsSourceAndDisplaysLog() {
  const ui = loadUi();
  await ui.create();
  await ui.environment();
  ui.find("plugin-source").value = "// invalid edited source";
  ui.responses.push(project("FirstMod", "// invalid edited source", "edited-hash"), {
    success: false, log: "ModEntry.cs(1,1): error CS1002", project: "D:/projects/FirstMod",
  });
  await ui.find("plugin-build").trigger();
  assert.deepEqual(ui.requests.slice(-2).map((request) => request.name), ["plugin_save_source", "plugin_build_project"]);
  assert.equal(ui.find("plugin-source").value, "// invalid edited source");
  assert.equal(ui.state.savedSource, "// invalid edited source");
  assert.match(ui.find("plugin-build-log").textContent, /CS1002/);
  assert.match(ui.find("plugin-status").textContent, /编译失败/);
  assert.equal(ui.find("plugin-build").disabled, false);
  assert.equal(ui.find("plugin-status").classList.contains("is-error"), true);
}

async function checkBuildStopsWhenSaveFails() {
  const ui = loadUi();
  await ui.create();
  await ui.environment();
  ui.find("plugin-source").value = "// keep local version";
  ui.responses.push(new Error("源码已被其他编辑器修改"));
  await ui.find("plugin-build").trigger();
  assert.equal(ui.requests.at(-1).name, "plugin_save_source");
  assert.equal(ui.find("plugin-source").value, "// keep local version");
  assert.equal(ui.requests.some((request) => request.name === "plugin_build_project"), false);
}

async function checkWorkingBlocksRepeatedClicksAndUnlocksAfterFailure() {
  const ui = loadUi();
  const waiting = deferred();
  ui.responses.push(waiting.promise);
  const first = ui.find("plugin-create").trigger();
  assert.equal(ui.state.working, true);
  assert.equal(ui.find("plugin-create").disabled, true);
  assert.equal(ui.find("plugin-open-project").disabled, true);
  await ui.find("plugin-create").triggerRaw();
  await ui.find("plugin-open-project").triggerRaw();
  await ui.find("plugin-fetch-sdk").triggerRaw();
  assert.equal(ui.requests.length, 1);
  waiting.resolve(new Error("创建失败"));
  await first;
  assert.equal(ui.state.working, false);
  assert.equal(ui.find("plugin-create").disabled, false);
  assert.equal(ui.find("plugin-open-project").disabled, false);
  await ui.create();
  assert.equal(ui.state.project, "D:/projects/FirstMod");
}

async function main() {
  checkMarkup();
  for (const check of [checkDependenciesDisableBuild, checkCreateSavesPreviousEditsFirst,
    checkOpenSavesPreviousEditsAndHandlesCancellation, checkExternalChangeFailureKeepsEditsAndPreventsSwitching,
    checkBuildFailureKeepsSourceAndDisplaysLog, checkBuildStopsWhenSaveFails,
    checkWorkingBlocksRepeatedClicksAndUnlocksAfterFailure]) {
    await check();
  }
  console.log("Plugin studio UI checks passed (8 groups).");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
