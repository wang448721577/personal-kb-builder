from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config


class ParseError(Exception):
    pass


@dataclass
class Block:
    text: str
    kind: str = "text"
    title_path: str = ""
    page_start: int | None = None
    page_end: int | None = None
    sheet: str | None = None
    slide: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass
class ParsedDoc:
    title: str
    author: str = ""
    page_count: int = 0
    blocks: list[Block] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    needs_ocr: list[int] = field(default_factory=list)
    parser: str = ""
    error: str = ""


def parse_file(path: Path, cfg: Config) -> ParsedDoc:
    from .office import parse_office
    from .pdf import parse_pdf
    from .image import parse_image
    from .media import parse_media
    from .text import parse_text

    ext = path.suffix.lower()
    if ext in {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}:
        return parse_office(path, cfg)
    if ext == ".pdf":
        return parse_pdf(path, cfg)
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}:
        return parse_image(path, cfg)
    if ext in {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".m4v", ".webm", ".mpg", ".mpeg",
               ".mp3", ".wav", ".m4a", ".aac", ".flac", ".wma", ".ogg", ".opus"}:
        return parse_media(path, cfg)
    return parse_text(path, cfg)
