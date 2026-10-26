from __future__ import annotations

import csv
import email
import io
import json
import re
import zipfile
from email import policy
from pathlib import Path

from ..config import Config
from ..textutil import clean_title, normalize_text
from ..utils import read_text_auto
from .base import Block, ParsedDoc, ParseError


def _strip_html(html: str) -> tuple[str, str]:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else ""
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = soup.get_text("\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return title, normalize_text(text)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
        return "", normalize_text(text)


def parse_html(path: Path, data: bytes | None = None) -> ParsedDoc:
    raw = data if data is not None else path.read_bytes()
    html = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "latin-1"):
        try:
            html = raw.decode(enc)
            break
        except Exception:
            continue
    html = html or raw.decode("utf-8", errors="ignore")
    title, text = _strip_html(html)
    if not text:
        raise ParseError("HTML 无正文")
    return ParsedDoc(title=clean_title(title, path.stem), page_count=0,
                     blocks=[Block(text=text, kind="text", title_path="")], parser="html")


def parse_csv(path: Path, cfg: Config) -> ParsedDoc:
    text = read_text_auto(path, max_bytes=50_000_000)
    rows: list[list[str]] = []
    try:
        reader = csv.reader(io.StringIO(text))
        for i, row in enumerate(reader):
            if i > cfg.max_rows_text:
                break
            rows.append([str(c).strip()[:300] for c in row])
    except Exception as exc:
        raise ParseError(f"CSV 解析失败: {exc}") from exc
    if not rows:
        raise ParseError("CSV 为空")
    cols = rows[0]
    body = rows[1:]
    lines = [f"CSV 文件：{path.name}", f"列名：{' | '.join(cols)}",
             f"样例行数：{len(body)}", "| " + " | ".join(cols) + " |",
             "|" + "---|" * len(cols)]
    for r in body:
        lines.append("| " + " | ".join((r + [""] * len(cols))[:len(cols)]) + " |")
    meta = {"columns": cols, "rows_sample": len(body), "csv": True}
    return ParsedDoc(title=clean_title(path.stem, path.name), page_count=1,
                     blocks=[Block(text="\n".join(lines), kind="table", title_path=path.stem)],
                     meta=meta, parser="csv")


def parse_json(path: Path) -> ParsedDoc:
    text = read_text_auto(path, max_bytes=5_000_000)
    title = path.stem
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            title = str(obj.get("title") or obj.get("name") or title)
        pretty = json.dumps(obj, ensure_ascii=False, indent=1)
    except Exception:
        pretty = text
    pretty = pretty[:200_000]
    if not pretty.strip():
        raise ParseError("JSON 为空")
    return ParsedDoc(title=clean_title(title, path.stem), page_count=0,
                     blocks=[Block(text=normalize_text(pretty), kind="text", title_path="")],
                     meta={"json": True}, parser="json")


def parse_eml(path: Path) -> ParsedDoc:
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = str(msg.get("subject") or path.stem)
    sender = str(msg.get("from") or "")
    date = str(msg.get("date") or "")
    body_parts = []
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("content-disposition") or "")
            if "attachment" in disp:
                attachments.append(part.get_filename() or "attachment")
                continue
            if ctype == "text/plain":
                try:
                    body_parts.append(part.get_content())
                except Exception:
                    pass
            elif ctype == "text/html":
                try:
                    _, t = _strip_html(part.get_content())
                    body_parts.append(t)
                except Exception:
                    pass
    else:
        try:
            body_parts.append(msg.get_content())
        except Exception:
            pass
    body = "\n\n".join(p for p in body_parts if p)
    text = f"主题：{subject}\n发件人：{sender}\n日期：{date}\n\n{body}"
    if attachments:
        text += "\n\n附件：" + "、".join(attachments)
    return ParsedDoc(title=clean_title(subject, path.stem), author=sender, page_count=0,
                     blocks=[Block(text=normalize_text(text), kind="text", title_path="")],
                     meta={"date": date, "attachments": attachments}, parser="eml")


def parse_ipynb(path: Path) -> ParsedDoc:
    data = json.loads(read_text_auto(path))
    cells = data.get("cells") or []
    parts = []
    for cell in cells:
        src = "".join(cell.get("source") or [])
        if not src.strip():
            continue
        ctype = cell.get("cell_type")
        if ctype == "markdown":
            parts.append(src)
        elif ctype == "code":
            parts.append("```python\n" + src + "\n```")
    text = "\n\n".join(parts)
    if not text.strip():
        raise ParseError("Notebook 为空")
    return ParsedDoc(title=clean_title(path.stem, path.name), page_count=len(cells),
                     blocks=[Block(text=normalize_text(text), kind="text", title_path="")],
                     meta={"cells": len(cells)}, parser="ipynb")


def parse_rtf(path: Path) -> ParsedDoc:
    raw = read_text_auto(path, max_bytes=5_000_000)
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", raw)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    text = normalize_text(text)
    if not text:
        raise ParseError("RTF 无正文")
    return ParsedDoc(title=clean_title(path.stem, path.name), page_count=0,
                     blocks=[Block(text=text, kind="text", title_path="")], parser="rtf")


def parse_zip_text(path: Path) -> ParsedDoc:
    parts = []
    title = path.stem
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".xml", ".xhtml", ".html", ".htm", ".txt"))]
            for name in names[:200]:
                data = zf.read(name)
                if name.lower().endswith((".html", ".htm", ".xhtml")):
                    _, text = _strip_html(data.decode("utf-8", errors="ignore"))
                else:
                    text = data.decode("utf-8", errors="ignore")
                    text = re.sub(r"<[^>]+>", " ", text)
                text = normalize_text(text)
                if text:
                    parts.append(text)
    except Exception as exc:
        raise ParseError(f"压缩文档解析失败: {exc}") from exc
    if not parts:
        raise ParseError("压缩文档无可读文本")
    return ParsedDoc(title=clean_title(title, path.name), page_count=len(parts),
                     blocks=[Block(text="\n\n".join(parts), kind="text", title_path="")],
                     meta={"parts": len(parts)}, parser="zip-text")


def parse_text(path: Path, cfg: Config) -> ParsedDoc:
    ext = path.suffix.lower()
    if ext in {".html", ".htm"}:
        return parse_html(path)
    if ext == ".csv":
        return parse_csv(path, cfg)
    if ext in {".json", ".jsonl"}:
        return parse_json(path)
    if ext == ".eml":
        return parse_eml(path)
    if ext == ".ipynb":
        return parse_ipynb(path)
    if ext == ".rtf":
        return parse_rtf(path)
    if ext in {".odt", ".ods", ".odp", ".epub", ".pages", ".numbers", ".key"}:
        return parse_zip_text(path)
    if ext == ".caj":
        from .caj import parse_caj
        return parse_caj(path, cfg)
    if ext == ".djvu":
        from .pdf import parse_pdf
        return parse_pdf(path, cfg)

    text = read_text_auto(path, max_bytes=20_000_000)
    text = normalize_text(text)
    if not text:
        raise ParseError("文本为空或无法解码")
    return ParsedDoc(title=clean_title(path.stem, path.name), page_count=0,
                     blocks=[Block(text=text, kind="text", title_path="")],
                     meta={}, parser=ext.lstrip(".") or "text")
