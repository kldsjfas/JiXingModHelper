<p align="center">
  <img src="icon.png" width="120" alt="吉星派对 Mod 助手">
</p>

<h1 align="center">吉星派对 Mod 助手</h1>

<p align="center">
  <b>Astral Party · 本地换皮 Mod 工具</b><br>
  一个界面里搞定:找资源 → 看图 → 换图 → 打包 → 装进游戏 → 不满意一键还原
</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-blue">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-blue">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
  <img alt="UI" src="https://img.shields.io/badge/UI-HTML%20%2B%20WebView2-9b6dff">
</p>

---

给 Steam / TapTap 版《**吉星派对**》（ **星引擎 Party / Astral Party**）用的本地换皮工具。

## 为什么做这个

起因很简单：我想给游戏换个卡面、换张立绘。

照着网上的教程走一遍才发现有多折腾——先开 **dnSpy-net-win64** 翻资源结构，再用 **AssetStudio** 把一堆哈希命名的 `.bundle` 导出来、在里面找到那一张图，改完还得手动打包、覆盖回游戏目录。两个工具来回倒腾，全程看不到「改之前长啥样、改之后又长啥样」，万一改错了想还原更是要命。

折腾几次就烦了，干脆自己写了这个：**搜资源、预览、换图、生成 mod、装进游戏，全在一个窗口里；装之前自动备份，不喜欢一键还原。** 换皮该有的顺手劲儿，我尽量都做进去了。

> 它只替换游戏目录里 Addressable 的 `.bundle` 本地文件，装之前会把原文件备份好。**不注入进程、不读内存、不动反作弊那一套。**

---

## 目前能做到哪一步（说实话）

功能不是全都一样成熟，别被功能表唬住，看这里：

| 类型 | 状态 | 能干嘛 |
|------|------|--------|
| **贴图**（卡面 / 立绘 / 图标 / 筹码…） | ✅ **稳定** | 预览、替换、导出 PNG/JPG——**主力功能，就用它** |
| **文本** | 🧪 按资源支持 | 编辑普通 TextAsset 和可识别的 FairyGUI 文字字段，查找、批量替换、恢复原文 |
| **动画** | 🧪 按资源支持 | 序列帧替换、同源 AnimationClip 导入，支持的动画可播放并查看替换前后对照 |
| 3D 模型 | 👁 只读 | - |

**1.2.0 加入了文字编辑、动画替换与播放预览。** 贴图仍是最成熟的部分；文字和动画有格式限制，工具中的预览也不能代替游戏内验证。详细范围和操作步骤见[功能试用指南](PREVIEW_GUIDE.md)。

---

## 主要功能

- **装别人的 Mod**：选文件夹 / `.zip` / `.rar`，自动比对当前游戏版本，只装对得上的包，跳过版本不符的。
- **备份 & 还原**：装之前自动备份原文件；可以单个禁用 / 启用 / 卸载，也能侧栏「一键全还原」，随时退回没改过的样子。
- **浏览资源**：按类型和细分类（手牌、立绘、筹码、地图格子…）翻，中间点、右边预览。
- **自己做**：选中一张贴图 → 换成自己的图（可裁剪）→ 加进「作品集」→ 导出 ZIP 分享，或直接装进游戏。
- **改文字**：直接编辑普通文本和支持的 FairyGUI 字段，可查找、批量替换、恢复原文；不支持的二进制文本保持只读。
- **换动画、看效果**：独立序列帧可导入 GIF/APNG/动画 WebP 或 PNG 帧目录；支持单条 Sprite 动画轨的 AnimationClip 按原关键帧时序播放。浏览、制作和作品集都能暂停、逐帧、调倍速、点图放大，制作和作品集可看前后对照。
- **迁移旧 Mod**：选择旧资源包或旧 Mod 文件夹，自动匹配新版 bundle，把能确定的贴图加入作品集；重名不确定的会跳过并报告。

<p align="center">
  <img src="docs/screenshots/01_dashboard.png" width="720" alt="仪表盘">
</p>

---

## 快速开始

**要什么**：Windows 10/11、装了 Steam 或 TapTap 版《吉星派对》。不用装 Python。

**怎么用**：去 [Releases](https://github.com/kldsjfas/JiXingModHelper/releases) 下载 **v1.2.0** 压缩包，解压，双击 `JiXingModHelper.exe`。旧版 v1.1.0 不包含这次的文字编辑和动画预览改进。

> 备份、索引、作品集都写在 exe 同目录（`modkit_data`、`made_mods`），所以**整个文件夹一起拷**，别只拷一个 exe 出来。

打开之后：

1. 先看**仪表盘**是不是「游戏已连接」，没连上就点「刷新检测」。
2. 想装现成的：**Mod 管理** → 选文件夹/压缩包 → 看清「能装几个 / 跳过几个」 → 确认安装。
3. 想自己改：**浏览资源** → 选一张图 → **制作替换** → **我的作品集** → 导出或装进游戏。

---

## 图文步骤

### 浏览资源：找图

左边下拉选类型，贴图还能再点细分类（手牌、立绘、筹码、地图格子…），中间点一项右边就出预览。看中了直接「去制作替换」。

![浏览资源](docs/screenshots/02_browse.png)

> 小提示：列表如果空的，先点一次**刷新索引**（第一次要扫全部包，几千个，得等一会儿）。

### 装别人的 Mod

选好文件夹或压缩包，它会告诉你「能装 X 个、跳过 Y 个」——跳过的多半是旧版本的包，对不上当前游戏。确认安装会先备份，之后随时能禁用/卸载。

![Mod 管理](docs/screenshots/03_manage.png)

### 迁移旧 Mod

游戏更新后旧 bundle 哈希失效时，到「Mod 管理 → 迁移旧 Mod」选择旧资源包或整个旧 Mod 文件夹。工具会按资源组、贴图名和尺寸匹配当前版本；唯一匹配的贴图可以一键加入作品集，重名且无法确定的资源只会报告，不会盲目覆盖。

### 自己换图 → 作品集

浏览里选中 → 制作替换 → 选自己的图（可裁剪）→ 确认加进作品集。作品集里能对着看原图/新图，满意了导出 ZIP 或直接装。

![制作替换](docs/screenshots/05_studio.png)
![作品集](docs/screenshots/04_pack.png)

### 文字与动画（1.2.0）

- **文字**：类型选「文本」，可搜索 `Common_fui`、`Fight_fui` 或 `SettingList_fui`。进入「制作替换」后编辑右侧文字，保存到作品集再检查。
- **角色动作预览**：类型选「动画」，搜索 `Cry`、`Hit` 或 `Walk-back`，选中支持的片段就会播放。当前验证的资源中，`Cry` / `Hit` 为 30 帧、1 秒，`Walk-back` 为 24 帧、0.8 秒；资源名和内容可能随游戏版本变化。
- **序列帧替换**：类型选「动态图像」→「序列帧动画组」，例如 `lianxutu_blj`。导入动画图片或 PNG 帧目录后，右侧预览会展示按目标帧数和尺寸适配的效果。

保存到作品集只保存草稿；确认后再主动安装到游戏。AnimationClip 导入要求同源 `.animbin`，选图片则只替换关联图集。完整骨骼动画、多轨与位置/旋转/缩放动画、粒子和依赖场景的效果暂不支持完整预览，无法解析的资源会说明原因。工具不是 Unity 编辑器，不能把任意 GIF 直接变成角色骨骼动作。

---

## 原理

新版游戏会把热更新后的美术资源缓存在这儿：

```text
%USERPROFILE%\AppData\LocalLow\feimo\AstralParty_CN\com.unity.addressables\AssetBundles\<缓存键>\<包哈希>\__data
```

旧版资源仍可能位于：

```text
...\Astral Party\...\StreamingAssets\aa\StandaloneWindows64\*.bundle
```

工具会按当前 Addressables catalog 自动合并两种布局，把新版缓存里的 `__data` 映射回原本的 `.bundle` 名称；同名资源优先使用 AppData 热更新版本，其余资源从 Steam/TapTap 基础包读取。换皮说白了就是：**用改过贴图的同名 bundle 覆盖游戏实际读取的资源文件**。本工具用 [UnityPy](https://github.com/K0lb3/UnityPy) 读写这些 bundle，覆盖前先把原件备份到 `modkit_data/backups/`，所以还原只是把备份拷回去而已。

---

## 从源码跑 / 打包

使用 **Python 3.11 或更高版本**；当前固定的 NumPy 版本不支持 Python 3.10。CI 和发布构建使用 Python 3.11。

```powershell
# 装依赖
python -m pip install -r requirements.txt

# 直接跑（默认 HTML 前端，由原生 WebView2 窗口承载）
python -m astral_party_auto

# 打包成 exe
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

打包产物在 `dist\JiXingModHelper\`，双击 exe 启动。

已有同名输出目录时，构建会停止并保留旧包。再次打包可指定新目录，例如 `powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -Destination .\dist\v1.2.0`，产物会放在该目录下的 `JiXingModHelper\` 中。

### 提交改动前自检

装好依赖后，在仓库根目录运行：

```powershell
python selfcheck.py
python selfcheck_animation.py
python selfcheck_clip.py
python selfcheck_clip_preview.py
python -m unittest discover -s tests -p "test_*.py"
```

默认自检使用临时目录和模拟资源，检查迁移匹配、热更新缓存、Mod 启停与还原、作品集、文字和动画读写，不需要安装游戏，也不会修改真实游戏文件。`selfcheck.py` 出现 `ALL CHECKS PASSED`，其余测试显示通过，表示这些检查通过。使用自己的真实资源副本验证时，参见[功能试用指南](PREVIEW_GUIDE.md)。

每个 PR 都会运行这套自检；主分支和版本标签的构建也会先检查，通过后再打包。

---

## 常见问题

| 问题 | 处理 |
|------|------|
| 检测不到游戏 | 确认 Steam 或 TapTap 里装了游戏，点「刷新检测」 |
| 浏览/搜索是空的 | 点一次「刷新索引」，第一次扫几千个包要等一会儿 |
| 新版游戏可浏览的资源较少 | 先在游戏里打开相关角色/界面，让资源下载到本机缓存，再回工具刷新检测和索引 |
| 同名资源该选哪个 | 列表右侧会显示对应 bundle；逐项预览后选择需要的那一个 |
| 旧 Mod 迁移有项目被跳过 | 说明新版存在多个同名候选或已经没有该资源；为避免替错，工具不会自动处理歧义项 |
| 装完进游戏没变化 | 确认「能装数量 > 0」；重启游戏；看是不是被别的 mod 覆盖了 |
| 文字改了没显示 / 缺字 | 文案可能由服务器下发，或字体不含新增字形；支持编辑不代表游戏内一定会使用该字段 |
| 动画显示不能预览 | 当前只完整播放支持的序列帧、动画图片和单条 Sprite 动画轨；复杂骨骼、多轨或场景依赖资源会说明限制 |
| 动画替换后速度没变 | 独立序列帧替换保留游戏原来的帧数与播放规则，预览帧率只影响工具内查看 |
| RAR 解压失败 | 装个 7-Zip，或先手动解压成文件夹再选 |

---

## 说在前面

改的是你本机的游戏资源文件（有备份、能还原），联机别人是看不见的，只能本地过过眼瘾。

## 许可

[MIT](LICENSE)

## 致谢

- [UnityPy](https://github.com/K0lb3/UnityPy) — 读写 Unity 资源
- [WebView2](https://developer.microsoft.com/microsoft-edge/webview2/) — C# 原生窗口与 HTML 前端容器

有问题开 Issue，PR 也欢迎，尽量小而清楚就行。
