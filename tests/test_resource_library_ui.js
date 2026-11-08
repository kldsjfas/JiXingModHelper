/* 缩略图只读、翻页隔离与网格重绘回归。 */
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

function loadFrontend(responder) {
  const elements = new Map();
  function makeElement() {
    const classes = new Set();
    const attributes = new Map();
    const handlers = new Map();
    let html = "";
    return {
      children: [], dataset: {}, textContent: "", value: "", disabled: false,
      get innerHTML() { return html; },
      set innerHTML(value) { html = value; this.children = []; },
      classList: {
        add: (name) => classes.add(name), remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
        toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
      },
      setAttribute: (name, value) => attributes.set(name, value),
      getAttribute: (name) => attributes.get(name),
      addEventListener: (name, callback) => handlers.set(name, callback),
      trigger: (name, event) => handlers.get(name)?.(event),
      appendChild(child) { this.children.push(child); },
      remove() {},
    };
  }
  const find = (selector) => {
    if (!elements.has(selector)) elements.set(selector, makeElement());
    return elements.get(selector);
  };
  const switches = ["grid", "list"].map((view) => {
    const button = makeElement();
    button.dataset.resourceView = view;
    return button;
  });
  const calls = [];
  const context = vm.createContext({
    console, URLSearchParams, setTimeout, clearTimeout,
    window: { location: { hash: "" }, addEventListener() {} },
    document: {
      readyState: "loading", addEventListener() {},
      querySelector: find,
      querySelectorAll: (selector) => selector === "[data-resource-view]" ? switches : [],
      createElement: makeElement,
    },
    fetch: async (url, options) => {
      const args = JSON.parse(options.body);
      calls.push({ url, args });
      const data = await responder(url, args);
      return { json: async () => ({ ok: true, data }) };
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/app.js"), "utf8");
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, "globalThis.ui = { state, renderResources, renderCategories, setResourceView, toggleSelectedResource, retryResourceThumbnails };\n})();"), context);
  const ui = context.ui;
  ui.state.page = "browse";
  ui.state.assetTypes = [{ id: "texture", label: "贴图" }, { id: "mesh", label: "3D 模型" }];
  return { ...ui, find, calls, switches };
}

const thumbnailData = "data:image/png;base64,YQ==";
const previewFor = (items) => ({ items: items.map((row) => ({ ...row, thumbnail_data: thumbnailData })) });
const row = (name) => ({ bundle: "characters.bundle", name, category_label: "角色卡面", actions: ["替换图片", "导出"] });

async function settleUntil(done) {
  for (let step = 0; step < 50 && !done(); step += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.ok(done(), "Thumbnail queue did not settle");
}

async function checkBatchesAndReadOnlySelection() {
  const first = deferred();
  let active = 0;
  let maxActive = 0;
  const ui = loadFrontend(async (url, args) => {
    assert.equal(url, "/api/get_resource_thumbnails");
    assert.equal(args[0], "texture");
    assert.ok(args[1].length <= 8);
    active += 1;
    maxActive = Math.max(maxActive, active);
    if (ui.calls.length === 1) await first.promise;
    active -= 1;
    return previewFor(args[1]);
  });
  const selection = { bundle: "selected.bundle", name: "keep-selected" };
  const replacement = { name: "keep-candidate.png" };
  ui.state.selection = selection;
  ui.state.replacement = replacement;
  ui.state.resources = Array.from({ length: 25 }, (_, index) => row(`Hero_${index}`));
  ui.state.totalResources = 25;
  ui.renderResources();
  const detachedThumbnail = ui.find("#resource-list").children[0].children[0];
  ui.setResourceView("list");
  ui.toggleSelectedResource("characters.bundle", "Hero_3");
  assert.equal(ui.calls.length, 1, "View changes duplicated the active batch");
  first.resolve();
  await settleUntil(() => !ui.state.thumbnailLoading && ui.state.thumbnailCache.size === 25);
  assert.equal(ui.calls.length, 4);
  assert.equal(maxActive, 1, "More than one thumbnail batch ran concurrently");
  assert.equal(ui.state.selection, selection);
  assert.equal(ui.state.replacement, replacement);
  assert.equal(ui.state.resourceView, "list");
  assert.equal(ui.state.selectedResources.length, 1);
  assert.doesNotMatch(detachedThumbnail.innerHTML, /<img/, "A detached render target received an image");
  assert.match(ui.find("#resource-list").children[0].children[0].innerHTML, /<img/);
  assert.equal(ui.switches[1].getAttribute("aria-pressed"), "true");
  ui.setResourceView("grid");
  assert.equal(ui.calls.length, 4, "Cached page thumbnails were fetched again");
}

async function checkOldPageCannotPaintNewPage() {
  const first = deferred();
  const ui = loadFrontend(async (_url, args) => {
    if (args[1][0].name === "old") await first.promise;
    return previewFor(args[1]);
  });
  ui.state.resources = [row("old")];
  ui.renderResources();
  ui.state.browseRequestSeq += 1;
  ui.state.resources = [row("new")];
  ui.state.thumbnailCache.clear();
  ui.renderResources();
  first.resolve();
  await settleUntil(() => !ui.state.thumbnailLoading && ui.state.thumbnailCache.size === 1);
  assert.equal(ui.calls.length, 2);
  assert.equal(ui.state.thumbnailCache.has("characters.bundle::old"), false);
  assert.equal(ui.state.thumbnailCache.has("characters.bundle::new"), true);
  assert.match(ui.find("#resource-list").children[0].children[1].innerHTML, /new/);
}

async function checkFailureRetryAndUnsafeImage() {
  let retry = false;
  const ui = loadFrontend(async (_url, args) => {
    if (retry) return previewFor(args[1]);
    return { items: args[1].map((item) => ({ ...item, thumbnail_data: item.name === "valid" ? thumbnailData : 'data:image/png;base64,YQ==" onerror="alert(1)' })) };
  });
  ui.state.resources = [row("valid"), row("invalid")];
  ui.renderResources();
  await settleUntil(() => !ui.state.thumbnailLoading);
  assert.equal(ui.state.thumbnailCache.get("characters.bundle::invalid").data, undefined);
  assert.match(ui.find("#thumbnail-status").textContent, /1 项未加载/);
  assert.equal(ui.find("#retry-thumbnails").classList.contains("is-hidden"), false);
  retry = true;
  ui.retryResourceThumbnails();
  await settleUntil(() => !ui.state.thumbnailLoading);
  assert.equal(ui.calls.length, 2);
  assert.equal(ui.calls[1].args[1].length, 1);
  assert.equal(ui.calls[1].args[1][0].name, "invalid");
  assert.equal(ui.find("#retry-thumbnails").classList.contains("is-hidden"), true);
}

async function checkTypesAndMetadataAreEscaped() {
  const ui = loadFrontend(async () => { throw new Error("No thumbnail request expected"); });
  ui.state.assetType = "mesh";
  ui.state.resources = [{ ...row('<script>"asset"</script>'), actions: ["<img onerror=bad>"] }];
  ui.renderResources();
  assert.equal(ui.calls.length, 0);
  const item = ui.find("#resource-list").children[0];
  assert.equal(item.children[0].textContent, "3D 模型");
  assert.match(item.children[1].innerHTML, /&lt;script&gt;/);
  assert.match(item.children[1].innerHTML, /&lt;img onerror=bad&gt;/);
  assert.doesNotMatch(item.children[1].innerHTML, /<script>|<img onerror/);
}

async function checkCategoryGroups() {
  const ui = loadFrontend(async () => ({}));
  ui.state.categories = [
    { id: "all", label: "全部", count: 10, group: "全部资源" },
    { id: "hand_card", label: "手牌", count: 4, group: "卡牌与角色" },
    { id: "hero_card", label: "角色卡面", count: 3, group: "卡牌与角色" },
    { id: "land", label: "地图格子", count: 3, group: "地图与对局" },
  ];
  ui.renderCategories();
  const headings = ui.find("#category-list").children.filter((element) => element.className === "category-group-title");
  assert.deepEqual(headings.map((heading) => heading.textContent), ["卡牌与角色", "地图与对局"]);
  const active = ui.find("#category-list").children.find((element) => element.className?.includes("is-active"));
  assert.equal(active.getAttribute("aria-pressed"), "true");
}

async function main() {
  const checks = [checkBatchesAndReadOnlySelection, checkOldPageCannotPaintNewPage, checkFailureRetryAndUnsafeImage,
    checkTypesAndMetadataAreEscaped, checkCategoryGroups];
  for (const check of checks) {
    await check();
    console.log(`PASS: ${check.name}`);
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
