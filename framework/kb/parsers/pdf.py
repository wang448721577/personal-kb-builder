from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..pymupdf_lock import PDF_LOCK
from ..textutil import clean_title, normalize_text
from .base import Block, ParsedDoc, ParseError


def parse_pdf(path: Path, cfg: Config) -> ParsedDoc:
    try:
        import pymupdf as fitz
    except Exception:
        import fitz  # type: ignore

    with PDF_LOCK:
        try:
            doc = fitz.open(str(path))
        except Exception as exc:
            raise ParseError(f"PDF 打开失败: {exc}") from exc

        try:
            if getattr(doc, "needs_pass", False):
                raise ParseError("PDF 已加密/需要密码，按策略跳过")
            meta_raw = doc.metadata or {}
            page_count = doc.page_count
            title = clean_title(meta_raw.get("title", ""), path.stem)
            author = (meta_raw.get("author") or "").strip()
            blocks: list[Block] = []
            needs_ocr: list[int] = []
            first_line = ""

            min_chars = max(0, cfg.ocr.min_chars_page)
            for i in range(page_count):
                try:
                    page = doc[i]
                    text = normalize_text(page.get_text("text"))
                except Exception:
                    continue
                if text and not first_line:
                    for line in text.splitlines():
                        line = line.strip()
                        if len(line) >= 4:
                            first_line = line[:120]
                            break
                if len(text) < min_chars:
                    try:
                        has_image = bool(page.get_images(full=False))
                    except Exception:
                        has_image = True
                    if cfg.ocr.enabled and (has_image or text.strip()):
                        needs_ocr.append(i + 1)
                if len(text) >= 12:
                    blocks.append(Block(text=text, kind="page", page_start=i + 1,
                                        page_end=i + 1, title_path=""))
            if not title or len(title) < 2:
                title = first_line if len(first_line) >= 4 else path.stem

            meta = {
                "pdf_pages": page_count,
                "pdf_title": meta_raw.get("title", ""),
                "pdf_subject": meta_raw.get("subject", ""),
                "pdf_keywords": meta_raw.get("keywords", ""),
                "pdf_created": meta_raw.get("creationDate", ""),
                "ocr_pages": needs_ocr[:5000],
            }
            return ParsedDoc(title=title, author=author, page_count=page_count,
                             blocks=blocks, meta=meta, needs_ocr=needs_ocr, parser="pdf")
        finally:
            try:
                doc.close()
            except Exception:
                pass
