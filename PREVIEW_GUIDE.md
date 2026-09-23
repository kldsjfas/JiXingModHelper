# 吉星派对 Mod 助手 1.2.0 功能试用指南

使用 [v1.2.0 Release](https://github.com/kldsjfas/JiXingModHelper/releases) 中的完整压缩包，解压后双击 `JiXingModHelper.exe`，请保留旁边的 `_internal` 文件夹。也可以按 [README 的源码运行说明](README.md#从源码跑--打包)安装依赖后运行 `python -m astral_party_auto`。

## 第一次用

1. 确认仪表盘显示游戏已连接，打开「浏览资源」，首次使用点「刷新索引」。如果收到的是为本机准备、且与当前游戏版本匹配的预置索引，可以直接使用；游戏更新、列表为空或换了电脑后应重新扫描。
2. 修改文字：类型选「文本」，搜索 `Common_fui`、`Fight_fui` 或 `SettingList_fui`。选中后点「去制作替换」，在右侧逐条改字，也可查找、批量替换或恢复原文。
3. 预览角色动作：类型选「动画」，搜索 `Cry`，选中支持的片段后自动播放，点画面可放大。当前验证的 `Cry`、`Hit` 均为 30 帧、1 秒，`Walk-back` 为 24 帧、0.8 秒；不同游戏版本的资源可能变化。
4. 修改独立序列帧：类型选「动态图像」，分类选「序列帧动画组」，可搜索 `lianxutu_blj`。选择 GIF、APNG、动画 WebP 或 PNG 帧图文件夹，在左右预览区查看原动画与适配后的候选效果。
5. 点「保存到作品集」只保存草稿。到「我的作品集」查看保存后的对照，确认后再主动点「安装到游戏」。建议先退出游戏，安装后重新打开验证。

作品集、备份和索引保存在程序自己的数据目录；打包版位于 exe 旁边。升级时保留旧版目录和备份。若另一份助手已经安装过 Mod，请先用原来那份助手停用或还原后再用新版本安装，以免两份助手各自管理的备份混淆。

## 支持范围

- **文字**：普通 TextAsset，以及可识别的 FairyGUI 界面文字字段，例如文本、输入提示、按钮标题和工具提示。普通文本保留原编码、BOM 和换行，JSON/XML 在写入前检查格式；结构化二进制保持只读。FairyGUI 只编辑识别出的文字字段，不把整个字符串表当作可替换文案；压缩包、未知版本和扩展字符串表暂不支持。
- **动画播放**：独立贴图序列、可解码的 Sprite 序列、GIF/APNG/动画 WebP，以及单条 Sprite 动画轨的 AnimationClip。支持暂停、逐帧、进度、倍速、点图放大和前后对照。
- **序列帧替换**：独立 Texture2D 连续帧按导入素材的帧时长采样到原帧数，并适配原尺寸；预览帧率不会修改游戏的播放规则。预览最多均匀抽样 120 帧，写入使用全部目标帧。动画图片容器只接受同格式文件，保留导入文件的逐帧时长。
- **AnimationClip**：支持的单条 Sprite 动画轨按原关键帧时序播放，不依靠文件名猜播放顺序。可以导入同源 `.animbin` 查看替换效果；选择图片只替换关联图集。原版、导入候选和保存后的作品集均可对照播放。
- **暂不支持**：完整骨骼动画、多轨、位置/旋转/缩放动画、粒子及依赖场景的复杂效果。无法解析的跨包 Sprite 引用也会提示原因。共享图集/网格 Sprite 可在支持的资源中预览，暂不直接替换单帧 Sprite；Live2D、Spine、视频等没有通用替换支持。

改字后是否出现、字体是否包含新增字形，以及动画在游戏中的最终效果，需要实机试用确认；服务端下发的文案不一定来自这些本地资源。工具内的播放预览不等于完整还原 Unity 场景，也不能把任意 GIF 转成骨骼动画。

## 从源码验证

安装 `requirements.txt` 中的依赖后，在仓库根目录运行：

```powershell
python selfcheck.py
python selfcheck_animation.py
python selfcheck_clip.py
python selfcheck_clip_preview.py
python -m unittest discover -s tests -p "test_*.py"
```

这些检查默认使用临时目录和模拟资源，不需要游戏。若要用本机资源做额外验证，可为以下脚本显式提供游戏的 `StreamingAssets\aa\StandaloneWindows64` 目录：

```powershell
python selfcheck_animation.py --game-bundles "你的游戏资源目录"
python selfcheck_clip.py --game-bundles "你的游戏资源目录"
python selfcheck_clip_preview.py --game-bundles "你的游戏资源目录"
python tests/preview_lab.py --assets "你的游戏资源目录"
```

真实资源检查使用指定版本的样例 bundle；如果游戏更新后找不到样例，这部分检查可能跳过或报缺少资源，不能视为已经验证当前版本。资源副本验证覆盖文字保存/恢复、帧替换/播放、失败不覆盖草稿、同包其他对象保留以及原件哈希不变；目前尚未在游戏进程里验证修改后的最终表现。

如果发现问题，记录游戏版本、资源名、对应 bundle、操作步骤和「运行日志」里的错误。分享日志前检查是否含有自己的路径等信息；无需上传整个游戏资源包。
