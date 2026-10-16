/* 隔离验证作品集对比与文件拖入；真实浏览器拖放由浏览器验收覆盖。 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { File } = require("node:buffer");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function loadFrontend() {
  const elements = new Map();
  function makeElement(id = "") {
    const classes = new Set();
    const handlers = new Map();
    return {
      id, children: [], value: "", innerHTML: "", textContent: "", disabled: false,
      classList: {
        add: (name) => classes.add(name), remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
        toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
      },
      appendChild(child) { this.children.push(child); },
      addEventListener(type, handler) { handlers.set(type, handler); },
      trigger(type, event) { return handlers.get(type)?.(event); },
      setAttribute() {}, remove() {}, contains: () => false,
    };
  }
  function find(id) {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  }
  const documentEvents = new Map();
  const requests = [];
  const reads = [];
  let readGate = null;
  let responder = async () => ({ ok: true, data: { name: "replacement.png", preview_data: "data:image/png;base64,bmV3" } });
  class Reader {
    async readAsDataURL(file) {
      reads.push(file);
      const gate = readGate;
      if (gate) await gate.promise;
      const bytes = Buffer.from(await file.arrayBuffer());
      this.result = `data:${file.type || "application/octet-stream"};base64,${bytes.toString("base64")}`;
      this.onload();
    }
  }
  const context = vm.createContext({
    console, URLSearchParams, FileReader: Reader,
    setTimeout: () => 0, clearTimeout() {},
    window: { location: { hash: "" }, addEventListener() {} },
    document: {
      readyState: "loading", addEventListener: (type, handler) => documentEvents.set(type, handler),
      body: makeElement("body"),
      querySelector: (selector) => find(selector.slice(1)), querySelectorAll: () => [],
      getElementById: find, createElement: () => makeElement(),
    },
    fetch: async (url, options) => {
      const request = { url, args: JSON.parse(options.body) };
      requests.push(request);
      return { json: async () => responder(request) };
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/app.js"), "utf8");
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, "globalThis.ui = { state, renderBrowseComparison, renderReplacement, refreshSelectionComparison, importDroppedReplacement, bindReplacementDrop, isPlayableAnimation, canEdit, updateExportButtons, showPage, refreshStudio, commitReplacement };\n})();"), context);
  const ui = context.ui;
  ui.state.page = "studio";
  ui.state.selection = texture("Portrait");
  return { ...ui, find, requests, reads, documentEvents,
    respond(handler) { responder = handler; }, waitForRead(gate) { readGate = gate; } };
}

function texture(name, modified = null) {
  return { asset_type: "texture", kind: "texture", bundle: "sample.bundle", name, editable: true,
    comparison: { modified, notice: modified ? "作品集已修改这张贴图。" : "当前作品集尚未修改这张贴图。" } };
}

function drop(files, items) {
  return { prevented: false, stopped: false,
    preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; },
    dataTransfer: { types: ["Files"], files, items: items || files.map(() => ({ kind: "file", webkitGetAsEntry: () => ({ isDirectory: false }) })) } };
}

async function checkComparison() {
  const ui = loadFrontend();
  // 不能根据作品集同 bundle 的其他条目推断当前图片已修改。
  ui.state.draft.items = [{ name: "Other", bundle: "sample.bundle", kind: "texture" }];
  ui.renderBrowseComparison();
  assert.ok(ui.find("preview-comparison").classList.contains("has-comparison"));
  assert.match(ui.find("preview-modified").innerHTML, /尚未修改/);
  assert.doesNotMatch(ui.find("preview-modified").innerHTML, /<img/);
  const modified = { preview_data: "data:image/png;base64,c2F2ZWQ=", width: 4, height: 4, label: "作品集修改图", draft_index: 0 };
  ui.state.selection = texture("Portrait", modified);
  ui.renderBrowseComparison();
  ui.renderReplacement();
  assert.match(ui.find("preview-modified").innerHTML, /c2F2ZWQ=/);
  assert.match(ui.find("studio-replacement").innerHTML, /c2F2ZWQ=/);
  assert.match(ui.find("studio-replacement-heading").textContent, /已保存/);
  ui.state.replacement = { name: "new.png", preview_data: "data:image/png;base64,bmV3" };
  ui.renderReplacement();
  assert.match(ui.find("studio-replacement").innerHTML, /bmV3/);
  assert.match(ui.find("studio-replacement-heading").textContent, /尚未保存/);
  assert.match(ui.find("studio-comparison-notice").textContent, /覆盖.*旧修改/);
  ui.state.replacement = null;
  ui.state.selection = texture("Unchanged");
  ui.renderReplacement();
  assert.doesNotMatch(ui.find("studio-replacement").innerHTML, /<img/);
  assert.match(ui.find("studio-comparison-notice").textContent, /尚未修改/);
  ui.state.selection = null;
  ui.renderBrowseComparison();
  assert.ok(ui.find("preview-modified-card").classList.contains("is-hidden"));
  assert.doesNotMatch(ui.find("preview-modified").innerHTML, /<img/);
}

async function checkComparisonRaces() {
  const ui = loadFrontend();
  const older = deferred();
  ui.respond(() => older.promise);
  const first = ui.refreshSelectionComparison();
  const fresh = texture("Portrait", { preview_data: "saved-current" });
  ui.respond(() => ({ ok: true, data: fresh }));
  await ui.refreshSelectionComparison();
  older.resolve({ ok: true, data: texture("Portrait", { preview_data: "stale" }) });
  await first;
  assert.equal(ui.state.selection.comparison.modified.preview_data, "saved-current");
  const delayed = deferred();
  ui.respond(() => delayed.promise);
  const second = ui.refreshSelectionComparison();
  ui.state.selectionRequest += 2; // A → B → A，名称相同也不能接收旧请求。
  ui.state.selection = texture("Portrait");
  delayed.resolve({ ok: true, data: fresh });
  await second;
  assert.equal(ui.state.selection.comparison.modified, null);
  ui.respond(() => ({ ok: true, data: texture("Portrait") }));
  await ui.refreshSelectionComparison();
  assert.match(ui.find("preview-modified").innerHTML, /尚未修改/);
}

async function checkFileBytesAndText() {
  const ui = loadFrontend();
  const bytes = Uint8Array.from([0, 255, 1, 128, 13, 10]);
  const file = new File([bytes], "中文图片.png", { type: "image/png" });
  const event = drop([file]);
  await ui.importDroppedReplacement(event);
  assert.equal(event.prevented, true);
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].url, "/api/import_replacement_file");
  assert.deepEqual(ui.requests[0].args, ["中文图片.png", Buffer.from(bytes).toString("base64"), { bundle: "sample.bundle", name: "Portrait", asset_type: "texture" }]);
  assert.equal(ui.state.replacement.name, "replacement.png");
  assert.equal(ui.state.replacementImporting, false);
  ui.state.selection = { asset_type: "text", bundle: "text.bundle", name: "Dialog", editable: true };
  ui.respond(() => ({ ok: true, data: { name: "dialog.txt", text_content: "新台词\n下一行", encoding: "UTF-8" } }));
  await ui.importDroppedReplacement(drop([new File(["新台词\n下一行"], "dialog.txt")]));
  assert.equal(ui.find("studio-text").value, "新台词\n下一行");
  assert.equal(ui.requests[1].args[2].asset_type, "text");
  ui.state.selection = { asset_type: "anim", bundle: "anim.bundle", name: "Idle", editable: true };
  ui.respond(() => ({ ok: true, data: { name: "Idle.animbin" } }));
  await ui.importDroppedReplacement(drop([new File([bytes], "Idle.animbin")]));
  assert.equal(ui.state.replacement.name, "Idle.animbin");
}

async function checkDropRejections() {
  const ui = loadFrontend();
  const previous = { name: "valid.png", preview_data: "old" };
  ui.state.replacement = previous;
  const file = new File(["image bytes"], "one.png");
  await ui.importDroppedReplacement(drop([file, file]));
  await ui.importDroppedReplacement(drop([file], [{ kind: "file", webkitGetAsEntry: () => ({ isDirectory: true }) }]));
  await ui.importDroppedReplacement(drop([new File([], "empty.png")]));
  await ui.importDroppedReplacement(drop([{ name: "large.png", size: 12 * 1024 * 1024 + 1 }]));
  assert.equal(ui.requests.length, 0);
  assert.equal(ui.reads.length, 0, "Rejected files were read into memory");
  assert.equal(ui.state.replacement, previous);
  ui.respond(() => ({ ok: false, error: "请用上方动态入口导入动图" }));
  await ui.importDroppedReplacement(drop([file]));
  assert.equal(ui.state.replacement, previous, "Rejected candidate discarded a valid replacement");
  assert.equal(ui.state.replacementImporting, false);
  assert.equal(ui.find("choose-replacement").disabled, false);
  ui.state.selection = { asset_type: "dynamic", animation_kind: "sequence", editable: true };
  await ui.importDroppedReplacement(drop([file]));
  assert.equal(ui.requests.length, 1, "Generic drop stole a dynamic resource import");
  const textDrag = { dataTransfer: { types: ["text/plain"] }, preventDefault() { throw new Error("Text drag was blocked"); } };
  await ui.importDroppedReplacement(textDrag);
}

async function checkImportRaces() {
  const ui = loadFrontend();
  const gate = deferred();
  ui.waitForRead(gate);
  const pendingRead = ui.importDroppedReplacement(drop([new File(["first"], "first.png")]));
  ui.state.selectionRequest += 1;
  ui.state.selection = texture("Other");
  gate.resolve();
  await pendingRead;
  assert.equal(ui.requests.length, 0, "A late file read imported into the next resource");
  assert.equal(ui.state.replacementImporting, false);
  ui.waitForRead(null);
  const response = deferred();
  const reached = deferred();
  ui.respond(() => { reached.resolve(); return response.promise; });
  const pendingImport = ui.importDroppedReplacement(drop([new File(["second"], "second.png")]));
  await reached.promise;
  ui.state.selectionRequest += 2;
  ui.state.selection = texture("Other");
  const newest = { name: "newer-selection.png" };
  ui.state.replacement = newest;
  response.resolve({ ok: true, data: { name: "late.png", preview_data: "late" } });
  await pendingImport;
  assert.equal(ui.state.replacement, newest, "A late API response replaced the next resource candidate");
}

function navigationResponder(ui, importResponse, onCommit = () => {}) {
  return ({ url }) => {
    if (url === "/api/import_replacement_file") return importResponse();
    if (url === "/api/get_studio_state") return { ok: true, data: ui.state.selection };
    if (url === "/api/get_categories" || url === "/api/get_character_list" || url === "/api/get_logs") return { ok: true, data: [] };
    if (url === "/api/get_character_labels") return { ok: true, data: { bundles: {}, resources: {} } };
    if (url === "/api/browse_assets") return { ok: true, data: { items: [{ name: "Portrait", bundle: "sample.bundle" }], total: 1 } };
    if (url === "/api/commit_replacement") {
      onCommit();
      return { ok: true, data: { draft: { items: [{ name: "Portrait", bundle: "sample.bundle", kind: "texture" }] } } };
    }
    throw new Error(`Unexpected navigation API: ${url}`);
  };
}

async function checkPageNavigationAfterImportSent() {
  const ui = loadFrontend();
  const oldCandidate = { name: "old-A.png", preview_data: "data:image/png;base64,QQ==" };
  const acceptedCandidate = { name: "new-B.png", preview_data: "data:image/png;base64,Qg==" };
  ui.state.replacement = oldCandidate;
  let backendCandidate = oldCandidate;
  const response = deferred();
  const reached = deferred();
  let committedCandidate;
  ui.respond(navigationResponder(ui, () => {
    backendCandidate = acceptedCandidate;
    reached.resolve();
    return response.promise;
  }, () => { committedCandidate = backendCandidate; }));
  const importRequest = ui.state.replacementRequest;
  const pendingImport = ui.importDroppedReplacement(drop([new File(["B"], "new-B.png")]));
  await reached.promise;
  ui.showPage("browse");
  assert.equal(ui.state.replacementRequest, importRequest + 1, "Page navigation invalidated a submitted import request");
  assert.equal(ui.state.replacement, oldCandidate);
  response.resolve({ ok: true, data: acceptedCandidate });
  await pendingImport;
  assert.equal(ui.state.replacement, backendCandidate, "Successful import was discarded after navigation");
  assert.equal(ui.state.replacementImporting, false);
  ui.showPage("studio");
  await ui.refreshStudio();
  assert.equal(ui.state.replacement, backendCandidate, "Returning to the studio restored a stale candidate");
  assert.match(ui.find("studio-replacement").innerHTML, /Qg==/);
  assert.doesNotMatch(ui.find("studio-replacement").innerHTML, /QQ==/);
  await ui.commitReplacement();
  assert.equal(committedCandidate, acceptedCandidate, "Saved bytes did not match the visible candidate");
}

async function checkPageNavigationBeforeImportSent() {
  const ui = loadFrontend();
  const oldCandidate = { name: "old-A.png", preview_data: "data:image/png;base64,QQ==" };
  ui.state.replacement = oldCandidate;
  const gate = deferred();
  ui.waitForRead(gate);
  ui.respond(navigationResponder(ui, () => { throw new Error("Hidden page started an import"); }));
  const pendingRead = ui.importDroppedReplacement(drop([new File(["B"], "new-B.png")]));
  ui.showPage("logs");
  gate.resolve();
  await pendingRead;
  assert.equal(ui.requests.some(({ url }) => url === "/api/import_replacement_file"), false);
  assert.equal(ui.state.replacement, oldCandidate);
  assert.equal(ui.state.replacementImporting, false);
  ui.showPage("studio");
  await ui.refreshStudio();
  assert.equal(ui.state.replacement, oldCandidate);
  assert.match(ui.find("studio-replacement").innerHTML, /QQ==/);
}

async function checkNewerImportWins() {
  const ui = loadFrontend();
  const response = deferred();
  const reached = deferred();
  ui.respond(() => { reached.resolve(); return response.promise; });
  const pendingImport = ui.importDroppedReplacement(drop([new File(["old"], "old.png")]));
  await reached.promise;
  ui.state.replacementRequest += 1;
  ui.state.replacementImporting = true;
  const newest = { name: "newer.png" };
  ui.state.replacement = newest;
  response.resolve({ ok: true, data: { name: "late-old.png" } });
  await pendingImport;
  assert.equal(ui.state.replacement, newest, "A superseded import overwrote the latest candidate");
  assert.equal(ui.state.replacementImporting, true, "A superseded import unlocked the latest request");
}

async function checkFileOnlyGlobalGuard() {
  const ui = loadFrontend();
  ui.bindReplacementDrop();
  let prevented = 0;
  for (const type of ["dragover", "drop"]) {
    ui.documentEvents.get(type)({ dataTransfer: { types: ["text/plain"] }, preventDefault() { prevented += 1; } });
    assert.equal(prevented, type === "dragover" ? 0 : 1);
    ui.documentEvents.get(type)({ dataTransfer: { types: ["Files"] }, preventDefault() { prevented += 1; } });
  }
  assert.equal(prevented, 2);
}

async function checkReadOnlySkillVideo() {
  const ui = loadFrontend();
  ui.state.selection = { asset_type: "dynamic", bundle: "skill.bundle", name: "VSkill_Hero124", animation_kind: "cri_usm", playable: true, exportable: true, editable: false };
  assert.equal(ui.isPlayableAnimation(ui.state.selection), true);
  assert.equal(ui.canEdit(ui.state.selection), false);
  ui.updateExportButtons();
  assert.equal(ui.find("export-primary").textContent, "导出 USM");
  assert.equal(ui.find("export-primary").disabled, false);
  assert.equal(ui.find("export-secondary").disabled, true);
  await ui.importDroppedReplacement(drop([new File(["movie bytes"], "movie.usm")]));
  assert.equal(ui.requests.length, 0);
  ui.state.selection.playable = false;
  ui.state.selection.exportable = false;
  assert.equal(ui.isPlayableAnimation(ui.state.selection), false);
  ui.updateExportButtons();
  assert.equal(ui.find("export-primary").disabled, true, "Unsupported CRI still offered a save dialog");
  assert.equal(ui.find("export-secondary").disabled, true);
  ui.state.selection = texture("Portrait");
  ui.updateExportButtons();
  assert.equal(ui.find("export-primary").disabled, false, "Unsupported CRI disabled exports for the next resource");
  assert.equal(ui.find("export-secondary").disabled, false);
}

async function main() {
  for (const test of [checkComparison, checkComparisonRaces, checkFileBytesAndText, checkDropRejections, checkImportRaces, checkPageNavigationAfterImportSent, checkPageNavigationBeforeImportSent, checkNewerImportWins, checkFileOnlyGlobalGuard, checkReadOnlySkillVideo]) {
    await test();
    console.log(`PASS: ${test.name}`);
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
