/* Replay after frontend_browser_qa_setup.js and a Playwright CLI snapshot. */
async (page) => {
  const fixture = page.__frontendQa;
  if (!fixture) throw new Error("Run frontend_browser_qa_setup.js first");
  fixture.calls = [];
  fixture.pageErrors = [];
  fixture.audioCleared = false;
  const checks = [];
  const output = "output/playwright/bug-audit-oct10";
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const waitUntil = async (ready) => {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (ready()) return;
      await page.waitForTimeout(50);
    }
    throw new Error("Expected mock request did not arrive");
  };
  await page.reload();
  await page.waitForFunction(() => !document.getElementById("app").classList.contains("is-loading"));

  await page.locator('.nav-button[data-page="browse"]').click();
  await waitUntil(() => fixture.delayedCategories.length > 0);
  await page.getByLabel("资源类型", { exact: true }).selectOption("text");
  await page.waitForFunction(() => document.getElementById("category-list").textContent.includes("当前文本分类"));
  const oldResponse = page.waitForResponse((response) => response.url().endsWith("/api/get_categories") && response.request().postDataJSON()[0] === "texture");
  for (const resolve of fixture.delayedCategories.splice(0)) resolve();
  await oldResponse;
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const categories = await page.locator("#category-list").innerText();
  check(categories.includes("当前文本分类") && !categories.includes("过期贴图分类"), "Late texture categories replaced the visible text categories");
  const browseRequests = fixture.calls.filter((call) => call.name === "browse_assets");
  check(browseRequests.length === 1 && browseRequests[0].args[0] === "text" && browseRequests[0].args[1] === "dialogs", "Stale category response issued a resource query");
  await page.screenshot({ path: `${output}/01-browse-async.png`, fullPage: true });
  checks.push({ name: "browse_type_race", status: "PASS", categories, resourceRequests: browseRequests });

  await page.locator('.nav-button[data-page="pack"]').click();
  await page.getByRole("button", { name: "[图] Portrait", exact: true }).click();
  await page.waitForFunction(() => document.getElementById("draft-detail-title").textContent === "图片 · Portrait");
  for (const id of ["draft-replace", "draft-crop", "draft-remove"]) check(await page.locator(`#${id}`).isEnabled(), `${id} unavailable for loaded texture`);
  await page.getByRole("button", { name: "[文] Config", exact: true }).click();
  await waitUntil(() => fixture.delayedDetail.length > 0);
  for (const id of ["draft-replace", "draft-crop", "draft-remove"]) check(await page.locator(`#${id}`).isDisabled(), `${id} inherited previous controls while loading`);
  for (const resolve of fixture.delayedDetail.splice(0)) resolve();
  await page.waitForFunction(() => document.getElementById("draft-detail-title").textContent === "预览未加载 · Config");
  for (const id of ["draft-replace", "draft-crop", "draft-remove"]) check(await page.locator(`#${id}`).isDisabled(), `${id} enabled after failed detail`);
  await page.screenshot({ path: `${output}/02-draft-detail-failure.png`, fullPage: true });
  checks.push({ name: "draft_detail_failure", status: "PASS", controlsDisabledWhileLoading: true, controlsDisabledAfterFailure: true });

  await page.locator('.nav-button[data-page="audio"]').click();
  await page.locator("#audio-list button").first().click();
  await page.waitForFunction(() => document.getElementById("audio-original-player").readyState >= 1 && document.getElementById("audio-replacement-player").readyState >= 1);
  await page.getByRole("button", { name: "选择替换 WEM…", exact: true }).click();
  await page.waitForFunction(() => !document.getElementById("audio-save").disabled);
  check(await page.locator("#audio-replacement-badge").innerText() === "尚未保存", "Mock candidate did not reach savable state");
  const commitCount = fixture.calls.filter((call) => call.name === "audio_commit").length;
  await page.getByRole("button", { name: "清空音频作品集", exact: true }).click();
  await page.getByRole("button", { name: "确认清空草稿", exact: true }).click();
  await page.waitForFunction(() => document.getElementById("audio-draft-count").textContent === "0 项");
  check(await page.locator("#audio-save").isDisabled(), "Save remained enabled with an invalidated ticket");
  check(await page.locator("#audio-replacement-badge").innerText() === "尚未选择", "Candidate badge survived audio clear");
  check(await page.locator("#audio-replacement-player").getAttribute("src") === null, "Candidate blob URL survived audio clear");
  check(await page.locator("#audio-discard").isHidden(), "Discard button still offered a cleared candidate");
  check(fixture.calls.filter((call) => call.name === "audio_commit").length === commitCount, "Clear triggered an unexpected audio commit");
  await page.waitForFunction(() => !document.querySelector("#toast-region .toast"));
  await page.setViewportSize({ width: 1600, height: 1200 });
  await page.locator("#main-content").evaluate((element) => { element.scrollTop = 0; });
  await page.screenshot({ path: `${output}/03-audio-clear-candidate.png`, fullPage: true });
  checks.push({ name: "audio_clear_candidate", status: "PASS", saveDisabled: true, sourceRemoved: true, draftCount: 0 });

  check(fixture.pageErrors.length === 0, `Browser page errors: ${fixture.pageErrors.join("; ")}`);
  return { date: "2026-10-10", checks, pageErrors: fixture.pageErrors, actualHtmlAndJs: true,
    fixtureOnly: true, gameFilesOrRealDraftModified: false, screenshots: checks.map((_, index) => [
      `${output}/01-browse-async.png`, `${output}/02-draft-detail-failure.png`, `${output}/03-audio-clear-candidate.png`,
    ][index]) };
}
