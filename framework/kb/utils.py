from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

try:
    import blake3
    _HAS_BLAKE3 = True
except Exception:
    _HAS_BLAKE3 = False


def new_hash() -> "hashlib._Hash":
    if _HAS_BLAKE3:
        return blake3.blake3()
    return hashlib.sha256()


def hash_bytes(data: bytes) -> str:
    h = new_hash()
    h.update(data)
    return h.hexdigest()


def quick_hash(path: Path, block: int = 1024 * 1024) -> str:
    size = path.stat().st_size
    h = new_hash()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(block))
        if size > block * 2:
            f.seek(-block, os.SEEK_END)
            h.update(f.read(block))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    h = new_hash()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def text_hash(text: str) -> str:
    return hash_bytes(text.encode("utf-8", errors="ignore"))


def read_text_auto(path: Path, max_bytes: int = 20_000_000) -> str:
    data = path.read_bytes()[:max_bytes]
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def safe_name(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] or "untitled"


def human_size(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def find_ffmpeg() -> str | None:
    for exe in ("ffmpeg", "ffprobe"):
        p = shutil.which(exe)
        if p:
            return p
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if base.exists():
        for p in base.rglob("ffmpeg.exe"):
            return str(p)
    return None


def find_ffprobe() -> str | None:
    p = shutil.which("ffprobe")
    if p:
        return p
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if base.exists():
        for p in base.rglob("ffprobe.exe"):
            return str(p)
    return None


def sanitize_fts_term(term: str) -> str:
    return re.sub(r'["*:^()\-]', " ", term).strip()
