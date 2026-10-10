/* Playwright CLI run-code callback. Uses the actual page files and mocks only HTTP APIs. */
async (page) => {
  const fixture = {
    delayedCategories: [], delayedDetail: [], calls: [], pageErrors: [], audioCleared: false,
    draft: { name: "隔离测试作品集", items: [
      { kind: "texture", name: "Portrait", bundle: "test.bundle" },
      { kind: "text", name: "Config", bundle: "test.bundle" },
    ] },
    audio: { id: "test-bank:42", name: "测试音效", category: "effects", editable: true,
      description: "仅用于界面验证", sample_rate: 24000, channels: 1, codec: "PCM" },
  };
  page.__frontendQa = fixture;
  page.on("pageerror", (error) => fixture.pageErrors.push(error.message));
  const reply = (route, data) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, data }) });
  await page.route("**/poll?*", (route) => route.fulfill({ status: 200, contentType: "application/json", body: '{"events":[],"cursor":0}' }));
  await page.route("**/api/*", async (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop();
    const args = route.request().postDataJSON();
    fixture.calls.push({ name, args });
    let data;
    switch (name) {
      case "bootstrap":
        data = { dashboard: { has_game: true, game_name: "隔离测试", bundle_count: 1, installed_count: 0, backup_count: 0 },
          installed: [], draft: fixture.draft, logs: [], app_info: { version: "1.3.0" },
          assetTypes: [{ id: "texture", label: "贴图" }, { id: "text", label: "文本" }, { id: "anim", label: "动画" }] };
        break;
      case "check_for_updates":
        data = { status: "up_to_date", checking: false, current_version: "1.3.0", message: "隔离测试：没有访问 GitHub" };
        break;
      case "get_categories":
        if (args[0] === "texture") {
          await new Promise((resolve) => fixture.delayedCategories.push(resolve));
          data = [{ id: "hand_card", label: "过期贴图分类", count: 1 }];
        } else data = [{ id: "dialogs", label: "当前文本分类", count: 1 }];
        break;
      case "get_character_list": data = []; break;
      case "get_character_labels": data = { bundles: {}, resources: {} }; break;
      case "browse_assets": data = { items: [{ name: "Config", bundle: "test.bundle" }], total: 1 }; break;
      case "get_draft": data = fixture.draft; break;
      case "get_draft_detail":
        if (args[0] === 1) {
          await new Promise((resolve) => fixture.delayedDetail.push(resolve));
          return route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":false,"error":"测试：作品集文件丢失"}' });
        }
        data = { item: fixture.draft.items[0], original_data: "/app_icon.png", modified_data: "/app_icon.png" };
        break;
      case "audio_catalog": data = { items: [fixture.audio], total: 1,
        categories: [{ id: "all", label: "全部音频" }, { id: "effects", label: "音效" }],
        draft: fixture.audioCleared ? [] : [fixture.audio], status: { scanning: false, message: "隔离音频目录" } };
        break;
      case "audio_preview": data = { name: fixture.audio.name, url: "/audio-media/mock.wav", duration: 1, sample_rate: 24000, channels: 1, editable: true }; break;
      case "audio_choose_replacement": data = { name: "replacement.wem", ticket: "valid-test-ticket", url: "/audio-media/mock.wav", duration: 1, sample_rate: 24000, channels: 1 }; break;
      case "audio_clear_draft": fixture.audioCleared = true; data = { items: [] }; break;
      default: return route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ ok: false, error: `Unexpected test API: ${name}` }) });
    }
    await reply(route, data);
  });
  await page.route("**/audio-media/mock.wav", async (route) => {
    const samples = 24000;
    const wave = Buffer.alloc(44 + samples * 2);
    wave.write("RIFF", 0); wave.writeUInt32LE(wave.length - 8, 4); wave.write("WAVEfmt ", 8);
    wave.writeUInt32LE(16, 16); wave.writeUInt16LE(1, 20); wave.writeUInt16LE(1, 22);
    wave.writeUInt32LE(24000, 24); wave.writeUInt32LE(48000, 28); wave.writeUInt16LE(2, 32); wave.writeUInt16LE(16, 34);
    wave.write("data", 36); wave.writeUInt32LE(samples * 2, 40);
    for (let index = 0; index < samples; index++) wave.writeInt16LE(Math.round(Math.sin(index * 2 * Math.PI * 220 / 24000) * 2000), 44 + index * 2);
    await route.fulfill({ status: 200, contentType: "audio/wav", body: wave });
  });
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto("http://127.0.0.1:8797/");
  await page.waitForFunction(() => !document.getElementById("app").classList.contains("is-loading"));
  return { ready: true, title: await page.title(), fixtureOnly: true };
}
