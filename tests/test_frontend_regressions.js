/* 请求故意倒序返回，验证用户快切页面时不会误用上一项状态。 */
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

function makeDom() {
  const elements = new Map();
  function makeElement(id = "") {
    const classes = new Set();
    const handlers = new Map();
    return {
      id, children: [], disabled: false, textContent: "", innerHTML: "", value: "",
      classList: {
        add: (name) => classes.add(name), remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
        toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
      },
      appendChild(child) { this.children.push(child); },
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      addEventListener(name, handler) { handlers.set(name, handler); },
      trigger(name, event = {}) { return handlers.get(name)?.(event); },
      setAttribute() {}, removeAttribute() {}, focus() {}, remove() {}, pause() {}, load() {},
    };
  }
  const find = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  };
  return {
    find,
    document: {
      readyState: "loading", addEventListener() {},
      querySelector: (selector) => find(selector.slice(1)),
      querySelectorAll: () => [], getElementById: find, createElement: () => makeElement(),
    },
  };
}

function loadScript(filename, hooks, globals = {}) {
  const dom = makeDom();
  const context = vm.createContext({
    console, URLSearchParams, AbortController, ...globals,
    setTimeout: () => 0, clearTimeout() {},
    window: { location: { hash: "" }, addEventListener() {} }, document: dom.document,
  });
  const file = path.join(__dirname, "../astral_party_auto/webui", filename);
  const source = fs.readFileSync(file, "utf8");
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, `globalThis.ui = { ${hooks} };\n})();`), context);
  assert.ok(context.ui, `${filename} test hook was not installed`);
  return { ...context.ui, ...dom };
}

async function checkBrowseRace() {
  const oldCategories = deferred();
  const browseCalls = [];
  const ui = loadScript("app.js", "state, refreshBrowse", {
    fetch: async (url, options) => {
      const args = JSON.parse(options.body);
      let data;
      if (url === "/api/get_categories") data = args[0] === "texture"
        ? await oldCategories.promise : [{ id: "dialogs", label: "对话", count: 1 }];
      else if (url === "/api/get_character_list") data = [];
      else if (url === "/api/get_character_labels") data = { bundles: {}, resources: {} };
      else if (url === "/api/browse_assets") {
        browseCalls.push(args);
        data = { items: [], total: 0 };
      } else throw new Error(`Unexpected request: ${url}`);
      return { json: async () => ({ ok: true, data }) };
    },
  });
  ui.state.page = "browse";
  const first = ui.refreshBrowse();
  ui.state.assetType = "text";
  ui.state.categoryId = "all";
  await ui.refreshBrowse();
  oldCategories.resolve([{ id: "hand_card", label: "手牌", count: 8 }]);
  await first;
  assert.equal(ui.state.categories[0].id, "dialogs", "Late texture categories replaced the text categories");
  assert.equal(ui.state.categoryId, "dialogs");
  assert.equal(browseCalls.length, 1, "Stale category response launched an extra resource request");
  assert.equal(browseCalls[0][0], "text");
  assert.equal(browseCalls[0][1], "dialogs");
}

async function checkDraftDetailFailure() {
  const detail = deferred();
  let response = detail.promise;
  const ui = loadScript("app.js", "state, showDraftDetail", {
    fetch: async () => ({ json: async () => await response }),
  });
  ui.state.page = "pack";
  ui.state.draft = { items: [{ kind: "texture", name: "Portrait" }, { kind: "text", name: "Config" }] };
  for (const id of ["draft-replace", "draft-crop", "draft-remove"]) ui.find(id).disabled = false;
  const pending = ui.showDraftDetail(1);
  for (const id of ["draft-replace", "draft-crop", "draft-remove"]) {
    assert.equal(ui.find(id).disabled, true, `${id} kept the previous item controls during load`);
  }
  detail.resolve({ ok: false, error: "作品集文件丢失" });
  await pending;
  for (const id of ["draft-replace", "draft-crop", "draft-remove"]) {
    assert.equal(ui.find(id).disabled, true, `${id} remained enabled after preview failure`);
  }
  assert.match(ui.find("draft-detail-title").textContent, /Config/);
  response = Promise.resolve({ ok: true, data: { item: ui.state.draft.items[1], original_text: "old", modified_text: "new" } });
  await ui.showDraftDetail(1);
  assert.equal(ui.find("draft-replace").disabled, true, "Text draft enabled image replacement");
  assert.equal(ui.find("draft-crop").disabled, true, "Text draft enabled image crop");
  assert.equal(ui.find("draft-remove").disabled, false, "Recovered draft could not be removed");
}

async function checkLateResourceList() {
  const resources = deferred();
  const categories = deferred();
  let resourceRequests = 0;
  const ui = loadScript("app.js", "state, loadResources, refreshBrowse", {
    fetch: async (url) => {
      let data;
      if (url === "/api/browse_assets") {
        resourceRequests += 1;
        data = resourceRequests === 1 ? await resources.promise : { items: [], total: 0 };
      } else if (url === "/api/get_categories") data = await categories.promise;
      else if (url === "/api/get_character_list") data = [];
      else if (url === "/api/get_character_labels") data = { bundles: {}, resources: {} };
      else throw new Error(`Unexpected request: ${url}`);
      return { json: async () => ({ ok: true, data }) };
    },
  });
  ui.state.page = "browse";
  const pendingList = ui.loadResources();
  ui.state.assetType = "text";
  const refreshing = ui.refreshBrowse();
  resources.resolve({ items: [{ name: "old texture", bundle: "old.bundle" }], total: 1 });
  await pendingList;
  assert.equal(ui.state.resources.length, 0, "Old resources appeared while new categories were loading");
  categories.resolve([{ id: "all", label: "全部", count: 0 }]);
  await refreshing;
  assert.equal(ui.state.resources.length, 0);

  const lateDetail = deferred();
  const hidden = loadScript("app.js", "state, loadResources", {
    fetch: async () => ({ json: async () => await lateDetail.promise }),
  });
  hidden.state.page = "browse";
  const pendingHidden = hidden.loadResources();
  hidden.state.page = "studio";
  hidden.state.selection = { asset_type: "text", name: "Current", bundle: "current.bundle" };
  lateDetail.resolve({ ok: true, data: { items: [], total: 0 } });
  await pendingHidden;
  assert.equal(hidden.state.selection.name, "Current", "Hidden browse response cleared the studio selection");
}

async function checkAudioClearCandidate() {
  const revoked = [];
  const ui = loadScript("audio.js", "state, init, syncButtons", {
    URL: { revokeObjectURL: (url) => revoked.push(url) },
  });
  ui.init({ headers: {}, call: async () => ({ items: [] }), toast() {} });
  ui.state.active = true;
  ui.state.selected = { id: "bank:42", name: "音效", editable: true };
  ui.state.draft = [{ id: "bank:42", name: "音效" }];
  ui.state.candidate = { ticket: "old-ticket", name: "replacement.wem" };
  ui.state.players.set("replacement", {
    element: ui.find("audio-replacement-player"), abort: new AbortController(),
    objectUrl: "blob:candidate", ready: true,
  });
  ui.syncButtons();
  assert.equal(ui.find("audio-save").disabled, false, "Fixture must start with a savable candidate");
  await ui.find("audio-clear-accept").trigger("click");
  assert.equal(ui.state.candidate, null, "Cleared audio draft retained an invalidated candidate ticket");
  assert.equal(ui.find("audio-save").disabled, true);
  assert.equal(ui.state.players.has("replacement"), false, "Cleared candidate preview was still active");
  assert.deepEqual(revoked, ["blob:candidate"]);
  assert.equal(ui.find("audio-replacement-badge").textContent, "尚未选择");
}

async function checkAudioFailedClearPreservesCandidate() {
  const ui = loadScript("audio.js", "state, init, syncButtons");
  ui.init({ headers: {}, call: async () => { throw new Error("文件被占用"); }, toast() {} });
  ui.state.selected = { id: "bank:42", editable: true };
  ui.state.draft = [{ id: "bank:42" }];
  ui.state.candidate = { ticket: "valid-ticket" };
  ui.state.players.set("replacement", { ready: true });
  await ui.find("audio-clear-accept").trigger("click");
  assert.equal(ui.state.draft.length, 1, "Failed clear discarded the existing draft");
  assert.equal(ui.state.candidate.ticket, "valid-ticket", "Failed clear discarded a valid candidate");
  assert.equal(ui.find("audio-save").disabled, false);
  assert.match(ui.find("audio-operation-status").textContent, /文件被占用/);
}

async function main() {
  const checks = [checkBrowseRace, checkLateResourceList, checkDraftDetailFailure, checkAudioClearCandidate, checkAudioFailedClearPreservesCandidate];
  const failures = [];
  for (const check of checks) {
    try { await check(); console.log(`PASS: ${check.name}`); }
    catch (error) { failures.push(error); console.error(`FAIL: ${check.name}: ${error.message}`); }
  }
  assert.equal(failures.length, 0, `${failures.length} frontend regressions failed`);
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
