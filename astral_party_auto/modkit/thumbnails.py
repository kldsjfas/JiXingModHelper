"""Small, read-only resource previews kept in a bounded memory cache."""
from __future__ import annotations

import base64
import io
from collections import OrderedDict
from pathlib import Path

from PIL import Image

from .animation import MAX_FRAME_PIXELS, _load_bundle, _release_readers

THUMBNAIL_EDGE = 192
MAX_CACHED_THUMBNAILS = 192


class ResourceThumbnails:
    def __init__(self):
        self._cache: OrderedDict[tuple, str] = OrderedDict()

    @_release_readers
    def read(self, source: Path, names: list[str]) -> dict[str, dict]:
        source = Path(source)
        stamp = source.stat()
        identity = (str(source.resolve()), stamp.st_mtime_ns, stamp.st_size)
        results = {}
        pending = set(names)
        for name in names:
            key = (*identity, name)
            if key in self._cache:
                self._cache.move_to_end(key)
                results[name] = {"thumbnail_data": self._cache[key]}
                pending.discard(name)
        if not pending:
            return results

        environment = _load_bundle(source)
        for resource in environment.objects:
            if resource.type.name not in ("Texture2D", "Sprite"):
                continue
            try:
                name = resource.peek_name()
            except Exception:
                continue
            if name not in pending:
                continue
            pending.remove(name)
            try:
                data = resource.read()
                width = int(getattr(data, "m_Width", 0) or 0)
                height = int(getattr(data, "m_Height", 0) or 0)
                if width * height > MAX_FRAME_PIXELS:
                    raise ValueError("图片过大，请选中后单独查看。")
                if resource.type.name == "Sprite":
                    from .clip_preview import _render_data
                    if data.m_Rect.width * data.m_Rect.height > MAX_FRAME_PIXELS:
                        raise ValueError("图片过大，请选中后单独查看。")
                    render_data = _render_data(data)
                    for pointer in (render_data.texture, getattr(render_data, "alphaTexture", None)):
                        if pointer is not None and pointer:
                            atlas = pointer.read()
                            if atlas.m_Width * atlas.m_Height > MAX_FRAME_PIXELS:
                                raise ValueError("图片使用的大图集过大，请选中后单独查看。")
                pixels = data.image
                if pixels is None:
                    raise ValueError("资源没有可解码的图片。")
                if pixels.width * pixels.height > MAX_FRAME_PIXELS:
                    raise ValueError("图片过大，请选中后单独查看。")
                thumbnail = pixels.convert("RGBA")
                thumbnail.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                thumbnail.save(buffer, format="PNG")
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                results[name] = {"thumbnail_data": "data:image/png;base64," + encoded}
            except Exception as exc:
                results[name] = {"error": str(exc) or "图片解码失败。"}

        after = source.stat()
        if (after.st_mtime_ns, after.st_size) != (stamp.st_mtime_ns, stamp.st_size):
            raise ValueError("资源包在读取时发生变化，请重新加载缩略图。")
        for name in pending:
            results[name] = {"error": "图片不在当前资源包中，请刷新索引。"}
        for name, result in results.items():
            if result.get("thumbnail_data"):
                self._cache[(*identity, name)] = result["thumbnail_data"]
                self._cache.move_to_end((*identity, name))
        while len(self._cache) > MAX_CACHED_THUMBNAILS:
            self._cache.popitem(last=False)
        return results
