from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from ..config import Config
from ..utils import quick_hash
from .base import ParsedDoc, ParseError
from .pdf import parse_pdf

log = logging.getLogger("kb.caj")

CAJ_TOOL_DIR = Path(os.environ.get("CAJ2PDF_DIR", ""))
CAJ_SCRIPT = CAJ_TOOL_DIR / "caj2pdf"


def convert_caj(path: Path, cfg: Config) -> Path:
    out = cfg.cache_dir / "caj" / f"{quick_hash(path)}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size > 1024:
        return out
    if not CAJ_SCRIPT.exists():
        raise ParseError("caj2pdf 未安装")

    from filelock import FileLock
    lock_path = cfg.cache_dir / "caj" / ".convert.lock"
    with FileLock(str(lock_path), timeout=600):
        if out.exists() and out.stat().st_size > 1024:
            return out
        cmd = [sys.executable, str(CAJ_SCRIPT), "convert", str(path), "-o", str(out)]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.run(
                cmd, cwd=str(CAJ_TOOL_DIR), capture_output=True, timeout=900,
                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise ParseError("CAJ 转换超时") from exc
        if proc.returncode != 0 or not out.exists() or out.stat().st_size <= 1024:
            err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="ignore")[-800:]
            raise ParseError(f"CAJ 转换失败: {err}")
    return out


def parse_caj(path: Path, cfg: Config) -> ParsedDoc:
    if not bool((cfg.raw.get("caj") or {}).get("auto_convert", True)):
        return ParsedDoc(title=path.stem[:200], page_count=0, blocks=[],
                         meta={"caj_pending": True, "caj_source": str(path)},
                         parser="caj-pending")
    try:
        pdf = convert_caj(path, cfg)
        doc = parse_pdf(pdf, cfg)
        doc.title = path.stem[:200]
        doc.meta["caj_source"] = str(path)
        doc.meta["converted_path"] = str(pdf)
        doc.parser = "caj"
        if not doc.blocks:
            doc.meta["ocr_required"] = True
        return doc
    except Exception as exc:
        log.warning("CAJ 暂不可读 %s: %s", path, exc)
        return ParsedDoc(
            title=path.stem[:200], page_count=0, blocks=[],
            meta={"caj_error": str(exc)[:1000], "caj_source": str(path)},
            parser="caj-unsupported",
        )
