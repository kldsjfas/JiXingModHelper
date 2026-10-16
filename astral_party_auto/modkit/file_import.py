"""Stage files explicitly dropped into the replacement editor."""
from __future__ import annotations

import base64
import binascii
from pathlib import Path
from tempfile import TemporaryDirectory

MAX_DROPPED_FILE_BYTES = 12 * 1024 * 1024
MAX_IMPORT_REQUEST_BYTES = 20 * 1024 * 1024
MAX_DROPPED_IMAGE_PIXELS = 64 * 1024 * 1024
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
_SUFFIXES = {
    "texture": IMAGE_SUFFIXES,
    "text": frozenset({".txt", ".json", ".xml", ".csv", ".bytes"}),
    "anim": IMAGE_SUFFIXES | {".animbin", ".bin"},
}


def stage_dropped_file(filename: str, content_base64: str, asset_type: str, cache_root: Path):
    """Use a generated basename; browser input never selects a local file path."""
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        raise ValueError("拖入文件的名称无效。")
    if any(character in filename for character in ("/", "\\", ":", "\0")):
        raise ValueError("请拖入文件本身，不能导入路径或文件夹。")
    suffix = Path(filename).suffix.lower()
    if suffix not in _SUFFIXES.get(asset_type, ()):
        if asset_type == "texture":
            raise ValueError("普通贴图请拖入 PNG、JPG、WebP 或 BMP；动图和视频请使用动画制作入口。")
        raise ValueError("当前资源不支持这个文件格式，请使用对应的制作入口。")
    if not isinstance(content_base64, str) or not content_base64:
        raise ValueError("拖入的文件为空或读取失败。")
    if len(content_base64) > 4 * ((MAX_DROPPED_FILE_BYTES + 2) // 3):
        raise ValueError("拖入文件超过 12 MiB，请使用选择文件按钮。")
    try:
        payload = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("拖入文件读取失败，请重新拖入。") from error
    if not payload or len(payload) > MAX_DROPPED_FILE_BYTES:
        raise ValueError("拖入文件为空或超过 12 MiB，请使用选择文件按钮。")
    cache_root.mkdir(parents=True, exist_ok=True)
    temporary = TemporaryDirectory(prefix="drop-", dir=cache_root)
    try:
        path = Path(temporary.name) / ("replacement" + suffix)
        path.write_bytes(payload)
        if suffix in IMAGE_SUFFIXES:
            from PIL import Image

            try:
                with Image.open(path) as image:
                    expected_format = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
                                       ".webp": "WEBP", ".bmp": "BMP"}[suffix]
                    if image.format != expected_format:
                        raise ValueError("图片内容与文件后缀不符，请用图片软件另存为 PNG 后再拖入。")
                    if image.width * image.height > MAX_DROPPED_IMAGE_PIXELS:
                        raise ValueError("图片尺寸过大，请先缩小图片后再拖入。")
            except Image.DecompressionBombError as error:
                raise ValueError("图片尺寸过大，请先缩小图片后再拖入。") from error
            except OSError as error:
                raise ValueError("图片无法读取，文件可能损坏。请重新导出 PNG/JPG 后再拖入。") from error
        return temporary, path
    except Exception:
        temporary.cleanup()
        raise
