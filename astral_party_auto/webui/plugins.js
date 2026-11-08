/* Cesium 功能插件有独立工程和构建状态。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { working: false, project: null, sourceHash: "", savedSource: "", environment: null };
  let bridge;

  function status(text, error = false) {
    $("plugin-status").textContent = text;
    $("plugin-status").classList.toggle("is-error", error);
  }

  function syncButtons() {
    const locked = state.working;
    ["plugin-create", "plugin-open-project", "plugin-choose-sdk", "plugin-fetch-sdk"].forEach((id) => {
      if ($(id)) $(id).disabled = locked;
    });
    $("plugin-source").disabled = locked || !state.project;
    $("plugin-save").disabled = locked || !state.project;
    $("plugin-build").disabled = locked || !state.project || !state.environment?.sdk || !state.environment?.dotnet;
  }

  function showEnvironment(environment) {
    state.environment = environment;
    const dependencies = [environment.sdk ? `已选择 SDK ${environment.sdk}` : "请下载官方 SDK 或选择已有 SDK 目录",
      environment.dotnet ? "已检测到 .NET SDK 命令" : "需要安装 .NET SDK 后重新打开助手"];
    $("plugin-sdk-status").textContent = dependencies.join("。 ");
    $("plugin-loader-status").textContent = environment.loader_present
      ? "已发现当前游戏的 CesiumLoader 文件。插件加载效果需进入游戏验证。"
      : "插件运行需要 CesiumLoader，可从下方官方仓库获取。";
    syncButtons();
  }

  function showProject(project) {
    state.project = project.project;
    state.sourceHash = project.source_hash;
    state.savedSource = project.source;
    $("plugin-source").value = project.source;
    $("plugin-project-path").textContent = project.project;
    $("plugin-build-log").textContent = "";
    syncButtons();
  }

  async function run(action, progress) {
    if (state.working) return;
    state.working = true;
    syncButtons();
    status(progress);
    try {
      await action();
    } catch (error) {
      status(error.message || "操作失败，请检查工程和开发组件。", true);
    } finally {
      state.working = false;
      syncButtons();
    }
  }

  function request(name, ...args) {
    return bridge.call(name, { quiet: true }, ...args);
  }

  async function saveSource() {
    const project = await request("plugin_save_source", $("plugin-source").value, state.sourceHash);
    state.sourceHash = project.source_hash;
    state.savedSource = project.source;
  }

  function templateFields() {
    const frameRate = $("plugin-template").value === "frame_rate";
    $("plugin-fps-field").classList.toggle("is-hidden", !frameRate);
    $("plugin-message-field").classList.toggle("is-hidden", frameRate);
  }

  function init(dependencies) {
    bridge = dependencies;
    $("plugin-template").addEventListener("change", templateFields);
    $("plugin-create").addEventListener("click", () => run(async () => {
      if (state.project && $("plugin-source").value !== state.savedSource) await saveSource();
      const project = await request("plugin_create_project", {
        template: $("plugin-template").value,
        id: $("plugin-id").value,
        name: $("plugin-name").value,
        author: $("plugin-author").value,
        fps: Number($("plugin-fps").value),
        message: $("plugin-message").value,
      });
      showProject(project);
      status("工程已创建，可以直接修改源码后编译。每次创建都会保留已有工程。");
    }, "创建插件工程…"));
    $("plugin-open-project").addEventListener("click", () => run(async () => {
      if (state.project && $("plugin-source").value !== state.savedSource) await saveSource();
      const project = await request("plugin_open_project");
      if (project) {
        showProject(project);
        status("工程已打开。");
      } else status("已取消选择工程。");
    }, "选择插件工程…"));
    $("plugin-choose-sdk").addEventListener("click", () => run(async () => {
      const environment = await request("plugin_choose_sdk");
      if (environment) showEnvironment(environment);
      status(environment ? "SDK 已选择。" : "已取消选择 SDK。");
    }, "选择开发 SDK…"));
    $("plugin-fetch-sdk")?.addEventListener("click", () => run(async () => {
      showEnvironment(await request("plugin_fetch_sdk"));
      status("官方 SDK 已下载并校验。可以创建并编译插件了。");
    }, "下载并校验官方 SDK 3.3.2…"));
    $("plugin-save").addEventListener("click", () => run(async () => {
      await saveSource();
      status("源码已保存。");
    }, "保存源码…"));
    $("plugin-build").addEventListener("click", () => run(async () => {
      await saveSource();
      const result = await request("plugin_build_project");
      $("plugin-build-log").textContent = result.log || "";
      if (result.success) {
        status(`插件 ZIP 已生成于 ${result.package}。需要 CesiumLoader 加载，游戏内效果待实测。`);
      } else status("编译失败，详细原因见下方构建日志。修正源码后可以重新编译。", true);
    }, "编译插件，首次还原 .NET 依赖可能需要一些时间…"));
    templateFields();
    syncButtons();
  }

  async function enter() {
    if (!bridge || state.working) return;
    try {
      showEnvironment(await request("plugin_status"));
    } catch (error) {
      status(error.message || "开发组件检测失败。", true);
    }
  }

  window.PluginStudio = { init, enter };
})();
