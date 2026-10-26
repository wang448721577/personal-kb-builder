from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .config import Config
from .indexer import Indexer
from .parsers.base import Block, ParsedDoc
from .textutil import clean_title
from .utils import now_str, safe_name, text_hash


class WeChatIngestor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.wc = cfg.wechat
        self.idx = Indexer(cfg)
        self.conn = self.idx.conn()
        self.conn.execute("PRAGMA busy_timeout=300000")

    def _export(self, since: int, recent: int) -> tuple[Path, dict]:
        cfg = self.cfg
        export = cfg.data_dir / "cache" / "wechat_export.jsonl"
        export.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.data_dir / "cache" / "wechat_tmp"
        keys = cfg.data_dir / "cache" / "wechat_keys"
        tmp.mkdir(parents=True, exist_ok=True)
        keys.mkdir(parents=True, exist_ok=True)
        log_path = cfg.data_dir / "cache" / "wechat_export.err.txt"
        cmd = [
            sys.executable, "-m", "kb.wechat_export",
            "--out", str(export),
            "--since", str(max(0, since)),
            "--recent", str(max(0, recent)),
            "--max-per-session", str(max(1000, self.wc.max_messages_per_session)),
        ]
        for p in self.wc.exclude_prefixes:
            cmd += ["--exclude-prefix", p]
        for p in self.wc.exclude_chats:
            cmd += ["--exclude-chat", p]
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": str(cfg.home),
            "PYTHONIOENCODING": "utf-8",
            "TEMP": str(tmp),
            "TMP": str(tmp),
            "KB_WECHAT_TMP": str(tmp),
            "WECHATAUTO_KEYS_DIR": str(keys),
        })
        with open(log_path, "wb") as err:
            proc = subprocess.Popen(
                cmd, cwd=str(cfg.home), env=env,
                stdout=subprocess.PIPE, stderr=err,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                out, _ = proc.communicate(timeout=3600)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                raise RuntimeError("微信导出超时（3600 秒）")
        if proc.returncode != 0:
            msg = log_path.read_text(encoding="utf-8", errors="ignore")[-1500:]
            raise RuntimeError("微信导出失败: " + msg)
        try:
            summary = json.loads((out or b"{}").decode("utf-8", errors="ignore") or "{}")
        except Exception:
            summary = {}
        return export, summary

    def _upsert(self, account: str, username: str, nickname: str, month: str,
                blocks: list[Block], meta: dict, content_hash: str,
                max_ts: int, is_group: bool) -> bool:
        cfg = self.cfg
        conn = self.conn
        ah = hashlib.sha1(account.encode("utf-8")).hexdigest()[:8]
        sh = hashlib.sha1(username.encode("utf-8")).hexdigest()[:12]
        path = f"wechat://{ah}/{sh}/{month}"
        rel = f"微信聊天/{safe_name(nickname, 30)}/{month}.md"
        name = Path(rel).name
        title = clean_title(f"微信 · {nickname} · {month}", f"微信聊天 {month}")
        meta_json = json.dumps(meta, ensure_ascii=False, default=str)[:20000]
        parsed = ParsedDoc(title=title, page_count=0, blocks=blocks, meta=meta,
                           parser="wechat")
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
                            "UPDATE documents SET rel_path=?, name=?, ext='.chat', size=?, "
                            "mtime_ns=?, quick_hash=?, content_hash=?, collection='微信聊天', "
                            "department='个人通讯', year=?, doc_type='聊天记录', title=?, author='', "
                            "page_count=0, status='ok', parser='wechat', error=NULL, meta_json=?, "
                            "duplicate_of=NULL, ocr_status='none', transcript_status='none', "
                            "vector_status='pending', vector_count=0, updated_at=?, indexed_at=? "
                            "WHERE id=?",
                            (rel, name, meta.get("message_count", 0), int(max_ts * 1e9),
                             content_hash, content_hash, int(month[:4]), title, meta_json,
                             now_str(), now_str(), doc_id),
                        )
                    else:
                        cur = conn.execute(
                            "INSERT INTO documents(path, rel_path, name, ext, size, mtime_ns, "
                            "quick_hash, content_hash, collection, department, year, doc_type, title, "
                            "author, page_count, status, parser, meta_json, ocr_status, transcript_status, "
                            "vector_status, updated_at, indexed_at) VALUES(?,?,?,'.chat',?,?,?,?, "
                            "'微信聊天','个人通讯',?,'聊天记录',?,'',0,'ok','wechat',?, "
                            "'none','none','pending',?,?)",
                            (path, rel, name, meta.get("message_count", 0), int(max_ts * 1e9),
                             content_hash, content_hash, int(month[:4]), title, meta_json,
                             now_str(), now_str()),
                        )
                        doc_id = int(cur.lastrowid)
                    chunk_count = self.idx._build_chunks(conn, doc_id, parsed, name, rel, title,
                                                         "聊天记录")
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

    def sync(self, full: bool | None = None, recent: int | None = None) -> dict:
        conn = self.conn
        last = db.get_meta(conn, "wechat_last_ts")
        if full is None:
            full = last is None
        if full:
            since = 0
            recent = 0 if recent is None else max(0, int(recent))
        else:
            since = max(0, int(last) - 2 * 86400) if last else int(time.time()) - 365 * 86400
            recent = max(100, recent if recent is not None else self.wc.recent_limit)
        export, summary = self._export(since, recent)

        account = ""
        sessions: dict[str, dict] = {}
        by_session_month: dict[tuple[str, str], list[dict]] = defaultdict(list)
        with open(export, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "account":
                    account = obj.get("username", "")
                elif obj.get("type") == "session":
                    sessions[obj["username"]] = obj
                elif obj.get("type") == "message":
                    ts = int(obj.get("ts") or 0)
                    if not ts:
                        continue
                    month = datetime.fromtimestamp(ts).strftime("%Y-%m")
                    by_session_month[(obj.get("username", ""), month)].append(obj)

        written = 0
        max_seen = 0
        for (username, month), messages in by_session_month.items():
            if not messages:
                continue
            info = sessions.get(username, {})
            nickname = info.get("nickname") or username
            is_group = bool(info.get("is_group"))
            messages.sort(key=lambda x: (int(x.get("ts") or 0), int(x.get("local_id") or 0)))
            blocks: list[Block] = []
            buf: list[str] = []
            buf_chars = 0
            start_ms = end_ms = None
            types = Counter()

            def flush():
                nonlocal buf, buf_chars, start_ms, end_ms
                if not buf:
                    return
                text = "\n".join(buf)
                blocks.append(Block(text=text, kind="chat", title_path=nickname,
                                    start_ms=start_ms, end_ms=end_ms))
                buf, buf_chars, start_ms, end_ms = [], 0, None, None

            for m in messages:
                ts = int(m.get("ts") or 0)
                sender = str(m.get("sender_name") or m.get("sender_username") or "")
                if sender == account or (not sender):
                    label = "我" if sender == account else "系统"
                elif sender == nickname:
                    label = nickname
                else:
                    label = sender
                content = str(m.get("content") or "").strip()
                if not content:
                    continue
                mtype = str(m.get("msg_type") or m.get("type") or "")
                types[mtype] += 1
                line = f"[{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}] {label}: {content}"
                if buf and (buf_chars + len(line) > 1800 or len(buf) >= 25):
                    flush()
                if start_ms is None:
                    start_ms = ts * 1000
                end_ms = ts * 1000 + 1000
                buf.append(line)
                buf_chars += len(line) + 1
                max_seen = max(max_seen, ts)
            flush()
            if not blocks:
                continue
            body = "\n\n".join(b.text for b in blocks)
            content_hash = text_hash(body)
            meta = {
                "account": account, "session": username, "nickname": nickname,
                "month": month, "is_group": is_group,
                "message_count": len(messages), "types": dict(types),
                "export_summary": summary,
            }
            doc_max = max((int(m.get("ts") or 0) for m in messages), default=0)
            if self._upsert(account, username, nickname, month, blocks, meta, content_hash,
                            doc_max, is_group):
                written += 1
        if max_seen:
            db.set_meta(conn, "wechat_last_ts", str(max_seen))
        db.set_meta(conn, "wechat_last_sync", now_str())
        conn.commit()
        try:
            export.unlink(missing_ok=True)
        except Exception:
            pass
        return {"full": bool(full), "sessions": len(sessions),
                "months": len(by_session_month), "written": written,
                "last_ts": max_seen, "export": summary}

    def import_file(self, export: Path, delete: bool = False) -> dict:
        conn = self.conn
        account = ""
        sessions: dict[str, dict] = {}
        by_session_month: dict[tuple[str, str], list[dict]] = defaultdict(list)
        with open(export, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "account":
                    account = obj.get("username", "")
                elif obj.get("type") == "session":
                    sessions[obj["username"]] = obj
                elif obj.get("type") == "message":
                    ts = int(obj.get("ts") or 0)
                    if not ts:
                        continue
                    month = datetime.fromtimestamp(ts).strftime("%Y-%m")
                    by_session_month[(obj.get("username", ""), month)].append(obj)
        written = 0
        max_seen = 0
        for (username, month), messages in by_session_month.items():
            if not messages:
                continue
            info = sessions.get(username, {})
            nickname = info.get("nickname") or username
            is_group = bool(info.get("is_group"))
            messages.sort(key=lambda x: (int(x.get("ts") or 0), int(x.get("local_id") or 0)))
            blocks: list[Block] = []
            buf: list[str] = []
            chars = 0
            start_ms = end_ms = None
            types = Counter()
            def flush_local():
                nonlocal buf, chars, start_ms, end_ms
                if buf:
                    blocks.append(Block(text="\n".join(buf), kind="chat",
                                        title_path=nickname, start_ms=start_ms, end_ms=end_ms))
                    buf, chars, start_ms, end_ms = [], 0, None, None
            for m in messages:
                ts = int(m.get("ts") or 0)
                sender = str(m.get("sender_name") or m.get("sender_username") or "")
                label = "我" if sender == account else (sender or "系统")
                content = str(m.get("content") or "").strip()
                if not content:
                    continue
                types[str(m.get("msg_type") or m.get("type") or "")] += 1
                line = f"[{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}] {label}: {content}"
                if buf and (chars + len(line) > 1800 or len(buf) >= 25):
                    flush_local()
                if start_ms is None:
                    start_ms = ts * 1000
                end_ms = ts * 1000 + 1000
                buf.append(line)
                chars += len(line) + 1
                max_seen = max(max_seen, ts)
            flush_local()
            if not blocks:
                continue
            body = "\n\n".join(b.text for b in blocks)
            meta = {"account": account, "session": username, "nickname": nickname,
                    "month": month, "is_group": is_group, "message_count": len(messages),
                    "types": dict(types)}
            doc_max = max((int(m.get("ts") or 0) for m in messages), default=0)
            if self._upsert(account, username, nickname, month, blocks, meta,
                            text_hash(body), doc_max, is_group):
                written += 1
        if max_seen:
            db.set_meta(conn, "wechat_last_ts", str(max_seen))
        db.set_meta(conn, "wechat_last_sync", now_str())
        conn.commit()
        if delete:
            try:
                export.unlink(missing_ok=True)
            except Exception:
                pass
        return {"sessions": len(sessions), "months": len(by_session_month),
                "written": written, "last_ts": max_seen}

    def status(self) -> dict:
        docs = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(chunk_count),0) c FROM documents "
            "WHERE path LIKE 'wechat://%' AND status='ok'").fetchone()
        return {
            "enabled": self.wc.enabled,
            "chat_documents": int(docs["n"]),
            "chat_chunks": int(docs["c"]),
            "last_ts": db.get_meta(self.conn, "wechat_last_ts"),
            "last_sync": db.get_meta(self.conn, "wechat_last_sync"),
            "exclude_prefixes": self.wc.exclude_prefixes,
            "exclude_chats": self.wc.exclude_chats,
        }
