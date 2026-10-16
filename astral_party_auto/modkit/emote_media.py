"""动态图转表情视频，并从最终 USM 的颜色和透明度双流生成真实预览。"""
from __future__ import annotations

import base64
import bisect
import gc
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from ..core.config import APP_ROOT, RESOURCE_ROOT
from . import cri_emote

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FRAMES = 512
MAX_SOURCE_FRAME_PIXELS = 16_777_216
MAX_SOURCE_PIXELS = 64_000_000
MAX_OUTPUT_PIXELS = 32_000_000
MAX_SOURCE_DURATION_MS = 30_000
MAX_VIDEO_FRAME_PIXELS = 4_194_304
MAX_PREVIEW_PIXELS = 20_000_000
MAX_PREVIEW_EDGE = 512
MAX_PREVIEW_FRAMES = 120
FFMPEG_TIMEOUT = 60
PREVIEW_NOTICE = "预览由 USM 颜色流和透明度流实际解码合成；游戏内效果仍需测试。"


def find_ffmpeg(explicit_path: str | Path | None = None) -> Path:
    """优先用已有可执行文件；兼容打包程序，不下载或改动系统环境。"""
    candidates = []
    if explicit_path is not None:
        candidates.append(Path(explicit_path).expanduser())
    else:
        executable_dir = Path(sys.executable).resolve().parent
        package_dir = Path(getattr(sys, "_MEIPASS", executable_dir))
        for folder in (APP_ROOT, RESOURCE_ROOT, executable_dir, package_dir):
            candidates.append(folder / "tools" / "emote" / "ffmpeg" / "bin" / "ffmpeg.exe")
        for folder in (package_dir, executable_dir):
            candidates.extend((folder / "ffmpeg.exe", folder / "ffmpeg" / "bin" / "ffmpeg.exe"))
        available = shutil.which("ffmpeg")
        if available:
            candidates.append(Path(available))
        candidates.append(Path.home() / ".local" / "bin" / "ffmpeg.exe")
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        try:
            result = subprocess.run(
                [str(candidate), "-version"], capture_output=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.startswith(b"ffmpeg version"):
            return candidate.resolve()
    raise ValueError("找不到可用的表情转换组件（FFmpeg）；请运行程序旁的“获取表情转换组件.cmd”，完成后重新预览或导入。源码运行可执行 tools/emote/fetch_ffmpeg.ps1。")


def find_ffprobe(ffmpeg_path: str | Path) -> Path:
    ffmpeg = Path(ffmpeg_path)
    candidates = [ffmpeg.with_name("ffprobe.exe"), ffmpeg.with_name("ffprobe")]
    available = shutil.which("ffprobe")
    if available:
        candidates.append(Path(available))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            result = subprocess.run([str(candidate), "-version"], capture_output=True, timeout=5,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.startswith(b"ffprobe version"):
            return candidate.resolve()
    raise ValueError("表情转换组件缺少可用的 FFprobe；请运行程序旁的“获取表情转换组件.cmd”重新校验组件，完成后重试。")


def detect_emote_media(path: str | Path) -> str:
    """识别文件内容；改错扩展名不影响 GIF/APNG/WebP/USM 的导入。"""
    source = Path(path)
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_SOURCE_BYTES:
        raise ValueError("请选择不超过 64 MB 的动图、视频或 USM 文件。")
    with source.open("rb") as stream:
        signature = stream.read(4)
    if signature == b"CRID":
        return "usm"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if image.format in ("GIF", "PNG", "WEBP"):
                    return {"GIF": "gif", "PNG": "apng", "WEBP": "webp"}[image.format]
                raise ValueError("请选择 GIF、APNG、动画 WebP 或常见视频，静态图片不能替代动态表情。")
    except UnidentifiedImageError:
        return "video"
    except (Image.DecompressionBombWarning, Image.DecompressionBombError, OSError, EOFError) as exc:
        raise ValueError("素材图片过大或文件损坏，请重新导出。") from exc


def _work_parent(work_dir: str | Path | None) -> str | None:
    if work_dir is None:
        return None
    folder = Path(work_dir)
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder)


def _reference_data(bundle_path: str | Path, asset_name: str) -> tuple[dict, bytes]:
    try:
        _, tree, _, raw = cri_emote._embedded_object(cri_emote._load_bundle(bundle_path), asset_name)
        return tree, raw
    finally:
        # UnityPy object cycles must not keep a Windows draft bundle open.
        gc.collect()


def _duration(value) -> tuple[float, bool]:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return 100.0, True
    if not math.isfinite(duration) or duration <= 0:
        return 100.0, True
    return duration, False


def _source_timing(image: Image.Image) -> tuple[list[float], int, int]:
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_SOURCE_FRAME_PIXELS:
        raise ValueError("动图单帧超过 1600 万像素，请先缩小素材。")
    first = int(bool(image.info.get("default_image")))
    count = getattr(image, "n_frames", 1) - first
    if not 2 <= count <= MAX_SOURCE_FRAMES:
        raise ValueError("请选择包含 2 到 512 帧的 GIF、APNG 或动画 WebP。")
    if width * height * count > MAX_SOURCE_PIXELS:
        raise ValueError("动图解压总像素超过 6400 万，请先缩小素材或减少帧数。")
    durations, fallback_count = [], 0
    for index in range(first, first + count):
        image.seek(index)
        # WebP supplies frame duration after loading; GIF/APNG disposal is also
        # resolved by Pillow before later RGBA conversion.
        image.load()
        duration, fallback = _duration(image.info.get("duration"))
        durations.append(duration)
        fallback_count += int(fallback)
        if sum(durations) > MAX_SOURCE_DURATION_MS:
            raise ValueError("导入动图目前限制 30 秒，请先剪短素材。")
    return durations, first, fallback_count


def _fit_frame(frame: Image.Image, width: int, height: int) -> Image.Image:
    ratio = min(width / frame.width, height / frame.height)
    size = (max(1, min(width, round(frame.width * ratio))),
            max(1, min(height, round(frame.height * ratio))))
    resized = frame.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.paste(resized, ((width - size[0]) // 2, (height - size[1]) // 2))
    return canvas


def adapt_animation_source(path: str | Path, reference_info: dict) -> tuple[list[Image.Image], dict]:
    """依源帧时长选帧，以参考帧率重采样，等比缩放并保留透明留边。"""
    source = Path(path)
    raw = cri_emote._read_file(source, MAX_SOURCE_BYTES, "动图")
    width, height, fps = reference_info["width"], reference_info["height"], reference_info["fps"]
    if (not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0
            or not math.isfinite(fps) or not 1 <= fps <= 60 or width * height > 4_194_304):
        raise ValueError("参考视频尺寸或帧率无效，请重新选择原生动态表情。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ("GIF", "PNG", "WEBP"):
                    raise ValueError("请选择 GIF、APNG 或动画 WebP；普通视频暂不支持导入。")
                durations, first, fallback_count = _source_timing(image)
                source_duration = sum(durations)
                output_count = max(1, math.floor(source_duration * fps / 1000 + 0.5))
                if output_count > cri_emote.MAX_FRAMES or width * height * output_count > MAX_OUTPUT_PIXELS:
                    raise ValueError("转码后的帧数据超过 3200 万像素，请缩短动图或选择尺寸更小的原生表情。")
                ends, elapsed = [], 0.0
                for duration in durations:
                    elapsed += duration
                    ends.append(elapsed)
                indices = [min(len(durations) - 1, bisect.bisect_right(ends, index * 1000 / fps))
                           for index in range(output_count)]
                frames, previous, fitted = [], -1, None
                image.seek(first)
                for index in indices:
                    if index != previous:
                        image.seek(first + index)
                        fitted = _fit_frame(image.convert("RGBA"), width, height)
                        previous = index
                    frames.append(fitted.copy())
                output_duration = output_count * 1000 / fps
                notice = "等比适配原表情尺寸，空白部分透明；按原表情帧率重采样。"
                if abs(output_duration - source_duration) >= 0.5:
                    notice += " 时长已对齐到整帧，误差不超过一帧。"
                if fallback_count:
                    notice += f" {fallback_count} 帧没有有效时长，按每帧 100 毫秒处理。"
                return frames, {"format": {"GIF": "gif", "PNG": "apng", "WEBP": "webp"}[image.format],
                                "source_name": source.name, "source_frames": len(durations),
                                "source_width": image.width, "source_height": image.height,
                                "source_duration_ms": source_duration, "duration_ms": output_duration,
                                "width": width, "height": height, "fps": fps, "frames": output_count,
                                "duration_error_ms": output_duration - source_duration, "notice": notice}
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
            EOFError, OSError) as exc:
        raise ValueError("动图无法完整解码，或图片过大；请重新导出 GIF、APNG 或动画 WebP。") from exc


def _logged_process(command: list[str], output: Path | None, error: Path, label: str) -> None:
    try:
        with error.open("wb") as error_stream:
            if output is None:
                result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=error_stream,
                                        timeout=FFMPEG_TIMEOUT, check=False,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                with output.open("wb") as output_stream:
                    result = subprocess.run(command, stdout=output_stream, stderr=error_stream,
                                            timeout=FFMPEG_TIMEOUT, check=False,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ValueError(label + "超时，请缩小或剪短素材。") from exc
    except OSError as exc:
        raise ValueError(label + "无法启动，请检查 FFmpeg / FFprobe 是否可用。") from exc
    if result.returncode or error.stat().st_size:
        with error.open("rb") as stream:
            stream.seek(max(0, error.stat().st_size - 1600))
            detail = stream.read(1600).decode("utf-8", errors="replace").strip()
        raise ValueError(label + "失败：" + (detail or "外部程序返回错误。"))


def _probe_result(ffprobe: Path, source: Path, scratch: Path, *, count_frames=False) -> dict:
    output, error = scratch / "probe.json", scratch / "probe.stderr"
    command = [str(ffprobe), "-v", "error", "-threads", "1", "-max_alloc", "67108864",
               "-select_streams", "v:0", "-show_streams", "-show_format", "-of", "json"]
    if count_frames:
        command.extend(("-count_frames", "-err_detect", "explode"))
    command.append(str(source))
    _logged_process(command, output, error, "视频内容检测")
    if output.stat().st_size > 1_000_000:
        raise ValueError("视频元信息超过检测范围，请重新导出素材。")
    try:
        return json.loads(output.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("无法读取视频元信息，请重新导出素材。") from exc


def _video_duration(stream: dict, container: dict) -> float:
    # Container duration may belong to a much longer audio track, or include a
    # video start offset. Only trust metadata attached to the selected video.
    tags = {str(key).lower(): value for key, value in stream.get("tags", {}).items()}
    candidates = [stream.get("duration"), tags.get("duration")]
    for value in candidates:
        try:
            if isinstance(value, str) and ":" in value:
                hours, minutes, seconds = value.split(":")
                duration = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
            else:
                duration = float(value)
            if math.isfinite(duration) and duration > 0:
                return duration * 1000
        except (TypeError, ValueError):
            continue
    raise ValueError("无法核实视频时长，请先重新导出 MP4、WebM、MOV、MKV 或 AVI。")


def _packet_video_duration(ffprobe: Path, source: Path, scratch: Path, stream: dict) -> float:
    """缺失视频时长时，读取该视频轨的 PTS；不拿音轨或容器总长补帧。"""
    output, error = scratch / "packets.json", scratch / "packets.stderr"
    command = [str(ffprobe), "-v", "error", "-threads", "1", "-max_alloc", "67108864",
               "-select_streams", "v:0", "-show_packets", "-show_entries", "packet=pts_time,duration_time",
               "-of", "json", str(source)]
    _logged_process(command, output, error, "视频时序检测")
    if output.stat().st_size > 1_000_000:
        raise ValueError("视频时序条目超过检测范围，请先剪短素材。")
    try:
        packets = json.loads(output.read_text(encoding="utf-8"))["packets"]
        if not 1 <= len(packets) <= MAX_SOURCE_FRAMES:
            raise ValueError("packet count")
        timeline = [(float(packet["pts_time"]), float(packet.get("duration_time", 0))) for packet in packets]
        if any(not math.isfinite(pts) or not math.isfinite(duration) or duration < 0 for pts, duration in timeline):
            raise ValueError("invalid timestamps")
        timeline.sort()
        first, last = timeline[0][0], timeline[-1][0]
        tail = timeline[-1][1]
        if tail <= 0:
            try:
                rate = str(stream.get("avg_frame_rate", "0/1")).split("/")
                fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 else float(rate[0])
            except (ValueError, ZeroDivisionError):
                fps = 0
            tail = 1 / fps if math.isfinite(fps) and fps > 0 else 0
        if tail <= 0 and len(timeline) > 1:
            tail = last - timeline[-2][0]
        duration = (last - first + tail) * 1000
        if not math.isfinite(duration) or duration <= 0 or tail <= 0:
            raise ValueError("missing terminal frame duration")
        return duration
    except (KeyError, TypeError, ValueError, ZeroDivisionError, UnicodeError) as exc:
        raise ValueError("无法核实视频轨的真实时长或超过 512 帧，请剪短或重新导出素材。") from exc


def _video_display_aspect(stream: dict, width: int, height: int) -> float:
    """输出 RGBA 没有 SAR 字段，必须把像素比例折进实际缩放尺寸。"""
    def ratio(value):
        if value in (None, "", "N/A"):
            return None
        parts = str(value).split(":")
        if len(parts) != 2:
            raise ValueError("视频宽高比元信息无效，请重新导出素材。")
        try:
            numerator, denominator = (int(part) for part in parts)
        except ValueError as exc:
            raise ValueError("视频宽高比元信息无效，请重新导出素材。") from exc
        if not 1 <= numerator <= 65535 or not 1 <= denominator <= 65535:
            raise ValueError("视频宽高比元信息超过范围，请重新导出素材。")
        return numerator / denominator

    pixel_aspect = ratio(stream.get("sample_aspect_ratio"))
    declared_display = ratio(stream.get("display_aspect_ratio"))
    if pixel_aspect is None:
        display = declared_display or width / height
        pixel_aspect = display * height / width
    else:
        display = width * pixel_aspect / height
    if not math.isfinite(pixel_aspect) or not 0.01 <= pixel_aspect <= 100:
        raise ValueError("视频像素宽高比超过范围，请重新导出素材。")
    # FFmpeg applies quarter-turn display matrices before filtering.
    rotation = next((row.get("rotation") for row in stream.get("side_data_list", [])
                     if isinstance(row, dict) and "rotation" in row), stream.get("tags", {}).get("rotate", 0))
    try:
        rotation = float(rotation)
    except (TypeError, ValueError) as exc:
        raise ValueError("视频旋转元信息无效，请重新导出素材。") from exc
    if not math.isfinite(rotation):
        raise ValueError("视频旋转元信息无效，请重新导出素材。")
    if abs(rotation % 180 - 90) < 0.01:
        display = 1 / display
    return display


def adapt_video_source(path: str | Path, reference_info: dict, *, work_dir: str | Path | None = None,
                       ffmpeg_path: str | Path | None = None) -> tuple[list[Image.Image], dict]:
    """有界检测视频内容，用真实时间戳重采样，忽略音轨并保留可解码 alpha。"""
    source = Path(path)
    if detect_emote_media(source) != "video":
        raise ValueError("所选素材是动图或 USM，请按实际内容导入。")
    ffmpeg = find_ffmpeg(ffmpeg_path)
    ffprobe = find_ffprobe(ffmpeg)
    width, height, fps = reference_info["width"], reference_info["height"], reference_info["fps"]
    if (not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0
            or not math.isfinite(fps) or not 1 <= fps <= 60 or width * height > 4_194_304):
        raise ValueError("参考视频尺寸或帧率无效，请重新选择原生动态表情。")
    with tempfile.TemporaryDirectory(prefix="emote-video-", dir=_work_parent(work_dir)) as folder:
        scratch = Path(folder)
        probe = _probe_result(ffprobe, source, scratch)
        streams, container = probe.get("streams", []), probe.get("format", {})
        if not streams or not isinstance(streams[0], dict) or not isinstance(container, dict):
            raise ValueError("文件没有可解码的视频画面，请选择 GIF、APNG、动画 WebP 或常见视频。")
        formats = str(container.get("format_name", "")).split(",")
        if not set(formats).intersection({"mov", "mp4", "matroska", "webm", "avi"}):
            raise ValueError("目前支持 MP4、WebM、MOV、MKV、AVI 视频，请先转换素材格式。")
        stream = streams[0]
        try:
            source_width, source_height = int(stream["width"]), int(stream["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("无法核实视频尺寸，请重新导出素材。") from exc
        if source_width <= 0 or source_height <= 0 or source_width * source_height > MAX_VIDEO_FRAME_PIXELS:
            raise ValueError("视频单帧超过 400 万像素，请先缩小素材。")
        display_aspect = _video_display_aspect(stream, source_width, source_height)
        try:
            duration = _video_duration(stream, container)
            timing_source = "video_track_metadata"
        except ValueError:
            duration = _packet_video_duration(ffprobe, source, scratch, stream)
            timing_source = "video_packet_pts"
        if duration > MAX_SOURCE_DURATION_MS:
            raise ValueError("导入视频目前限制 30 秒，请先剪短素材。")
        output_count = max(1, math.floor(duration * fps / 1000 + 0.5))
        if output_count > cri_emote.MAX_FRAMES or width * height * output_count > MAX_OUTPUT_PIXELS:
            raise ValueError("转码后的帧数据超过 3200 万像素，请缩短视频或选择尺寸更小的原生表情。")
        counted = _probe_result(ffprobe, source, scratch, count_frames=True)
        try:
            source_count = int(counted["streams"][0]["nb_read_frames"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("无法核实视频完整帧数，请重新导出素材。") from exc
        if not 1 <= source_count <= MAX_SOURCE_FRAMES:
            raise ValueError("视频超过 512 帧，请降低帧率或剪短素材。")
        if source_width * source_height * source_count > MAX_SOURCE_PIXELS:
            raise ValueError("视频解码总像素超过 6400 万，请缩小素材或减少帧数。")
        pixel_format = str(stream.get("pix_fmt", "")).lower()
        tags = {str(key).lower(): value for key, value in stream.get("tags", {}).items()}
        alpha_tag = str(tags.get("alpha_mode", "0")) == "1"
        has_alpha = alpha_tag or pixel_format.startswith(("yuva", "gbrap")) or pixel_format in {
            "rgba", "argb", "bgra", "abgr", "rgba64be", "rgba64le", "bgra64be", "bgra64le", "ya8", "ya16be", "ya16le",
        }
        decoder = {"vp8": "libvpx", "vp9": "libvpx-vp9"}.get(stream.get("codec_name")) if alpha_tag else None
        if display_aspect >= width / height:
            fitted_width, fitted_height = width, max(1, min(height, round(width / display_aspect)))
        else:
            fitted_width, fitted_height = max(1, min(width, round(height * display_aspect))), height
        filters = (f"setpts=PTS-STARTPTS,format=rgba,"
                   f"scale={fitted_width}:{fitted_height}:flags=lanczos,setsar=1,"
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
                   f"tpad=stop_mode=clone:stop_duration={1 / fps:.12g},"
                   f"fps=fps={fps:.12g}:start_time=0:round=near")
        output, error = scratch / "adapted.rgba", scratch / "decode.stderr"
        command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror", "-y",
                   "-threads", "1", "-max_alloc", "67108864"]
        if decoder:
            command.extend(("-c:v", decoder))
        command.extend(("-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn", "-filter_threads", "1",
                        "-vf", filters, "-frames:v", str(output_count), "-fps_mode", "passthrough",
                        "-pix_fmt", "rgba", "-f", "rawvideo", str(output)))
        _logged_process(command, None, error, "视频转换")
        frame_bytes = width * height * 4
        if not output.is_file() or output.stat().st_size != frame_bytes * output_count:
            raise ValueError("视频解码帧数与时长不一致，未生成表情；请重新导出素材。")
        frames = []
        with output.open("rb") as decoded:
            for _ in range(output_count):
                frames.append(Image.frombytes("RGBA", (width, height), decoded.read(frame_bytes)))
        notice = "视频按实际时间戳重采样，等比适配原表情尺寸，忽略音轨。"
        notice += " 已保留视频透明通道。" if has_alpha else " 视频没有透明通道，画面按不透明处理，尺寸留边透明。"
        output_duration = output_count * 1000 / fps
        if abs(output_duration - duration) >= 0.5:
            notice += " 时长已对齐到整帧，误差不超过一帧。"
        return frames, {"format": "video", "container": container.get("format_name"),
                        "source_codec": stream.get("codec_name"), "source_name": source.name,
                        "source_frames": source_count, "source_width": source_width, "source_height": source_height,
                        "source_display_aspect": display_aspect, "fitted_width": fitted_width, "fitted_height": fitted_height,
                        "source_duration_ms": duration, "duration_ms": output_duration,
                        "duration_error_ms": output_duration - duration, "width": width, "height": height,
                        "fps": fps, "frames": output_count, "has_alpha": has_alpha, "decoder": decoder,
                        "timing_source": timing_source,
                        "notice": notice}


def _sample_preview(total: int, fps: float, limit: int) -> tuple[list[int], list[float]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 240:
        raise ValueError("预览帧数需要在 1 到 240 之间。")
    count = min(total, limit)
    indices = ([0] if count == 1 else
               [index * (total - 1) // (count - 1) for index in range(count)])
    boundaries = indices + [total]
    durations = [(boundaries[index + 1] - boundaries[index]) * 1000 / fps for index in range(count)]
    return indices, durations


def _decode_stream(ffmpeg: Path, source: Path, output: Path, indices: list[int],
                   width: int, height: int, pixel_format: str, *,
                   crop_size: tuple[int, int] | None = None) -> bytes:
    # FFmpeg limits expression recursion. A flat sum of 120 eq() terms fails
    # even for a tiny video; bounded groups keep the parser stack shallow.
    groups = ["(" + "+".join(f"eq(n\\,{index})" for index in indices[start:start + 16]) + ")"
              for start in range(0, len(indices), 16)]
    selection = "+".join(groups)
    crop = f"crop={crop_size[0]}:{crop_size[1]}:0:0:exact=1," if crop_size else ""
    filters = f"select={selection},{crop}scale={width}:{height}:flags=bilinear"
    error_path = output.with_suffix(".stderr")
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror", "-y",
               "-threads", "1", "-f", "mpegvideo", "-i", str(source), "-an", "-sn", "-dn",
               "-filter_threads", "1", "-vf", filters, "-fps_mode", "passthrough",
               "-frames:v", str(len(indices)), "-pix_fmt", pixel_format, "-f", "rawvideo", str(output)]
    try:
        with error_path.open("wb") as error_stream:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=error_stream,
                                    timeout=FFMPEG_TIMEOUT, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ValueError("表情视频解码超时，请尝试较短或较小的素材。") from exc
    except OSError as exc:
        raise ValueError("无法启动 FFmpeg，请检查它是否可用。") from exc
    if result.returncode:
        with error_path.open("rb") as stream:
            stream.seek(max(0, error_path.stat().st_size - 1600))
            detail = stream.read(1600).decode("utf-8", errors="replace").strip()
        raise ValueError("USM 视频流无法完整解码：" + (detail or "FFmpeg 返回错误。"))
    expected = width * height * len(indices) * (3 if pixel_format == "rgb24" else 1)
    if not output.is_file() or output.stat().st_size != expected:
        raise ValueError("USM 解码帧数与视频头不一致，无法生成可靠预览。")
    return output.read_bytes()


def read_usm_preview(raw: bytes, *, work_dir: str | Path | None = None,
                     ffmpeg_path: str | Path | None = None,
                     max_preview_frames: int = MAX_PREVIEW_FRAMES,
                     allow_display_crop: bool = False) -> dict:
    info = cri_emote.inspect_usm_bytes(raw, allow_display_crop=allow_display_crop)
    ffmpeg = find_ffmpeg(ffmpeg_path)
    indices, durations = _sample_preview(info["frames"], info["fps"], max_preview_frames)
    edge = min(MAX_PREVIEW_EDGE, int(math.sqrt(MAX_PREVIEW_PIXELS / len(indices))))
    display_width, display_height = info["display_width"], info["display_height"]
    scale = min(1, edge / max(display_width, display_height))
    width, height = max(1, round(display_width * scale)), max(1, round(display_height * scale))
    decode_options = {}
    if (display_width, display_height) != (info["width"], info["height"]):
        decode_options["crop_size"] = (display_width, display_height)
    with tempfile.TemporaryDirectory(prefix="emote-preview-", dir=_work_parent(work_dir)) as folder:
        scratch = Path(folder)
        streams = {b"@SFV": scratch / "color.m1v", b"@ALP": scratch / "alpha.m1v"}
        handles = {tag: path.open("wb") for tag, path in streams.items()}
        try:
            for chunk in cri_emote._chunks(raw):
                if chunk.kind == 0 and chunk.tag in handles:
                    handles[chunk.tag].write(chunk.payload)
        finally:
            for handle in handles.values():
                handle.close()
        colors = _decode_stream(ffmpeg, streams[b"@SFV"], scratch / "color.raw", indices, width, height, "rgb24", **decode_options)
        alphas = _decode_stream(ffmpeg, streams[b"@ALP"], scratch / "alpha.raw", indices, width, height, "gray", **decode_options)
        frame_pixels = width * height
        frames = []
        for index in range(len(indices)):
            color = Image.frombytes("RGB", (width, height), colors[index * frame_pixels * 3:(index + 1) * frame_pixels * 3])
            alpha = Image.frombytes("L", (width, height), alphas[index * frame_pixels:(index + 1) * frame_pixels])
            color.putalpha(alpha)
            buffer = io.BytesIO()
            color.save(buffer, format="PNG")
            frames.append("data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
    notice = PREVIEW_NOTICE
    if len(indices) < info["frames"]:
        notice += f" 预览按时间均匀抽样为 {len(indices)} 帧，保留实际总时长。"
    return {"animation_kind": "cri_emote", "frames": frames,
            "names": [f"frame_{index + 1}" for index in indices], "durations": durations,
            "total": info["frames"], "total_duration_ms": info["frames"] * 1000 / info["fps"],
            "fps": info["fps"], "width": display_width, "height": display_height,
            "encoded_width": info["width"], "encoded_height": info["height"],
            "preview_width": width, "preview_height": height, "truncated": len(indices) < info["frames"],
            "has_alpha": True, "preview_available": True, "notice": notice,
            "sha256": info["sha256"]}


def preview_emote_media(bundle_path: str | Path, asset_name: str, **options) -> dict:
    _, raw = _reference_data(bundle_path, asset_name)
    return read_usm_preview(raw, **options)


def prepare_emote_media(source_path: str | Path, reference_bundle: str | Path, asset_name: str,
                        output_path: str | Path, *, work_dir: str | Path | None = None,
                        ffmpeg_path: str | Path | None = None) -> dict:
    """先验证和回解最终视频，成功后才写输出；失败不覆盖输入或旧输出。"""
    source, output = Path(source_path), Path(output_path)
    if output.suffix.lower() != ".usm":
        raise ValueError("转换输出需要使用 .usm 扩展名。")
    if output.resolve() in (source.resolve(), Path(reference_bundle).resolve()):
        raise ValueError("转换输出不能覆盖导入素材或参考资源包。")
    reference_tree, reference_raw = _reference_data(reference_bundle, asset_name)
    reference_info = cri_emote.inspect_usm_bytes(reference_raw)
    ffmpeg = find_ffmpeg(ffmpeg_path)
    source_kind = detect_emote_media(source)
    if source_kind == "usm":
        final_raw = cri_emote._read_file(source, cri_emote.MAX_USM_BYTES, "USM 视频")
        source_info = {"format": "usm", "source_name": source.name, "notice": "导入原生 USM 视频。"}
    else:
        from .usm_encoder import encode_emote_frames

        if source_kind == "video":
            frames, source_info = adapt_video_source(source, reference_info, work_dir=work_dir, ffmpeg_path=ffmpeg)
        else:
            frames, source_info = adapt_animation_source(source, reference_info)
        try:
            final_raw = encode_emote_frames(frames, reference_raw, ffmpeg, work_dir=work_dir)
        finally:
            frames.clear()
    final_info = cri_emote.inspect_usm_bytes(final_raw)
    cri_emote._compatible(final_info, reference_info)
    cri_emote._check_movie_cache(reference_tree, final_raw, reference_raw)
    animation = read_usm_preview(final_raw, work_dir=work_dir, ffmpeg_path=ffmpeg)
    cri_emote._write_atomic(output, final_raw)
    return {"path": str(output), "source_name": source.name, "size": len(final_raw),
            "video_info": {**final_info, "preview_available": True}, "animation": animation,
            "source_info": source_info, "notice": source_info["notice"] + " " + animation["notice"]}
