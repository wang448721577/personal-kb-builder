from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..textutil import clean_title, normalize_text
from .base import Block, ParsedDoc


def parse_image(path: Path, cfg: Config) -> ParsedDoc:
    from PIL import Image, ExifTags

    meta: dict = {}
    width = height = 0
    exif_date = ""
    try:
        with Image.open(str(path)) as img:
            width, height = img.size
            meta["format"] = img.format or path.suffix.lstrip(".").upper()
            try:
                exif = img.getexif()
                if exif:
                    for tag, value in exif.items():
                        name = ExifTags.TAGS.get(tag, str(tag))
                        if name in ("DateTimeOriginal", "DateTime", "Make", "Model"):
                            meta[name] = str(value)
                    exif_date = str(meta.get("DateTimeOriginal") or meta.get("DateTime") or "")
            except Exception:
                pass
    except Exception as exc:
        meta["error"] = str(exc)

    name = path.stem
    info = f"图片：{name}"
    if width and height:
        info += f"\n尺寸：{width} x {height}"
    if exif_date:
        info += f"\n拍摄时间：{exif_date}"
    if cfg.ocr.images and cfg.ocr.enabled:
        meta["ocr_image"] = True
    meta["width"] = width
    meta["height"] = height

    blocks = [Block(text=info, kind="image", title_path="图片信息")]
    return ParsedDoc(title=clean_title(name, path.name), page_count=1, blocks=blocks,
                     meta=meta, parser="image")
