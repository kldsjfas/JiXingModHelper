/* 诊断重试、离页后的迟到请求、剪贴板失败与授权请求回归。 */
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

function loadUi() {
  const elements = new Map();
  const find = (id) => {
    if (!elements.has(id)) elements.set(id, {
      id, value: "", textContent: "", disabled: false,
      classList: { add() {}, remove() {}, toggle() {} },
      querySelector: (selector) => find(`${id}-${selector}`),
      focus() { this.focused = true; }, select() { this.selected = true; },
      remove() {}, removeAttribute() {},
    });
    return elements.get(id);
  };
  const responses = [], requests = [], copied = [];
  const clipboard = { async writeText(text) { copied.push(text); } };
  const context = vm.createContext({
    URLSearchParams, console, setTimeout: () => 0, clearTimeout() {}, navigator: { clipboard },
    window: { location: { hash: "#token=diagnostic-test-token" }, addEventListener() {} },
    document: {
      readyState: "loading", addEventListener() {}, body: find("body"),
      querySelector: (selector) => find(selector.slice(1)), querySelectorAll: () => [],
      createElement: () => find("created"),
    },
    fetch: async (url, options) => {
      requests.push({ url, options });
      assert.ok(responses.length, `Unexpected request: ${url}`);
      const response = responses.shift();
      return { json: async () => await response };
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/app.js"), "utf8");
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, "globalThis.ui = { state, checkResources, copyResourceDiagnostics, showPage, doAction };\n})();"), context);
  return { ...context.ui, find, responses, requests, copied, clipboard };
}

async function checks() {
  const ui = loadUi();
  ui.responses.push({ ok: true, data: { text: "current report" } });
  await ui.checkResources();
  assert.equal(ui.find("resource-diagnostics-output").value, "current report");
  assert.equal(ui.find("copy-resource-diagnostics").disabled, false);
  assert.equal(ui.requests[0].url, "/api/get_resource_diagnostics");
  assert.equal(ui.requests[0].options.headers["X-JiXing-Token"], "diagnostic-test-token");
  assert.equal(ui.requests[0].options.headers["Content-Type"], "application/json");
  assert.equal(ui.requests[0].options.body, "[]");
  await ui.copyResourceDiagnostics();
  assert.deepEqual(ui.copied, ["current report"]);

  ui.clipboard.writeText = async () => { throw new Error("denied"); };
  await ui.copyResourceDiagnostics();
  assert.equal(ui.find("resource-diagnostics-output").selected, true);
  assert.match(ui.find("resource-diagnostics-status").textContent, /Ctrl\+C/);

  ui.responses.push({ ok: false, error: "temporary failure" });
  await ui.checkResources();
  assert.match(ui.find("resource-diagnostics-status").textContent, /temporary failure/);
  assert.equal(ui.find("check-resources").disabled, false);
  assert.equal(ui.find("resource-diagnostics-output").value, "current report");

  const old = deferred();
  ui.responses.push(old.promise);
  const pending = ui.checkResources();
  assert.equal(ui.find("check-resources").disabled, true);
  await ui.checkResources();
  assert.equal(ui.requests.length, 3, "duplicate click must not submit another request");
  ui.showPage("manage");
  assert.equal(ui.state.resourceDiagnosticsLoading, false);
  assert.match(ui.find("resource-diagnostics-status").textContent, /检查已取消/);
  ui.showPage("dashboard");
  ui.responses.push({ ok: true, data: { text: "new report" } });
  await ui.checkResources();
  old.resolve({ ok: true, data: { text: "late old report" } });
  await pending;
  assert.equal(ui.find("resource-diagnostics-output").value, "new report");
  assert.equal(ui.find("check-resources").disabled, false);

  const copyDone = deferred();
  ui.clipboard.writeText = () => copyDone.promise;
  const copying = ui.copyResourceDiagnostics();
  ui.showPage("manage");
  const previousStatus = ui.find("resource-diagnostics-status").textContent;
  copyDone.resolve();
  await copying;
  assert.equal(ui.find("resource-diagnostics-status").textContent, previousStatus);

  ui.showPage("dashboard");
  ui.responses.push({ ok: true, data: { has_game: true, game_exe: "new install" } });
  await ui.doAction("refresh-detection");
  assert.equal(ui.state.resourceDiagnosticsText, "");
  assert.equal(ui.find("copy-resource-diagnostics").disabled, true);
  assert.equal(ui.find("resource-diagnostics-output").value, "");

  const html = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/index.html"), "utf8");
  for (const id of ["resource-diagnostics", "check-resources", "copy-resource-diagnostics", "resource-diagnostics-output", "resource-diagnostics-status"]) {
    assert.equal((html.match(new RegExp(`id="${id}"`, "g")) || []).length, 1);
  }
  assert.equal((html.match(/data-resource-diagnostics/g) || []).length, 2);
  assert.match(html, /readonly spellcheck="false"/);
  console.log("resource diagnostics UI: passed");
}

checks().catch((error) => { console.error(error); process.exitCode = 1; });
