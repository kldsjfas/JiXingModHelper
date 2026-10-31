/* 真实事件与倒序响应：映射、确认、重复保存和迟到预览不能串到另一项。 */
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

function setup() {
  const elements = new Map();
  function makeElement(tag = "div") {
    const classes = new Set();
    const attributes = new Map();
    const handlers = new Map();
    return {
      tagName: tag.toUpperCase(), className: "", children: [], value: "", textContent: "", disabled: false,
      checked: false, src: "", title: "", alt: "", max: 1,
      classList: {
        toggle(name, condition) { if (condition) classes.add(name); else classes.delete(name); },
        contains(name) { return classes.has(name); },
      },
      appendChild(child) { this.children.push(child); return child; },
      replaceChildren(...children) { this.children = children; },
      setAttribute(name, value) { attributes.set(name, value); },
      removeAttribute(name) { attributes.delete(name); if (name === "value") this.value = ""; },
      getAttribute(name) { return attributes.get(name); },
      addEventListener(name, callback) {
        if (!handlers.has(name)) handlers.set(name, []);
        handlers.get(name).push(callback);
      },
      async emit(name) {
        for (const handler of handlers.get(name) || []) await handler({ target: this });
      },
      handlerCount(name) { return (handlers.get(name) || []).length; },
      scrollIntoView() { this.scrolled = true; },
      focus() { this.focused = true; },
    };
  }
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };
  const context = vm.createContext({ window: {}, document: { getElementById: get, createElement: makeElement } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/emote-batch.js"), "utf8"), context);
  const requests = [], responses = [], saved = [], notices = [];
  const bridge = {
    async api(...args) {
      requests.push(args);
      assert.ok(responses.length, `unexpected API request: ${args[0]}`);
      const result = responses.shift();
      if (result instanceof Error) throw result;
      return await result;
    },
    onSaved(value) { saved.push(value); },
    toast(...args) { notices.push(args); },
  };
  const batch = context.window.EmoteBatch;
  batch.init(bridge);
  batch.init(bridge);
  batch.updateDraft({ name: "old", items: [] });
  batch.updateGame("current.exe");
  batch.enter();
  function row(index) {
    const item = get("emote-batch-list").children[index];
    const fields = item.children[1];
    return { item, character: fields.children[0].children[0], target: fields.children[1].children[0],
      preview: fields.children[2], notice: item.children[2] };
  }
  return { batch, get, row, requests, responses, saved, notices };
}

function fixture() {
  return {
    token: "batch-one",
    sources: [
      { source_id: "a", name: "120102001.gif", frames: 20, duration_ms: 1200, width: 256, height: 256, size: 4096, suggested_emote_id: 120102001 },
      { source_id: "b", name: "自己的动作.gif", frames: 25, duration_ms: 1600, width: 240, height: 240, size: 5000, suggested_emote_id: null },
      { source_id: "c", name: "120127002.gif", frames: 15, duration_ms: 900, width: 256, height: 256, size: 3000, suggested_emote_id: 120127002 },
    ],
    targets: [
      { emote_id: 120102001, character_id: 102, character: "芬妮", label: "芬妮 · 表情01", name: "UT_Item_Emoji_102_01" },
      { emote_id: 120102002, character_id: 102, character: "芬妮", label: "芬妮 · 表情02", name: "UT_Item_Emoji_102_02" },
      { emote_id: 120114001, character_id: 114, character: "蓝海晴", label: "蓝海晴 · 表情01", name: "UT_Item_Emoji_114_01" },
      { emote_id: 120127002, character_id: 127, character: "邦妮", label: "邦妮 · 表情02", name: "UT_Item_Emoji_127_02" },
    ],
    characters: [{ character_id: 102, label: "芬妮", count: 2 }, { character_id: 114, label: "蓝海晴", count: 1 },
      { character_id: 127, label: "邦妮", count: 1 }],
  };
}

async function choose(ui, value = fixture()) {
  ui.responses.push(value);
  await ui.get("emote-batch-choose").emit("click");
}

async function role(ui, index, characterId, emoteId) {
  const row = ui.row(index);
  row.character.value = String(characterId);
  await row.character.emit("change");
  row.target.value = String(emoteId);
  await row.target.emit("change");
}

async function confirm(ui) {
  ui.get("emote-batch-confirm").checked = true;
  await ui.get("emote-batch-confirm").emit("change");
}

function sourcePreview(source = "first", original = "first", sourceId = "a", emoteId = 120102001) {
  return { source_data: `data:image/gif;base64,${source}`, original_data: `data:image/png;base64,${original}`,
    source: { source_id: sourceId }, target: { emote_id: emoteId } };
}

function images(node) {
  return (node.tagName === "IMG" ? [node] : []).concat(...node.children.map(images));
}

async function mappingAndCommit() {
  const ui = setup();
  assert.equal(ui.get("emote-batch-choose").handlerCount("click"), 1, "init doubled click handlers");
  await choose(ui);
  assert.equal(ui.row(0).character.value, "102");
  assert.equal(ui.row(0).target.value, "120102001");
  assert.equal(ui.row(1).character.value, "", "unknown filename guessed a character");
  assert.equal(ui.row(1).target.children.length, 1, "unselected row rendered every character's slots");
  assert.equal(ui.row(0).target.children.length, 3, "row should show only its character's slots");
  assert.equal(ui.get("emote-batch-save").disabled, true);
  await confirm(ui);
  assert.equal(ui.get("emote-batch-confirm").checked, false, "incomplete mapping accepted confirmation");
  await role(ui, 1, 127, 120127002);
  assert.equal(ui.get("emote-batch-confirm").disabled, true);
  assert.equal(ui.row(1).item.classList.contains("is-error"), true);
  assert.equal(ui.row(2).item.classList.contains("is-error"), true);
  await role(ui, 1, 102, 120102002);
  assert.equal(ui.get("emote-batch-confirm").disabled, false);
  assert.equal(ui.get("emote-batch-save").disabled, true, "complete mapping skipped human confirmation");
  assert.match(ui.get("emote-batch-summary").textContent, /芬妮 2 个/);
  assert.match(ui.get("emote-batch-summary").textContent, /邦妮 1 个/);
  await confirm(ui);
  assert.equal(ui.get("emote-batch-save").disabled, false);
  const completion = deferred();
  ui.responses.push(completion.promise);
  const pending = ui.get("emote-batch-save").emit("click");
  assert.equal(ui.get("emote-batch-choose").disabled, true);
  assert.equal(ui.row(0).character.disabled, true);
  const requestCount = ui.requests.length;
  await ui.get("emote-batch-save").emit("click");
  await ui.get("emote-batch-choose").emit("click");
  assert.equal(ui.requests.length, requestCount, "duplicate save or picker started during commit");
  assert.equal(ui.batch.progress({ token: "other", stage: "converting", done: 1, total: 3 }), false);
  ui.batch.progress({ token: "batch-one", stage: "convert", done: 2, total: 3, label: "second.gif" });
  assert.equal(ui.get("emote-batch-progress").value, 2);
  assert.match(ui.get("emote-batch-status").textContent, /制作动画 2\/3 · second.gif/);
  const committed = { count: 3, draft: { name: "new", items: [{ kind: "emote" }] } };
  completion.resolve(committed);
  await pending;
  assert.equal(ui.saved[0], committed);
  assert.deepEqual(JSON.parse(JSON.stringify(ui.requests.at(-1))), ["save_emote_gif_batch", "batch-one", [
    { source_id: "a", emote_id: 120102001 }, { source_id: "b", emote_id: 120102002 }, { source_id: "c", emote_id: 120127002 },
  ]]);
  assert.equal(ui.get("emote-batch-list").children.length, 0);
  assert.match(ui.get("emote-batch-status").textContent, /已将 3 个动态表情保存/);
  assert.match(ui.notices.at(-1)[0], /尚未安装到游戏/);
  assert.equal(ui.batch.progress({ stage: "committing", done: 3, total: 3 }), false);
}

async function previewsDoNotCrossRowsOrPages() {
  const ui = setup();
  await choose(ui);
  assert.equal(images(ui.get("emote-batch-list")).length, 0, "all GIFs were loaded into the list");
  const slow = deferred();
  ui.responses.push(slow.promise);
  const first = ui.row(0).preview.emit("click");
  const before = ui.requests.length;
  await ui.row(0).preview.emit("click");
  assert.equal(ui.requests.length, before, "duplicate current preview was not blocked");
  ui.responses.push(sourcePreview("new-source", "new-original", "c", 120127002));
  await ui.row(2).preview.emit("click");
  assert.equal(ui.get("emote-batch-source").children[0].src, "data:image/gif;base64,new-source");
  slow.resolve(sourcePreview("old-source", "old-original"));
  await first;
  assert.equal(ui.get("emote-batch-source").children[0].src, "data:image/gif;base64,new-source", "late preview replaced another row");
  const late = deferred();
  ui.responses.push(late.promise);
  const pending = ui.row(0).preview.emit("click");
  ui.batch.leave();
  late.resolve(sourcePreview("after-leave", "after-leave"));
  await pending;
  assert.equal(images(ui.get("emote-batch-source")).length, 0, "late preview was applied after leaving page");
  ui.batch.enter();
  const changed = deferred();
  ui.responses.push(changed.promise);
  const preview = ui.row(0).preview.emit("click");
  await role(ui, 0, 114, 120114001);
  changed.resolve(sourcePreview("wrong-target", "wrong-target"));
  await preview;
  assert.equal(images(ui.get("emote-batch-original")).length, 0, "changed mapping kept the prior target's preview");
  ui.responses.push(new Error("read failed"));
  await ui.row(0).preview.emit("click");
  assert.match(ui.get("emote-batch-preview-status").textContent, /read failed/);
  assert.equal(ui.row(0).preview.disabled, false, "failed preview cannot be retried");
  ui.responses.push(sourcePreview("retried", "retried", "a", 120114001));
  await ui.row(0).preview.emit("click");
  assert.equal(images(ui.get("emote-batch-source")).length, 1);
  ui.responses.push(sourcePreview("wrong-response", "wrong-response", "c", 120127002));
  await ui.row(0).preview.emit("click");
  assert.equal(images(ui.get("emote-batch-source")).length, 0, "response metadata for another target was displayed");
  assert.match(ui.get("emote-batch-preview-status").textContent, /对应关系不一致/);
}

async function cancellationAndInvalidation() {
  const ui = setup();
  await choose(ui);
  const originalRow = ui.row(0).item;
  await choose(ui, null);
  assert.equal(ui.row(0).item, originalRow, "canceled picker discarded existing mappings");
  await choose(ui, new Error("not a GIF"));
  assert.equal(ui.row(0).item, originalRow, "failed picker discarded existing mappings");
  assert.match(ui.get("emote-batch-status").textContent, /not a GIF/);
  const slow = deferred();
  ui.responses.push(slow.promise);
  const pending = ui.get("emote-batch-choose").emit("click");
  ui.batch.invalidate("game changed");
  slow.resolve(fixture());
  await pending;
  assert.equal(ui.get("emote-batch-list").children.length, 0, "old game's picker returned into new context");
  assert.match(ui.get("emote-batch-status").textContent, /game changed/);
  assert.equal(ui.get("emote-batch-choose").disabled, false);
  await choose(ui);
  ui.batch.updateDraft({ name: "different", items: [] });
  assert.equal(ui.get("emote-batch-list").children.length, 0, "draft mutation left batch candidate active");
  await choose(ui);
  ui.batch.updateGame("another.exe");
  assert.equal(ui.get("emote-batch-list").children.length, 0, "automatic game reconnection kept a stale batch");
  const left = deferred();
  ui.responses.push(left.promise);
  const choosing = ui.get("emote-batch-choose").emit("click");
  ui.batch.leave();
  left.resolve(fixture());
  await choosing;
  ui.batch.enter();
  assert.equal(ui.get("emote-batch-list").children.length, 0, "picker result was accepted after leaving page");
}

async function savingFailureAndChangedContext() {
  const ui = setup();
  await choose(ui);
  await role(ui, 1, 102, 120102002);
  await confirm(ui);
  ui.responses.push(new Error("disk full"));
  await ui.get("emote-batch-save").emit("click");
  assert.equal(ui.saved.length, 0);
  assert.equal(ui.get("emote-batch-confirm").checked, false);
  assert.equal(ui.get("emote-batch-save").disabled, true);
  assert.match(ui.get("emote-batch-status").textContent, /disk full/);
  await confirm(ui);
  const save = deferred();
  ui.responses.push(save.promise);
  const pending = ui.get("emote-batch-save").emit("click");
  ui.batch.invalidate("game changed");
  save.resolve({ count: 3, draft: { name: "old game's result" } });
  await pending;
  assert.equal(ui.saved.length, 0, "late commit response overwrote the new game context");
  assert.match(ui.get("emote-batch-status").textContent, /game changed/);
  assert.equal(ui.get("emote-batch-choose").disabled, false);
}

async function main() {
  await mappingAndCommit();
  await previewsDoNotCrossRowsOrPages();
  await cancellationAndInvalidation();
  await savingFailureAndChangedContext();
  const root = path.join(__dirname, "../astral_party_auto/webui");
  const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
  assert.match(html, /<script src="emote-batch\.js" defer><\/script>/);
  assert.match(html, /id="emote-batch-from-studio"/);
  assert.match(html, /id="emote-batch-confirm" type="checkbox"/);
  const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
  assert.match(app, /event === "emote_batch_progress"/);
  assert.match(app, /window\.EmoteBatch\?\.updateDraft\(state\.draft\)/);
  console.log("emote batch UI: mapping, preview races, cancellation, atomic save and context guards passed");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
