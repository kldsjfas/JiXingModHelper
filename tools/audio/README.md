# Wwise 音频解码组件

本项目在 Windows x64 上调用官方 **vgmstream CLI r2117**，把 Wwise WEM 解码为浏览器能试听的 PCM WAV。组件只解码，不把 WAV/MP3 编码成 WEM，也不会修改游戏文件。

## 获取及校验

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\audio\fetch_vgmstream.ps1
```

脚本从官方 GitHub Release 下载 4.47 MB 压缩包，核对固定 SHA256，再逐文件核对哈希；展开后约 10.7 MB。版本、来源、原包及各文件的 SHA256 均在 `source.json` 中。`vgmstream`、下载缓存和 ZIP 不纳入 Git。

默认程序位置为 `tools/audio/vgmstream/vgmstream-cli.exe`。官方 Windows CLI 对压缩包中的 DLL 有直接依赖，不能只复制 EXE；普通 Windows 系统库由系统提供，不额外安装驱动或服务。

## 调用

```powershell
# 查看 JSON 元数据，不输出音频。
& .\tools\audio\vgmstream\vgmstream-cli.exe -I .\sample.wem

# 完整播放一遍并输出 PCM WAV，避免游戏循环点造成重复。
& .\tools\audio\vgmstream\vgmstream-cli.exe -i -o .\sample.wav .\sample.wem
```

元数据原时长使用 `numberOfSamples / sampleRate`；默认的 `playSamples` 包含循环/淡出，不代表原片段长度。程序调用请使用参数列表，避免 shell 拼接，并设置超时及输出文件大小上限。

## 许可及再分发边界

- vgmstream 本体使用 ISC 许可，完整版权和许可声明保留在 `licenses/vgmstream.COPYING`，安装时亦保留官方包的 `COPYING`。
- 官方包还包含 Vorbis/Ogg、Opus、Speex、CELT、ATRAC9、mpg123、FFmpeg 等独立库；各自的上游许可文本在 `licenses/`，不会被本项目许可证替代。
- 上游 `doc/BUILD.md` 的固定版本副本 `licenses/UPSTREAM_BUILD.md` 列有依赖来源、版本、编译方法和许可说明。其中 **libg719_decode 被上游标记为许可未知**；FFmpeg/mpg123 等另有 LGPL 的通知、源码及可替换库等要求。
- 源码仓库和下载包只提供获取脚本、哈希清单与说明，不捆绑或转存上游工具的 EXE/DLL。下载版双击程序旁的 `获取音频解码组件.cmd`，直接从上述官方来源获取组件；下载后保留上游版权和许可声明。这些组件的许可证不会变成本项目的 MIT 许可证。

官方项目：https://github.com/vgmstream/vgmstream

固定发行版：https://github.com/vgmstream/vgmstream/releases/tag/r2117

本地验证：已从《星趴》BATTLE 音频包提取的 Wwise Vorbis 样本成功解码出 48 kHz 双声道 PCM WAV。测试输出只放在忽略的 `qa-evidence` 下，不随源代码分发游戏音频。
