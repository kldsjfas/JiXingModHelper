# 表情转换与播放组件

动态表情转码、视频内容检测及最终 USM 播放预览使用 FFmpeg 和 FFprobe。发布包只提供获取脚本、固定哈希清单与许可文本，不捆绑或转存第三方 EXE/DLL，也不包含游戏资源。

下载版双击程序旁的 **获取表情转换组件.cmd**。源码用户在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\emote\fetch_ffmpeg.ps1
```

组件来自 FFmpeg 官网下载页列出的 Windows 构建提供方 **BtbN/FFmpeg-Builds**，固定为月末发行版 `autobuild-2026-09-30-13-08` 的 `n8.1.3-9-g29e619e767` Windows x64 LGPL shared 构建。FFmpeg 项目本身只提供源码，不能把这个 Windows 二进制称为 FFmpeg 官方编译包。下载约 80.7 MB；安装所需两个 EXE、七个配套 DLL 和许可约 167.3 MB。逐文件大小与 SHA256 在 [source.json](source.json) 中。

脚本校验原 ZIP 的固定大小和 SHA256，再逐文件校验并启动 FFmpeg、FFprobe 检查版本。安装位置为 `tools/emote/ffmpeg/bin/`，程序优先使用这里的组件；共享构建必须保留配套 DLL，不能只拷两个 EXE。脚本不修改系统 PATH，不安装驱动或服务。

下载和解压在本组件的 `cache/` 中进行，成功后清理可重建的下载和解压缓存。失败不会用未校验文件覆盖旧组件，可以修好网络后重试。替换不同版本时先完整准备新组件，再保留旧目录为 `ffmpeg.previous-*`；旧组件不自动删除。安装结果及清理摘要保存在本机 `setup-result.json`，不纳入源码。

## 来源与许可

- 固定发行版：[BtbN 月末构建](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-09-30-13-08)。不使用会变化的 `latest` 下载地址。
- FFmpeg 对应源码：[29e619e767cde9045a75c29bc9a8278ae7b3a98b](https://github.com/FFmpeg/FFmpeg/tree/29e619e767cde9045a75c29bc9a8278ae7b3a98b)，源码归档地址也保留在清单中。
- 构建配方与依赖来源：[BtbN 构建提交 6c9aec5](https://github.com/BtbN/FFmpeg-Builds/tree/6c9aec5fc9a72ec3abedd1fa84db141fa18cf52b)。依赖的版本、构建参数和上游获取方式以该配方为准。
- 该变体采用 **LGPL 3.0 或更新版本**，包含上游原 `LICENSE.txt`；完整 LGPL 文本与其引用的 GPL 3.0 文本保存在 [licenses/](licenses/)，安装时一起保留。第三方组件不会变成本项目的 MIT 许可。
- 本项目只提供从上游直接获取的方式。若自行再分发组件或修改它们，应保留版权和许可声明，并按适用许可证满足对应源码及可替换库等要求。

BtbN 的日常构建保留 14 天，月末构建保留两年；本清单使用月末固定版，上游可能在 2028 年 9 月之后下线它。届时需要更新并重新验证清单，不会静默改用未知版本或关闭哈希校验。
