/* 工坊的安全展示、分步安装、失败重试和异步候选隔离。 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element(tag = "div", id = "") {
  const classes = new Set();
  const handlers = new Map();
  const attributes = new Map();
  return {
    tag, id, children: [], textContent: "", value: "", disabled: false, focused: false,
    get innerHTML() { throw new Error("Workshop used an HTML parsing sink"); },
    set innerHTML(_) { throw new Error("Workshop used an HTML parsing sink"); },
    classList: {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
    },
    setAttribute(name, value) {
      assert.ok(!/^on/i.test(name), "String event handler attribute was added");
      assert.notEqual(name, "href", "Remote links must be opened through the backend allowlist");
      attributes.set(name, value);
    },
    getAttribute: (name) => attributes.get(name),
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, handler) { handlers.set(name, handler); },
    async trigger(name, event = {}) { return await handlers.get(name)?.(event); },
    querySelectorAll(selector) {
      const tags = selector.split(",").map((value) => value.trim());
      const found = [];
      const walk = (parent) => {
        for (const child of parent.children) {
          if (tags.includes(child.tag)) found.push(child);
          walk(child);
        }
      };
      walk(this);
      return found;
    },
    reportValidity: () => true,
    focus() { this.focused = true; },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function visibleText(element) {
  return [element.textContent, ...element.children.map(visibleText)].join("\n");
}

function loadWorkshop() {
  const elements = new Map();
  const find = (id) => {
    if (!elements.has(id)) elements.set(id, element("div", id));
    return elements.get(id);
  };
  find("workshop-install-confirm").classList.add("is-hidden");
  const requests = [];
  const replies = new Map();
  const notices = [];
  const installed = [];
  const context = vm.createContext({
    window: {}, console,
    document: { getElementById: find, createElement: (tag) => element(tag) },
  });
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/workshop.js"), "utf8");
  const hooks = "state, showDetail, refresh, prepare, beginInstall, confirmInstall, install, chooseSubmission, exportSubmission, uploadSubmission, loadUploadStatus";
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, `globalThis.ui = { ${hooks} };\n})();`), context);
  context.window.ModWorkshop.init({
    async call(name, options, ...args) {
      requests.push({ name, options, args });
      const queue = replies.get(name) || [];
      assert.ok(queue.length, `Unexpected call: ${name}`);
      const reply = queue.shift();
      if (reply instanceof Error) throw reply;
      return await reply;
    },
    toast: (...args) => notices.push(args),
    onInstall: (data) => installed.push(data),
  });
  const reply = (name, result) => {
    if (!replies.has(name)) replies.set(name, []);
    replies.get(name).push(result);
  };
  return { ...context.ui, publicApi: context.window.ModWorkshop, find, requests, reply, notices, installed };
}

function entry(id = "first") {
  return {
    id, name: "猫猫贴图", author: "作者", description: "资源说明", category: "texture",
    version: "1.0", game_version: "2026-10", asset: { sha256: id.repeat(64).slice(0, 64), size: 2048 },
  };
}

function prepared(item, canInstall = true, conflicts = 0) {
  return {
    entry: item, inspection: { files: ["1.bundle", "2.bundle"], size: 4096, sha256: item.asset.sha256 },
    analysis: { matched: 2, unmatched: 0, conflicts, conflict_names: conflicts ? ["已有贴图"] : [], can_install: canInstall },
  };
}

async function loadEntries(ui, mods) {
  ui.reply("workshop_catalog", { mods });
  await ui.refresh();
}

async function checkSafeDisplayAndSearch() {
  const ui = loadWorkshop();
  const dangerous = '<img src=x onerror="alert(1)">';
  const item = { ...entry(), name: dangerous, author: "javascript:alert(1)", description: "<script>attack()</script>", cover_url: "https://attacker.invalid/a.svg" };
  await loadEntries(ui, [item, { ...entry("second"), category: "audio" }]);
  assert.equal(ui.find("workshop-results").children.length, 2);
  const firstButton = ui.find("workshop-results").children[0];
  assert.equal(firstButton.children[0].children[0].textContent, dangerous);
  await firstButton.trigger("click");
  assert.equal(ui.find("workshop-detail-title").textContent, dangerous);
  assert.equal(ui.find("workshop-detail-author").textContent, "javascript:alert(1)");
  assert.equal(ui.find("workshop-detail-description").textContent, item.description);
  assert.equal(ui.find("workshop-detail-sha256").textContent, item.asset.sha256);
  assert.equal(ui.find("workshop-detail-size").textContent, "2.0 KB");
  assert.equal(ui.find("workshop-install").textContent, "安装");
  assert.equal(ui.find("workshop-install-accept").disabled, true);
  ui.find("workshop-search").value = "alert";
  await ui.find("workshop-search").trigger("input");
  assert.equal(ui.find("workshop-results").children.length, 1);
  ui.find("workshop-category").value = "audio";
  await ui.find("workshop-category").trigger("change");
  assert.match(ui.find("workshop-results").children[0].children[0].textContent, /没有找到/);
  ui.reply("workshop_open_link", true);
  await ui.find("workshop-report").trigger("click");
  const report = ui.requests.at(-1);
  assert.deepEqual(Array.from(report.args), ["report", item.id]);
  assert.equal(ui.requests.filter((request) => request.name === "workshop_prepare").length, 0);
}

async function checkEmptyAndNetworkRetry() {
  const ui = loadWorkshop();
  ui.reply("workshop_catalog", new Error("连接超时"));
  await ui.refresh();
  assert.match(ui.find("workshop-catalog-status").textContent, /失败.*连接超时.*重试/);
  assert.equal(ui.find("workshop-refresh").disabled, false);
  assert.equal(ui.state.loaded, false);
  ui.reply("workshop_catalog", { mods: [], message: "首批作品正在征集。" });
  await ui.find("workshop-refresh").trigger("click");
  assert.equal(ui.state.loaded, true);
  assert.equal(ui.find("workshop-catalog-status").textContent, "首批作品正在征集。");
  assert.match(ui.find("workshop-results").children[0].children[0].textContent, /暂时没有已审核/);
  assert.equal(ui.find("workshop-install").disabled, true);
  ui.reply("workshop_catalog", { mods: [], status: "unavailable", message: "目录获取或验证失败；已停止下载，请检查网络后刷新。" });
  await ui.refresh(true);
  assert.equal(ui.state.loaded, false, "Backend unavailable result was treated as a loaded directory");
  assert.equal(ui.find("workshop-catalog-status").classList.contains("is-error"), true);
}

async function checkTwoStepInstallAndPageLeave() {
  const ui = loadWorkshop();
  const item = entry();
  await loadEntries(ui, [item]);
  ui.showDetail(item);
  await ui.install();
  assert.equal(ui.requests.length, 1, "An unchecked work could be installed");
  ui.reply("workshop_prepare", prepared(item, true, 1));
  await ui.find("workshop-install").trigger("click");
  assert.equal(ui.requests.at(-1).name, "workshop_prepare");
  assert.deepEqual(Array.from(ui.requests.at(-1).args), [item.id, item.asset.sha256]);
  assert.equal(ui.find("workshop-install").disabled, false);
  assert.match(ui.find("workshop-check-details").textContent, /可匹配 2.*冲突 1/);
  assert.match(ui.find("workshop-conflicts").textContent, /已有贴图/);
  assert.equal(ui.requests.length, 2, "The main button installed without separate confirmation");
  assert.equal(ui.find("workshop-install-confirm").classList.contains("is-hidden"), false);
  assert.equal(ui.find("workshop-install").classList.contains("is-hidden"), true);
  assert.match(ui.find("workshop-install-confirm-text").textContent, /已有 Mod 的效果可能受影响/);
  await ui.find("workshop-install-cancel").trigger("click");
  assert.equal(ui.find("workshop-install").classList.contains("is-hidden"), false);
  assert.equal(ui.find("workshop-install").textContent, "继续安装");
  await ui.install();
  assert.equal(ui.requests.length, 2, "Cancel retained active confirmation");
  await ui.find("workshop-install").trigger("click");
  assert.equal(ui.requests.length, 2, "A checked work was downloaded again after cancellation");
  ui.publicApi.leave();
  assert.equal(ui.find("workshop-install-confirm").classList.contains("is-hidden"), true);
  await ui.install();
  assert.equal(ui.requests.length, 2, "Page leave retained pending install confirmation");
  await ui.find("workshop-install").trigger("click");
  ui.reply("workshop_install", { name: item.name, matched: 2, installed: [{ name: item.name }] });
  await ui.find("workshop-install-accept").trigger("click");
  assert.equal(ui.requests.at(-1).name, "workshop_install");
  assert.equal(ui.installed.length, 1);
  assert.equal(ui.find("workshop-install-accept").disabled, true);
  assert.equal(ui.state.prepared, null);
}

async function checkFailuresAndAsyncCandidate() {
  const ui = loadWorkshop();
  const first = entry();
  const second = entry("second");
  await loadEntries(ui, [first, second]);
  ui.showDetail(first);
  ui.reply("workshop_prepare", prepared(first, false));
  await ui.prepare();
  assert.equal(ui.find("workshop-install-accept").disabled, true, "Missing game was installable");
  ui.confirmInstall();
  assert.equal(ui.find("workshop-install-confirm").classList.contains("is-hidden"), true);
  const download = deferred();
  ui.reply("workshop_prepare", download.promise);
  const request = ui.prepare();
  assert.equal(ui.find("workshop-refresh").disabled, true);
  ui.showDetail(second);
  download.resolve(prepared(first));
  await request;
  assert.equal(ui.state.prepared, null, "Older download became the newly selected candidate");
  assert.equal(ui.find("workshop-detail-title").textContent, second.name);
  ui.reply("workshop_prepare", prepared(first));
  await ui.prepare();
  assert.equal(ui.state.prepared, null, "Mismatched backend entry was accepted");
  assert.match(ui.find("workshop-analysis").textContent, /与当前作品不一致/);
  ui.reply("workshop_prepare", prepared(second));
  await ui.prepare();
  ui.confirmInstall();
  ui.reply("workshop_install", new Error("系统扫描不可用，已拒绝安装"));
  await ui.install();
  assert.equal(ui.find("workshop-install-accept").disabled, true);
  assert.equal(ui.state.prepared, null);
  assert.match(ui.find("workshop-analysis").textContent, /已拒绝安装/);
  ui.reply("workshop_catalog", new Error("下架目录不可用"));
  await ui.refresh(true);
  assert.equal(ui.state.selected, null);
  assert.equal(ui.state.items.length, 0, "Failed refresh kept stale installable items");
}

async function checkSubmissionBoundary() {
  const ui = loadWorkshop();
  await ui.exportSubmission();
  assert.equal(ui.requests.length, 0);
  ui.reply("workshop_choose_submission", { path_label: "外部工具制作.zip", inspection: { file_count: 2, total_size: 4096, sha256: "a".repeat(64), mod_info: null } });
  await ui.chooseSubmission();
  assert.equal(ui.find("workshop-export-submission").disabled, false);
  assert.match(ui.find("workshop-submission-file").textContent, /外部工具制作.zip.*检查通过/);
  const fields = { name: " <b>猫猫</b> ", author: " 作者 ", description: "说明<script>attack()</script>", category: "emote", version: "1.0", game_version: "2026-10" };
  for (const [field, value] of Object.entries(fields)) ui.find(`workshop-submit-${field}`).value = value;
  ui.reply("workshop_export_submission", { path: "D:\\投稿.zip", report_path: "D:\\投稿.submission.json", issue_url: "javascript:attack()" });
  await ui.exportSubmission({ preventDefault() {} });
  const metadata = ui.requests.at(-1).args[0];
  assert.equal(metadata.name, "<b>猫猫</b>");
  assert.equal(metadata.description, fields.description);
  assert.ok(!("id" in metadata));
  assert.match(ui.find("workshop-submission-result").textContent, /本地副本已导出.*尚未上传/);
  assert.match(ui.find("workshop-submission-export-details").textContent, /检查报告.*submission\.json/);
  assert.doesNotMatch(ui.find("workshop-submission-result").textContent, /javascript/);
  assert.equal(ui.requests.filter((request) => request.name === "submit_workshop_upload").length, 0,
    "Exporting a local copy automatically uploaded it");
  ui.reply("workshop_choose_submission", { cancelled: true });
  await ui.chooseSubmission();
  assert.equal(ui.state.submission, null);
  assert.equal(ui.find("workshop-export-submission").disabled, true);
  assert.match(ui.find("workshop-submission-file").textContent, /支持其他工具/);
}

function uploadService(submissions = [], accepting = true) {
  return { configured: true, accepting_uploads: accepting, submissions, max_package_bytes: 100000 };
}

async function chooseUpload(ui) {
  ui.reply("workshop_choose_submission", { path_label: "作品.zip", inspection: { file_count: 2, total_size: 4096, sha256: "a".repeat(64) } });
  await ui.chooseSubmission();
  for (const [field, value] of Object.entries({ name: "猫猫贴图", author: "作者", description: "作品说明", category: "texture", version: "1.0.0", game_version: "2026-10" })) {
    ui.find(`workshop-submit-${field}`).value = value;
  }
  await ui.find("workshop-submit-form").trigger("input");
}

async function checkUploadRequiresLiveService() {
  const entered = loadWorkshop();
  entered.reply("workshop_catalog", { mods: [] });
  entered.reply("get_workshop_upload_status", { configured: false, accepting_uploads: false, submissions: [] });
  entered.publicApi.enter();
  assert.deepEqual(Array.from(entered.requests.find((request) => request.name === "get_workshop_upload_status").args), [true],
    "Entering workshop did not request a current upload service health check");
  const ui = loadWorkshop();
  await ui.uploadSubmission();
  assert.equal(ui.requests.length, 0);
  await chooseUpload(ui);
  assert.equal(ui.find("workshop-upload-submission").disabled, true);
  assert.match(ui.find("workshop-upload-service-status").textContent, /上传服务暂未开通/);
  ui.reply("get_workshop_upload_status", { configured: false, accepting_uploads: false, submissions: [] });
  await ui.loadUploadStatus(true);
  await ui.find("workshop-submit-form").trigger("submit", { preventDefault() {} });
  assert.equal(ui.requests.filter((request) => request.name === "submit_workshop_upload").length, 0);
  ui.reply("get_workshop_upload_status", uploadService([], false));
  await ui.loadUploadStatus(true);
  assert.equal(ui.find("workshop-upload-submission").disabled, true, "Configured but offline service allowed upload");
  assert.match(ui.find("workshop-upload-service-status").textContent, /暂未接收作品/);
  ui.reply("get_workshop_upload_status", uploadService());
  await ui.find("workshop-refresh-submissions").trigger("click");
  assert.equal(ui.find("workshop-upload-submission").disabled, false);
  assert.match(ui.find("workshop-upload-service-status").textContent, /单个 ZIP 最多 97\.7 KB/);
  assert.deepEqual(Array.from(ui.requests.at(-1).args), [true]);
  ui.find("workshop-submit-form").reportValidity = () => false;
  await ui.uploadSubmission();
  assert.equal(ui.requests.filter((request) => request.name === "submit_workshop_upload").length, 0,
    "Invalid metadata reached upload API");
}

async function checkUploadReceiptAndExplicitAction() {
  const ui = loadWorkshop();
  ui.reply("get_workshop_upload_status", uploadService());
  await ui.loadUploadStatus(true);
  await chooseUpload(ui);
  assert.equal(ui.requests.filter((request) => request.name === "submit_workshop_upload").length, 0,
    "Choosing ZIP uploaded without user action");
  ui.find("workshop-submit-name").value = " <b>作品</b> ";
  await ui.find("workshop-submit-form").trigger("input");
  const pending = deferred();
  ui.reply("submit_workshop_upload", pending.promise);
  const upload = ui.find("workshop-submit-form").trigger("submit", { preventDefault() {} });
  assert.equal(ui.find("workshop-upload-submission").disabled, true);
  await ui.uploadSubmission();
  assert.equal(ui.requests.filter((request) => request.name === "submit_workshop_upload").length, 1,
    "Repeated click caused concurrent upload");
  const metadata = ui.requests.at(-1).args[0];
  assert.equal(metadata.name, "<b>作品</b>");
  assert.ok(!("id" in metadata));
  assert.ok(!("path" in metadata));
  assert.ok(!("sha256" in metadata));
  pending.resolve({ submission_id: "sub-123", status: "pending", name: "<b>作品</b>", message: "等待人工审核。", secret: "DO_NOT_DISPLAY", sha256: "a".repeat(64), size: 4096 });
  await upload;
  assert.match(ui.find("workshop-submission-result").textContent, /上传完成，待人工审核.*sub-123/);
  assert.equal(ui.find("workshop-upload-submission").textContent, "已上传");
  assert.equal(ui.find("workshop-upload-submission").disabled, true);
  assert.equal(ui.find("workshop-my-submissions").classList.contains("is-hidden"), false);
  const receipt = ui.find("workshop-submission-receipts").children[0];
  assert.equal(receipt.children[0].children[0].textContent, "<b>作品</b>");
  assert.equal(receipt.children[0].children[1].textContent, "待审核");
  assert.equal(receipt.children[1].textContent, "投稿编号：sub-123");
  assert.doesNotMatch(visibleText(receipt), /DO_NOT_DISPLAY|sha256/);
  await ui.uploadSubmission();
  assert.equal(ui.requests.filter((request) => request.name === "submit_workshop_upload").length, 1,
    "Already uploaded form allowed accidental duplicate");
  ui.reply("workshop_export_submission", { path: "D:\\已上传作品副本.zip" });
  await ui.exportSubmission();
  assert.doesNotMatch(ui.find("workshop-submission-result").textContent, /尚未上传/,
    "Exporting uploaded work falsely reported it was not uploaded");
}

async function checkUploadRetryAndReviewRefresh() {
  const ui = loadWorkshop();
  ui.reply("get_workshop_upload_status", uploadService());
  await ui.loadUploadStatus(true);
  await chooseUpload(ui);
  ui.reply("submit_workshop_upload", new Error("连接中断"));
  await ui.uploadSubmission();
  assert.ok(ui.state.submission, "Network failure lost chosen ZIP");
  assert.equal(ui.find("workshop-submit-description").value, "作品说明");
  assert.equal(ui.find("workshop-upload-submission").disabled, false);
  assert.match(ui.find("workshop-submission-result").textContent, /上传未确认.*连接中断.*已保留.*重试/);
  const firstMetadata = ui.requests.at(-1).args[0];
  ui.reply("submit_workshop_upload", { submission_id: "sub-retry", status: "pending", name: "猫猫贴图", message: "等待人工审核。" });
  await ui.uploadSubmission();
  assert.deepEqual(ui.requests.at(-1).args[0], firstMetadata, "Retry changed original metadata");
  ui.find("workshop-submit-description").value = "更新作品说明";
  await ui.find("workshop-submit-form").trigger("input");
  assert.equal(ui.find("workshop-upload-submission").disabled, false);
  ui.reply("get_workshop_upload_status", new Error("状态服务超时"));
  await ui.loadUploadStatus(true);
  assert.equal(ui.find("workshop-upload-submission").disabled, true);
  assert.equal(ui.find("workshop-submission-receipts").children.length, 1, "Status failure discarded receipt");
  assert.match(ui.find("workshop-upload-service-status").textContent, /暂停上传/);
  const unsafeMessage = "<img src=x onerror=alert(1)>";
  ui.reply("get_workshop_upload_status", uploadService([{ submission_id: "sub-retry", name: "猫猫贴图", status: "approved", message: unsafeMessage, query_secret: "NOT_FOR_UI" }]));
  await ui.find("workshop-refresh-submissions").trigger("click");
  const receipt = ui.find("workshop-submission-receipts").children[0];
  assert.equal(receipt.children[0].children[1].textContent, "审核通过");
  assert.equal(receipt.children[2].textContent, unsafeMessage);
  assert.doesNotMatch(visibleText(receipt), /NOT_FOR_UI/);
  assert.equal(ui.find("workshop-upload-submission").disabled, false);
  ui.reply("workshop_choose_submission", { cancelled: true });
  await ui.chooseSubmission();
  assert.equal(ui.find("workshop-upload-submission").disabled, true);
  assert.equal(ui.state.uploadedFingerprint, "");
}

async function checkInvalidUploadReceipt() {
  const ui = loadWorkshop();
  ui.reply("get_workshop_upload_status", uploadService([null, false, {}, { submission_id: "valid-old", status: "pending", name: "旧投稿" }]));
  await ui.loadUploadStatus(true);
  assert.equal(ui.find("workshop-submission-receipts").children.length, 1);
  await chooseUpload(ui);
  ui.reply("submit_workshop_upload", { submission_id: { secret: "DO_NOT_DISPLAY" }, status: "pending" });
  await ui.uploadSubmission();
  assert.equal(ui.state.uploadedFingerprint, "");
  assert.equal(ui.find("workshop-upload-submission").disabled, false);
  assert.match(ui.find("workshop-submission-result").textContent, /未返回有效的审核回执/);
  assert.doesNotMatch(ui.find("workshop-submission-result").textContent, /DO_NOT_DISPLAY/);
  assert.equal(ui.find("workshop-submission-receipts").children.length, 1);
}

async function checkOptionalMetadataAndRetry() {
  const ui = loadWorkshop();
  ui.reply("get_workshop_upload_status", uploadService());
  await ui.loadUploadStatus(true);
  await chooseUpload(ui);
  for (const field of ["author", "category", "version", "game_version"]) {
    ui.find(`workshop-submit-${field}`).value = "  ";
  }
  await ui.find("workshop-submit-form").trigger("input");
  ui.reply("submit_workshop_upload", new Error("回执连接中断"));
  await ui.uploadSubmission();
  const firstMetadata = ui.requests.at(-1).args[0];
  assert.equal(firstMetadata.name, "猫猫贴图");
  assert.equal(firstMetadata.description, "作品说明");
  assert.equal(firstMetadata.author, "匿名作者");
  assert.equal(firstMetadata.category, "mixed");
  assert.equal(firstMetadata.version, "1.0.0");
  assert.equal(firstMetadata.game_version, "");
  assert.equal(ui.find("workshop-upload-submission").disabled, false);

  for (const [field, value] of Object.entries({ author: "匿名作者", category: "mixed", version: "1.0.0" })) {
    ui.find(`workshop-submit-${field}`).value = value;
  }
  await ui.find("workshop-submit-form").trigger("input");
  ui.reply("submit_workshop_upload", { submission_id: "sub-anonymous", status: "pending", name: "猫猫贴图" });
  await ui.uploadSubmission();
  assert.deepEqual(ui.requests.at(-1).args[0], firstMetadata,
    "Blank optional fields and their displayed defaults produced different retry metadata");
  const uploadedFingerprint = ui.state.uploadedFingerprint;

  for (const field of ["author", "category", "version"]) ui.find(`workshop-submit-${field}`).value = "";
  await ui.find("workshop-submit-form").trigger("input");
  assert.equal(ui.state.uploadedFingerprint, uploadedFingerprint);
  assert.equal(ui.find("workshop-upload-submission").disabled, true,
    "Clearing optional defaults made the uploaded work available for duplicate upload");
  await ui.uploadSubmission();
  assert.equal(ui.requests.filter((request) => request.name === "submit_workshop_upload").length, 2);
}

async function checkInstallWhileLeavingPage() {
  const ui = loadWorkshop();
  const item = entry();
  await loadEntries(ui, [item]);
  ui.showDetail(item);
  const download = deferred();
  ui.reply("workshop_prepare", download.promise);
  const click = ui.find("workshop-install").trigger("click");
  ui.publicApi.leave();
  download.resolve(prepared(item));
  await click;
  assert.equal(ui.find("workshop-install-confirm").classList.contains("is-hidden"), true,
    "Late download opened installation confirmation after leaving the page");
  assert.equal(ui.find("workshop-install").textContent, "继续安装");
  assert.equal(ui.requests.filter((request) => request.name === "workshop_install").length, 0);
  await ui.install();
  assert.equal(ui.requests.length, 2, "Late download installed without confirmation");
}

function checkIntegration() {
  const html = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/index.html"), "utf8");
  const app = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/app.js"), "utf8");
  const source = fs.readFileSync(path.join(__dirname, "../astral_party_auto/webui/workshop.js"), "utf8");
  assert.ok(html.indexOf('src="workshop.js"') < html.indexOf('src="app.js"'));
  assert.ok(html.includes('href="workshop.css"'));
  assert.ok(html.includes('data-page="workshop"'));
  assert.ok(app.includes("window.ModWorkshop?.init"));
  assert.ok(app.includes("window.ModWorkshop?.enter"));
  assert.ok(app.includes("window.ModWorkshop?.leave"));
  assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|\beval\s*\(|\bfetch\s*\(|window\.open/);
  for (const phrase of ["浏览无需登录", "未审核的投稿", "不能保证零风险", "不会自动上传", "人工审核", "分享我的作品", "无需操作 GitHub", "上传服务暂未开通"]) {
    assert.ok(html.includes(phrase), `Missing workshop guidance: ${phrase}`);
  }
  const workshopSection = html.slice(html.indexOf('id="page-workshop"'), html.indexOf('id="page-browse"'));
  assert.ok(workshopSection.includes("制作工具不限"));
  assert.ok(workshopSection.includes("不接受程序或脚本"));
  assert.doesNotMatch(workshopSection + source, /请选择本工具导出|可先[在到].*我的作品集/,
    "Submission guidance still required an archive made by this helper");
  assert.ok(!html.includes('id="workshop-prepare"'), "The separate technical download button remained");
  assert.match(html, /id="workshop-share"[^>]*>\s*<summary>分享我的作品<\/summary>/);
  assert.doesNotMatch(html.match(/<details id="workshop-share"[^>]*>/)[0], /\sopen(?:\s|=|>)/);
  assert.ok(html.indexOf('id="workshop-choose-submission"') < html.indexOf('id="workshop-submit-form"'));
  const submissionForm = html.match(/<form id="workshop-submit-form">([\s\S]*?)<\/form>/)[1];
  const requiredFields = Array.from(submissionForm.matchAll(/<(?:input|textarea|select)\b[^>]*\brequired\b[^>]*>/g),
    (match) => match[0].match(/\bid="([^"]+)"/)[1]);
  assert.deepEqual(requiredFields, ["workshop-submit-name", "workshop-submit-description"]);
  const authorOptions = submissionForm.match(/<details\b[^>]*><summary>作者与分类（可选）<\/summary>([\s\S]*?)<\/details>/);
  assert.ok(authorOptions, "Author and category were not grouped as optional fields");
  assert.ok(authorOptions[1].includes('id="workshop-submit-author"'));
  assert.ok(authorOptions[1].includes('id="workshop-submit-category"'));
  assert.match(authorOptions[1], /<option value="mixed" selected>综合<\/option>/);
  assert.doesNotMatch(authorOptions[0].match(/<details\b[^>]*>/)[0], /\sopen(?:\s|=|>)/);
  assert.match(html, /id="workshop-submit-version"[^>]*value="1\.0\.0"/);
  assert.match(html, /id="workshop-upload-submission"[^>]*type="submit"[^>]*disabled>上传作品/);
  assert.match(html, /id="workshop-export-submission"[^>]*type="button"/);
  assert.ok(workshopSection.includes("点击上传会把选中的 ZIP 和作品介绍发送到工坊服务器"));
  assert.ok(!workshopSection.includes('id="workshop-open-submission"'));
  assert.doesNotMatch(workshopSection + source, /手动上传|打开投稿页|openLink\("submission"\)/);
}

(async () => {
  checkIntegration();
  await checkSafeDisplayAndSearch();
  await checkEmptyAndNetworkRetry();
  await checkTwoStepInstallAndPageLeave();
  await checkFailuresAndAsyncCandidate();
  await checkSubmissionBoundary();
  await checkUploadRequiresLiveService();
  await checkUploadReceiptAndExplicitAction();
  await checkUploadRetryAndReviewRefresh();
  await checkInvalidUploadReceipt();
  await checkOptionalMetadataAndRetry();
  await checkInstallWhileLeavingPage();
  console.log("Workshop UI safety: 12 checks passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
