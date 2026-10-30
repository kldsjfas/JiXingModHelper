/* 手动客户端选择取消、失败及重复点击都不应误清工作台。 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function main() {
  const elements = new Map();
  function find(id) {
    if (!elements.has(id)) elements.set(id, {
      disabled: true, textContent: "", events: {},
      addEventListener(name, action) { this.events[name] = action; },
    });
    return elements.get(id);
  }
  const context = vm.createContext({ window: {}, document: { getElementById: find } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/game-location.js"), "utf8"), context);
  const requests = [], changes = [], notices = [];
  let response = null;
  let complete;
  let blocked = false;
  const bridge = {
    async api(...args) {
      requests.push(args);
      if (blocked) return await new Promise((resolve) => { complete = resolve; });
      if (response instanceof Error) throw response;
      return response;
    },
    onChanged(value) { changes.push(value); }, toast(...args) { notices.push(args); },
  };
  context.window.GameLocation.init(bridge);
  context.window.GameLocation.init(bridge);
  context.window.GameLocation.update({ game_location_mode: "manual", game_detection_note: "saved client" });
  assert.equal(find("game-location-status").textContent, "saved client");
  assert.equal(find("choose-game-program").disabled, false);
  await find("choose-game-program").events.click();
  assert.deepEqual(requests[0], ["choose_game_location", "program"]);
  assert.equal(changes.length, 0, "cancel cleared the workbench");
  assert.equal(find("game-location-status").textContent, "saved client");
  response = new Error("wrong executable");
  await find("choose-game-folder").events.click();
  assert.equal(changes.length, 0);
  assert.equal(notices.at(-1)[0], "wrong executable");
  assert.equal(find("choose-game-folder").disabled, false);
  blocked = true;
  const pending = find("choose-game-program").events.click();
  const before = requests.length;
  await find("auto-game-location").events.click();
  assert.equal(requests.length, before, "duplicate change started while picker was active");
  const selected = { dashboard: { game_location_mode: "manual", game_exe: "INT.exe" }, installed: [] };
  complete(selected);
  await pending;
  assert.equal(changes[0], selected);
  assert.equal(find("choose-game-folder").disabled, false);
  blocked = false;
  response = { dashboard: { game_location_mode: "auto" }, installed: [] };
  await find("auto-game-location").events.click();
  assert.deepEqual(requests.at(-1), ["restore_automatic_game_location"]);
  assert.equal(changes.length, 2);
  console.log("game location UI: passed");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
