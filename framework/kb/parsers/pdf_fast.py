from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..config import Config
from ..textutil import clean_title, normalize_text
from .base import Block, ParsedDoc, ParseError


def parse_pdf_fast(path: Path, cfg: Config) -> ParsedDoc:
    exe = shutil.which("pdftotext")
    if exe:
        try:
            proc = subprocess.run(
                [exe, "-enc", "UTF-8", str(path), "-"],
                capture_output=True, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                raw = proc.stdout.decode("utf-8", errors="ignore")
                pages = raw.split("\x0c")
                while pages and not pages[-1].strip():
                    pages.pop()
                if pages:
                    blocks: list[Block] = []
                    needs_ocr: list[int] = []
                    min_chars = max(0, cfg.ocr.min_chars_page)
                    for i, text in enumerate(pages, 1):
                        text = normalize_text(text)
                        if len(text) < min_chars:
                            needs_ocr.append(i)
                        if len(text) >= 12:
                            blocks.append(Block(text=text, kind="page", page_start=i,
                                                page_end=i, title_path=""))
                    meta = {"pdf_pages": len(pages), "fast": "pdftotext",
                            "ocr_pages": needs_ocr[:5000]}
                    return ParsedDoc(title=clean_title("", path.stem),
                                     page_count=len(pages), blocks=blocks, meta=meta,
                                     needs_ocr=needs_ocr, parser="pdf-fast")
        except Exception:
            pass
    from .pdf import parse_pdf
    return parse_pdf(path, cfg)
