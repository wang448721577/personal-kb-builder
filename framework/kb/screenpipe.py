from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import Config
from .indexer import Indexer
from .parsers.base import Block, ParsedDoc
from .textutil import clean_title, normalize_text
from .utils import now_str, safe_name, text_hash


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    m = re.match(r"^(.*?\.\d{1,6})\d*(.*)$", s)
    if m:
        s = m.group(1) + m.group(2)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.astimezone()
    except Exception:
        return None


def _clean_screen_text(text: str, limit: int = 5000) -> str:
    text = normalize_text(text or "")
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text[:limit].strip()


@dataclass
class Entry:
    ts: datetime
    kind: str
    text: str
    app: str
    window: str
    url: str
    device: str
    frame_id: str
    title_path: str


class ScreenpipeIngestor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sp = cfg.screenpipe
        self.idx = Indexer(cfg)
        self.conn = self.idx.conn()
        self.conn.execute("PRAGMA busy_timeout=300000")

    # ---------- API ----------
    def _request(self, path: str, params: dict | None = None) -> dict:
        base = self.sp.base_url.rstrip("/")
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        headers = {"Accept": "application/json"}
        token = self.sp.token or ""
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8", errors="ignore"))

    def health(self) -> bool:
        try:
            return self._request("/health").get("status") in ("healthy", "ok")
        except Exception:
            return False

    def _fetch_page(self, start: str, end: str, offset: int, limit: int = 200) -> tuple[list, int]:
        data = self._request("/search", {
            "content_type": "all",
            "start_time": start,
            "end_time": end,
            "limit": limit,
            "offset": offset,
        })
        items = data.get("data") or []
        total = int((data.get("pagination") or {}).get("total") or len(items))
        return items, total

    def fetch(self, start: datetime, end: datetime, max_items: int = 8000) -> list[dict]:
        start_s = start.isoformat(timespec="seconds")
        end_s = end.isoformat(timespec="seconds")
        out: list[dict] = []
        offset = 0
        while len(out) < max_items:
            items, total = self._fetch_page(start_s, end_s, offset)
            if not items:
                break
            out.extend(items)
            offset += len(items)
            if offset >= total or len(items) < 200:
                break
        return out[:max_items]

    # ---------- grouping ----------
    def _to_entry(self, item: dict) -> Entry | None:
        content = item.get("content") or {}
        typ = str(item.get("type") or "")
        ts = _parse_ts(content.get("timestamp"))
        if ts is None:
            return None
        app = str(content.get("app_name") or "").strip()
        window = str(content.get("window_name") or "").strip()
        url = str(content.get("browser_url") or "").strip()
        device = str(content.get("device_name") or "").strip()
        blob = (app + " " + window).lower()
        if any(x and x.lower() in blob for x in self.sp.exclude_apps):
            return None
        if "transcription" in content and content.get("transcription"):
            text = _clean_screen_text(str(content.get("transcription")), 3000)
            kind = "audio"
            title_path = "音频 " + (device or "麦克风")
        else:
            text = _clean_screen_text(str(content.get("text") or ""), 5000)
            kind = "screen"
            title_path = " | ".join(x for x in (app, window, device) if x)
        if not text and not (app or window):
            return None
        return Entry(ts=ts, kind=kind, text=text, app=app, window=window,
                     url=url, device=device,
                     frame_id=str(content.get("frame_id") or content.get("audio_chunk_id") or ""),
                     title_path=title_path)

    def _bucket(self, ts: datetime) -> datetime:
        m = (ts.minute // max(1, self.sp.bucket_minutes)) * max(1, self.sp.bucket_minutes)
        return ts.replace(minute=m, second=0, microsecond=0)

    def _build_parsed(self, bucket: datetime, key_parts: tuple[str, ...],
                      entries: list[Entry]) -> tuple[str, ParsedDoc, str, str]:
        app = next((e.app for e in entries if e.app), "")
        window = next((e.window for e in entries if e.window), "")
        device = next((e.device for e in entries if e.device), "")
        label = app or device or "桌面"
        if window:
            label += " · " + window[:50]
        title = f"屏幕记忆 {bucket:%Y-%m-%d %H:%M} · {label}"
        title = clean_title(title, f"屏幕记忆 {bucket:%Y-%m-%d %H:%M}")

        # 同一窗口内相同画面只保留一次，避免 3 秒一帧造成重复
        seen: set[str] = set()
        blocks: list[Block] = []
        total_chars = 0
        for e in sorted(entries, key=lambda x: x.ts):
            if not e.text:
                continue
            h = text_hash(e.text)
            if h in seen:
                continue
            seen.add(h)
            if len(seen) > 60 or total_chars > 24000:
                break
            prefix = f"[{e.ts:%H:%M:%S}] {e.app}".strip()
            if e.window:
                prefix += f" | {e.window[:120]}"
            if e.url:
                prefix += f" | {e.url[:300]}"
            text = prefix + "\n" + e.text
            total_chars += len(text)
            start_ms = int(e.ts.timestamp() * 1000)
            end_ms = start_ms + (3000 if e.kind == "screen" else max(1000, self.sp.bucket_minutes * 60 * 1000))
            blocks.append(Block(text=text, kind="screen" if e.kind == "screen" else "transcript",
                                title_path=e.title_path, start_ms=start_ms, end_ms=end_ms))
        if not blocks:
            blocks = [Block(text=f"[{bucket:%H:%M}] {label}（无可提取文字）", kind="screen",
                            title_path=label, start_ms=int(bucket.timestamp() * 1000),
                            end_ms=int((bucket + timedelta(minutes=self.sp.bucket_minutes)).timestamp() * 1000))]
        parsed = ParsedDoc(title=title, page_count=0, blocks=blocks,
                           meta={"bucket": bucket.isoformat(), "app": app, "window": window,
                                 "device": device, "entries": len(entries),
                                 "frame_ids": [e.frame_id for e in entries if e.frame_id][:200]},
                           parser="screenpipe")
        body = "\n\n".join(b.text for b in blocks)
        key = "|".join(key_parts)
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        path = f"screenpipe://{bucket:%Y-%m-%d}/{bucket:%H%M}/{h}"
        rel = f"屏幕记忆/{bucket:%Y-%m-%d}/{bucket:%H-%M}-{safe_name(label, 30)}-{h[:6]}"
        return path, parsed, rel, text_hash(body)

    def _upsert(self, path: str, rel: str, parsed: ParsedDoc, content_hash: str,
                bucket: datetime) -> bool:
        cfg = self.cfg
        conn = self.conn
        title = parsed.title
        name = Path(rel).name + ".md"
        meta_json = json.dumps(parsed.meta, ensure_ascii=False, default=str)[:20000]
        with self.idx.write_lock:
            existing = conn.execute("SELECT id, content_hash FROM documents WHERE path=?",
                                    (path,)).fetchone()
            if existing and existing["content_hash"] == content_hash:
                return False
            try:
                with db.tx(conn):
                    if existing:
                        doc_id = int(existing["id"])
                        self.idx._delete_doc_content(conn, doc_id)
                        conn.execute(
                            "UPDATE documents SET rel_path=?, name=?, ext='.screen', size=?, "
                            "mtime_ns=?, quick_hash=?, content_hash=?, collection='屏幕记忆', "
                            "department='个人记忆', year=?, doc_type='屏幕活动', title=?, author='', "
                            "page_count=0, status='ok', parser='screenpipe', error=NULL, meta_json=?, "
                            "duplicate_of=NULL, ocr_status='none', transcript_status='none', "
                            "vector_status='pending', vector_count=0, updated_at=?, indexed_at=? "
                            "WHERE id=?",
                            (rel, name, len(parsed.blocks), int(bucket.timestamp() * 1e9),
                             content_hash, content_hash, bucket.year, title, meta_json,
                             now_str(), now_str(), doc_id),
                        )
                    else:
                        cur = conn.execute(
                            "INSERT INTO documents(path, rel_path, name, ext, size, mtime_ns, "
                            "quick_hash, content_hash, collection, department, year, doc_type, title, "
                            "author, page_count, status, parser, meta_json, ocr_status, transcript_status, "
                            "vector_status, updated_at, indexed_at) VALUES(?,?,?,'.screen',?,?,?,?, "
                            "'屏幕记忆','个人记忆',?,'屏幕活动',?,'',0,'ok','screenpipe',?, "
                            "'none','none','pending',?,?)",
                            (path, rel, name, len(parsed.blocks), int(bucket.timestamp() * 1e9),
                             content_hash, content_hash, bucket.year, title, meta_json,
                             now_str(), now_str()),
                        )
                        doc_id = int(cur.lastrowid)
                    chunk_count = self.idx._build_chunks(conn, doc_id, parsed, name, rel, title,
                                                         "屏幕活动")
                    char_count = conn.execute(
                        "SELECT COALESCE(SUM(char_count),0) n FROM chunks WHERE doc_id=?",
                        (doc_id,)).fetchone()["n"]
                    conn.execute(
                        "UPDATE documents SET chunk_count=?, char_count=? WHERE id=?",
                        (chunk_count, int(char_count), doc_id),
                    )
                return True
            except Exception:
                conn.rollback()
                raise

    # ---------- main ----------
    def ingest(self, since: datetime | None = None, until: datetime | None = None) -> dict:
        now = datetime.now().astimezone()
        until = until or now
        if since is None:
            last = db.get_meta(self.conn, "screenpipe_last_ts")
            last_dt = _parse_ts(last) if last else None
            if last_dt:
                since = last_dt - timedelta(minutes=max(1, self.sp.lookback_minutes))
            else:
                since = now - timedelta(minutes=max(1, self.sp.lookback_minutes))
        if since >= until:
            return {"entries": 0, "groups": 0, "written": 0, "since": since.isoformat(),
                    "until": until.isoformat()}

        items = self.fetch(since, until)
        entries = [e for e in (self._to_entry(i) for i in items) if e is not None]
        groups: dict[tuple, list[Entry]] = {}
        for e in entries:
            bucket = self._bucket(e.ts)
            key = (bucket, e.app or e.device or "", e.window or "")
            groups.setdefault(key, []).append(e)

        written = 0
        for (bucket, app, window), group in groups.items():
            path, parsed, rel, content_hash = self._build_parsed(
                bucket, (bucket.isoformat(), app, window), group)
            if self._upsert(path, rel, parsed, content_hash, bucket):
                written += 1
        db.set_meta(self.conn, "screenpipe_last_ts", until.isoformat())
        self.conn.commit()
        return {"entries": len(items), "usable": len(entries), "groups": len(groups),
                "written": written, "since": since.isoformat(), "until": until.isoformat()}

    def prune(self, days: int | None = None) -> int:
        days = int(days or self.sp.retention_days)
        if days <= 0:
            return 0
        cutoff = datetime.now().astimezone() - timedelta(days=days)
        rows = self.conn.execute(
            "SELECT path FROM documents WHERE path LIKE 'screenpipe://%'").fetchall()
        removed = 0
        for row in rows:
            p = row["path"]
            try:
                day = datetime.strptime(p.split("/")[2], "%Y-%m-%d").replace(
                    tzinfo=datetime.now().astimezone().tzinfo)
            except Exception:
                continue
            if day < cutoff:
                try:
                    self.idx.delete_path(Path(p))
                    removed += 1
                except Exception:
                    pass
        return removed

    def status(self) -> dict:
        docs = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(chunk_count),0) c FROM documents "
            "WHERE path LIKE 'screenpipe://%' AND status='ok'").fetchone()
        last = db.get_meta(self.conn, "screenpipe_last_ts")
        return {
            "screenpipe_running": self.health(),
            "screen_documents": int(docs["n"]),
            "screen_chunks": int(docs["c"]),
            "last_ingested": last,
            "base_url": self.sp.base_url,
            "data_dir": self.sp.data_dir,
        }
