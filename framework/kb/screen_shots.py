from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import psutil

from . import db
from .config import load_config
from .indexer import Indexer
from .ocr import ocr_image
from .parsers.base import Block, ParsedDoc
from .screenpipe import ScreenpipeIngestor
from .textutil import clean_title
from .utils import now_str, safe_name, text_hash

_lookback_min = 5
_cooldown_sec = 300

SKIP_TITLES = {"", "Program Manager", "Windows 输入体验", "Microsoft Text Input Application"}


def _foreground() -> dict | None:
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if title in SKIP_TITLES:
            return None
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            app = psutil.Process(pid.value).name()
        except Exception:
            app = ""
        rect = wt.RECT()
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wt.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect))
        if ok != 0:
            user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect))
        if rect.right - rect.left < 80 or rect.bottom - rect.top < 80:
            return None
        return {"hwnd": hwnd, "title": title, "app": app,
                "bbox": (rect.left, rect.top, rect.right, rect.bottom)}
    except Exception:
        return None


def _entry_text(item: dict) -> str:
    c = item.get("content") or {}
    return str(c.get("text") or "").strip()


def _entry_matches(item: dict, fg: dict) -> bool:
    c = item.get("content") or {}
    win = str(c.get("window_name") or "").lower()
    app = str(c.get("app_name") or "").lower()
    ft = fg["title"].lower()
    fa = fg["app"].lower().replace(".exe", "")
    if win and (win in ft or ft in win):
        return True
    return bool(app and fa and (fa in app or app in fa))


def _has_recent_text(sp: ScreenpipeIngestor, fg: dict) -> bool:
    end = datetime.now().astimezone()
    start = end - timedelta(minutes=_lookback_min)
    items = sp.fetch(start, end, max_items=800)
    if not items:
        return True
    relevant = [it for it in items if _entry_matches(it, fg)]
    return any(_entry_text(it) for it in relevant)


def _capture(fg: dict, out_dir: Path) -> Path | None:
    from PIL import ImageGrab
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now()
    name = f"{ts:%H%M%S}_{safe_name(fg['app'] or 'screen')}.jpg"
    out = out_dir / name
    try:
        img = ImageGrab.grab(bbox=fg["bbox"], all_screens=True)
        if img.width > 2200:
            ratio = 2200 / img.width
            img = img.resize((2200, max(1, int(img.height * ratio))))
        img.convert("RGB").save(out, "JPEG", quality=85)
        return out
    except Exception:
        return None


def _store(idx: Indexer, conn, image_path: Path, fg: dict, text: str, conf: float) -> bool:
    ts = datetime.now().astimezone()
    day = ts.strftime("%Y-%m-%d")
    rel = f"{day}/{image_path.name}"
    path = f"screenshot://{rel}"
    label = (fg["title"] or fg["app"] or "屏幕")[:40]
    title = clean_title(f"屏幕截图 {ts:%Y-%m-%d %H:%M} · {label}", "屏幕截图")
    blocks = []
    if text:
        blocks.append(Block(text=text, kind="screen", title_path=label))
    parsed = ParsedDoc(title=title, author="", page_count=0, blocks=blocks,
                       meta={"screenshot_path": str(image_path), "app": fg["app"],
                             "window": fg["title"], "ocr_conf": round(conf, 3),
                             "fallback": True}, parser="screenshot")
    content_hash = text_hash(text + str(fg.get("app")) + str(fg.get("title")))
    name = image_path.name + ".md"
    meta_json = json.dumps(parsed.meta, ensure_ascii=False)[:20000]
    with idx.write_lock:
        existing = conn.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
        with db.tx(conn):
            if existing:
                doc_id = int(existing["id"])
                idx._delete_doc_content(conn, doc_id)
                conn.execute(
                    "UPDATE documents SET content_hash=?, meta_json=?, title=?, updated_at=?, "
                    "indexed_at=? WHERE id=?",
                    (content_hash, meta_json, title, now_str(), now_str(), doc_id))
            else:
                cur = conn.execute(
                    "INSERT INTO documents(path, rel_path, name, ext, size, mtime_ns, "
                    "quick_hash, content_hash, collection, department, year, doc_type, title, "
                    "author, page_count, status, parser, meta_json, ocr_status, transcript_status, "
                    "vector_status, updated_at, indexed_at) VALUES(?,?,?,'.jpg',?,?,?,?, "
                    "'屏幕记忆','个人记忆',?,'屏幕截图',?,'',0,'ok','screenshot',?, "
                    "'done','none','pending',?,?)",
                    (path, rel, name, len(parsed.blocks), int(ts.timestamp() * 1e9),
                     content_hash, content_hash, ts.year, title, meta_json, now_str(), now_str()))
                doc_id = int(cur.lastrowid)
            idx._build_chunks(conn, doc_id, parsed, name, rel, title, "屏幕截图")
            conn.execute(
                "UPDATE documents SET chunk_count=(SELECT COUNT(*) FROM chunks WHERE doc_id=?), "
                "char_count=(SELECT COALESCE(SUM(char_count),0) FROM chunks WHERE doc_id=?) "
                "WHERE id=?", (doc_id, doc_id, doc_id))
    return True


def run(interval: int = 45, once: bool = False, force: bool = False) -> None:
    cfg = load_config()
    log = logging.getLogger("kb.shots")
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.FileHandler(cfg.log_dir / "screen_shots.log", encoding="utf-8"),
                      logging.StreamHandler()])
    idx = Indexer(cfg)
    sp = ScreenpipeIngestor(cfg)
    shots_dir = cfg.data_dir / "screen_shots"
    cooldown: dict[tuple, float] = {}
    last_hash: dict[tuple, str] = {}
    log.info("截屏回退工人启动：interval=%ss force=%s", interval, force)
    while True:
        try:
            fg = _foreground()
            if fg:
                key = (fg["app"], fg["title"])
                now_ts = time.time()
                if force or (
                        now_ts - cooldown.get(key, 0) >= _cooldown_sec
                        and not _has_recent_text(sp, fg)):
                    cooldown[key] = now_ts
                    from . import bg_cpu
                    bg_cpu.wait_if_over(cfg)
                    day_dir = shots_dir / datetime.now().strftime("%Y-%m-%d")
                    img = _capture(fg, day_dir)
                    if img is not None:
                        text, conf = ocr_image(img)
                        h = text_hash((text or "") + fg["app"] + fg["title"])
                        if not force and h == last_hash.get(key):
                            img.unlink(missing_ok=True)
                        else:
                            last_hash[key] = h
                            _store(idx, sp.conn, img, fg, text, conf)
                            log.info("截图回退入库：%s chars=%d", img.name, len(text))
                    else:
                        log.warning("截图失败：%s", fg["title"][:40])
        except Exception as exc:
            log.warning("截屏回退异常: %s", exc)
        if once:
            break
        time.sleep(max(10, interval))
