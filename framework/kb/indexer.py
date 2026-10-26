from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import db, vectorstore
from .config import IMAGE_EXTS, MEDIA_EXTS, Config
from .parsers.base import Block, ParsedDoc, parse_file
from .tables import export_tables
from .textutil import (chunk_text, classify, clean_title, estimate_tokens,
                       normalize_text, tokenize_for_fts)
from .utils import human_size, now_str, quick_hash, text_hash

log = logging.getLogger("kb.indexer")

LEGACY_EXTS = {".doc", ".xls", ".ppt"}


@dataclass
class ParseResult:
    path: str
    rel_path: str
    size: int
    mtime_ns: int
    quick_hash: str
    parsed: ParsedDoc | None = None
    error: str = ""
    skipped: bool = False
    duplicate_of: int | None = None


def _chunk_blocks(parsed: ParsedDoc, cfg: Config):
    for block in parsed.blocks:
        text = normalize_text(block.text)
        if len(text) < 2:
            continue
        limit = cfg.search.max_chunk_chars if block.kind in ("table", "slide") else cfg.search.chunk_chars
        overlap = cfg.search.overlap_chars
        parts = chunk_text(text, limit, overlap) if len(text) > limit else [text]
        for part in parts:
            yield part, block


class Indexer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.write_lock = threading.Lock()
        self._local = threading.local()
        self._legacy_sem = threading.Semaphore(max(1, cfg.legacy_workers))
        self._stop = threading.Event()

    # ---------- connections ----------
    def conn(self) -> "db.sqlite3.Connection":
        c = getattr(self._local, "conn", None)
        if c is None:
            c = db.connect(self.cfg)
            self._local.conn = c
        return c

    def worker_conn(self):
        c = getattr(self._local, "ro_conn", None)
        if c is None:
            c = db.connect(self.cfg, readonly=True)
            self._local.ro_conn = c
        return c

    # ---------- scanning ----------
    def scan_paths(self, limit: int | None = None, path_filter: str | None = None):
        cfg = self.cfg
        roots = [cfg.root / d for d in cfg.include_dirs] if cfg.include_dirs else [cfg.root]
        exts = cfg.include_exts
        excluded = cfg.excluded_set
        filter_lower = (path_filter or "").lower()
        count = 0
        for base in roots:
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [
                    d for d in dirnames
                    if d.lower() not in excluded
                    and not d.startswith(".")
                    and d.lower() not in {"$recycle.bin", "system volume information"}
                ]
                for name in filenames:
                    lower = name.lower()
                    if lower.startswith("~$") or lower in {"desktop.ini", "thumbs.db", ".ds_store", "header.htm", "footer.htm"}:
                        continue
                    if lower.startswith(".") or lower.endswith((".tmp", ".bak", ".part", ".crdownload")):
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in exts:
                        continue
                    path = Path(dirpath) / name
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    if st.st_size <= 0 or st.st_size > cfg.max_size_for(ext):
                        continue
                    rel = cfg.rel(path)
                    if filter_lower and filter_lower not in rel.lower():
                        continue
                    count += 1
                    yield path
                    if limit and count >= limit:
                        return

    def is_problem_path(self, path: Path) -> bool:
        try:
            row = self.worker_conn().execute(
                "SELECT status FROM documents WHERE path=?", (str(path),)).fetchone()
            return bool(row and row["status"] in ("error", "skipped"))
        except Exception:
            return False

    def _needs_work(self, conn, path: Path) -> bool:
        try:
            st = path.stat()
        except OSError:
            return False
        row = conn.execute(
            "SELECT size, mtime_ns, status FROM documents WHERE path=?",
            (str(path),),
        ).fetchone()
        if not row:
            return True
        if row["status"] in ("error", "skipped"):
            return False
        if row["size"] != st.st_size or row["mtime_ns"] != st.st_mtime_ns:
            return True
        return False

    def _parse_pdf_child(self, path: Path):
        import subprocess
        import sys
        import tempfile
        from .parsers.base import Block, ParsedDoc
        worker = Path(__file__).resolve().parent / "pdf_worker.py"
        out_dir = self.cfg.cache_dir / "pdfjson"
        out_dir.mkdir(parents=True, exist_ok=True)
        fd, out_name = tempfile.mkstemp(suffix=".json", dir=str(out_dir))
        os.close(fd)
        out = Path(out_name)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.cfg.home)
        env["PYTHONIOENCODING"] = "utf-8"
        err_path = out.with_suffix(".err.txt")
        err_file = open(err_path, "w+b")
        proc = subprocess.Popen(
            [sys.executable, str(worker), str(path), str(out)],
            cwd=str(self.cfg.home), env=env, stdout=subprocess.DEVNULL,
            stderr=err_file, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            proc.wait(timeout=240)
            if proc.returncode != 0:
                err_file.seek(0)
                msg = err_file.read().decode("utf-8", errors="ignore")[-500:]
                raise RuntimeError(f"PDF 解析失败: {msg}")
            data = json.loads(out.read_text(encoding="utf-8"))
            data["blocks"] = [Block(**b) for b in data.get("blocks", [])]
            return ParsedDoc(**data)
        finally:
            try:
                err_file.close()
                out.unlink(missing_ok=True)
                err_path.unlink(missing_ok=True)
            except Exception:
                pass
            if proc.poll() is None:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # ---------- parsing ----------
    def _parse_task(self, path: Path) -> ParseResult:
        try:
            st = path.stat()
            qh = quick_hash(path)
        except Exception as exc:
            return ParseResult(str(path), self.cfg.rel(path), 0, 0, "", error=str(exc), skipped=True)

        rel = self.cfg.rel(path)
        try:
            ro = self.worker_conn()
            dup = ro.execute(
                "SELECT id FROM documents WHERE size=? AND quick_hash=? AND status='ok' "
                "AND duplicate_of IS NULL AND path<>? LIMIT 1",
                (st.st_size, qh, str(path)),
            ).fetchone()
            if dup:
                return ParseResult(str(path), rel, st.st_size, st.st_mtime_ns, qh,
                                   duplicate_of=int(dup["id"]))
        except Exception:
            pass

        ext = path.suffix.lower()
        try:
            if ext in (".pdf", ".djvu"):
                parsed = self._parse_pdf_child(path)
            elif ext in LEGACY_EXTS:
                with self._legacy_sem:
                    parsed = parse_file(path, self.cfg)
            else:
                parsed = parse_file(path, self.cfg)
            return ParseResult(str(path), rel, st.st_size, st.st_mtime_ns, qh, parsed=parsed)
        except Exception as exc:
            return ParseResult(str(path), rel, st.st_size, st.st_mtime_ns, qh,
                               error=f"{type(exc).__name__}: {exc}", skipped=True)

    # ---------- document writes ----------
    def _delete_doc_content(self, conn, doc_id: int) -> None:
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
        for cid in ids:
            conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        for row in conn.execute("SELECT path FROM doc_tables WHERE doc_id=?", (doc_id,)).fetchall():
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        conn.execute("DELETE FROM doc_tables WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM jobs WHERE doc_id=?", (doc_id,))
        try:
            vectorstore.delete_doc(self.cfg, doc_id)
        except Exception:
            pass

    def _add_chunk(self, conn, doc_id: int, seq: int, text: str, kind: str,
                   title_path: str = "", page_start=None, page_end=None,
                   sheet=None, slide=None, start_ms=None, end_ms=None) -> int:
        text = text or ""
        th = text_hash(text)
        cur = conn.execute(
            "INSERT INTO chunks(doc_id, seq, kind, title_path, page_start, page_end, sheet, "
            "slide, start_ms, end_ms, text, text_hash, token_count, char_count, embedded) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (doc_id, seq, kind, title_path, page_start, page_end, sheet, slide,
             start_ms, end_ms, text, th, estimate_tokens(text), len(text)),
        )
        cid = int(cur.lastrowid)
        tok = tokenize_for_fts((title_path or "") + "\n" + text)
        conn.execute(
            "INSERT INTO chunks_fts(rowid, text_tok, chunk_id, doc_id) VALUES(?,?,?,?)",
            (cid, tok, cid, doc_id),
        )
        return cid

    def _build_chunks(self, conn, doc_id: int, parsed: ParsedDoc, name: str,
                      rel_path: str, title: str, doc_type: str) -> int:
        meta_text = (f"文件名：{name}\n标题：{title}\n路径：{rel_path}\n类型：{doc_type}")
        self._add_chunk(conn, doc_id, 0, meta_text, "meta", "文件信息")
        seq = 1
        for text, block in _chunk_blocks(parsed, self.cfg):
            self._add_chunk(conn, doc_id, seq, text, block.kind, block.title_path,
                            block.page_start, block.page_end, block.sheet, block.slide,
                            block.start_ms, block.end_ms)
            seq += 1
        return seq - 1

    def _enqueue(self, conn, kind: str, doc_id: int, payload: dict, priority: int = 100) -> None:
        conn.execute(
            "INSERT INTO jobs(kind, doc_id, page, state, priority, payload) "
            "VALUES(?,?,0,'pending',?,?) ON CONFLICT(kind, doc_id, page) DO UPDATE SET "
            "state='pending', attempts=0, error=NULL, priority=excluded.priority, "
            "payload=excluded.payload, updated_at=CURRENT_TIMESTAMP",
            (kind, doc_id, priority, json.dumps(payload, ensure_ascii=False)),
        )

    def _mark_duplicate(self, conn, existing, result: ParseResult, canonical_id: int,
                        title: str, author: str, cls: dict, content_hash: str) -> None:
        path = Path(result.path)
        if existing:
            self._delete_doc_content(conn, int(existing["id"]))
            conn.execute(
                "UPDATE documents SET size=?, mtime_ns=?, quick_hash=?, content_hash=?, title=?, "
                "author=?, collection=?, department=?, year=?, doc_type=?, status='duplicate', "
                "duplicate_of=?, error=NULL, chunk_count=0, char_count=0, vector_count=0, "
                "updated_at=?, indexed_at=? WHERE id=?",
                (result.size, result.mtime_ns, result.quick_hash, content_hash, title, author,
                 cls["collection"], cls["department"], cls["year"], cls["doc_type"],
                 canonical_id, now_str(), now_str(), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO documents(path, rel_path, name, ext, size, mtime_ns, quick_hash, "
                "content_hash, collection, department, year, doc_type, title, author, status, "
                "duplicate_of, updated_at, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, "
                "'duplicate',?,?,?)",
                (str(path), result.rel_path, path.name, path.suffix.lower(), result.size,
                 result.mtime_ns, result.quick_hash, content_hash, cls["collection"],
                 cls["department"], cls["year"], cls["doc_type"], title, author,
                 canonical_id, now_str(), now_str()),
            )
        conn.execute(
            "INSERT OR REPLACE INTO aliases(content_hash, path, canonical_doc_id) VALUES(?,?,?)",
            (content_hash, str(path), canonical_id),
        )

    def _export_tables(self, conn, doc_id: int, path: Path, parsed: ParsedDoc) -> None:
        ext = path.suffix.lower()
        source = path
        source_ext = ext
        if ext == ".xls":
            converted = parsed.meta.get("converted_path")
            if converted:
                source = Path(converted)
                source_ext = ".xlsx"
        if source_ext not in (".xlsx", ".csv"):
            return
        try:
            rows = export_tables(source, doc_id, self.cfg, source_ext)
            for r in rows:
                conn.execute(
                    "INSERT INTO doc_tables(doc_id, sheet, path, format, rows, cols, columns_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (doc_id, r["sheet"], r["path"], r["format"], r["rows"], r["cols"],
                     r["columns_json"]),
                )
        except Exception as exc:
            log.warning("表格导出失败 %s: %s", path, exc)

    def apply_result(self, result: ParseResult) -> str:
        cfg = self.cfg
        path = Path(result.path)
        name = path.name
        ext = path.suffix.lower()
        rel = result.rel_path
        cls = classify(rel, ext, path.stat().st_mtime if path.exists() else None)

        with self.write_lock:
            conn = self.conn()
            existing = conn.execute("SELECT * FROM documents WHERE path=?", (str(path),)).fetchone()
            try:
                with db.tx(conn):
                    if result.error:
                        status = "skipped" if result.skipped else "error"
                        if existing:
                            if result.skipped:
                                self._delete_doc_content(conn, int(existing["id"]))
                            conn.execute(
                                "UPDATE documents SET size=?, mtime_ns=?, quick_hash=?, status=?, "
                                "error=?, chunk_count=0, char_count=0, vector_count=0, updated_at=? WHERE id=?",
                                (result.size, result.mtime_ns, result.quick_hash, status,
                                 result.error[:1000], now_str(), existing["id"]),
                            )
                            return status
                        conn.execute(
                            "INSERT INTO documents(path, rel_path, name, ext, size, mtime_ns, quick_hash, "
                            "collection, department, year, doc_type, status, error, updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (str(path), rel, name, ext, result.size, result.mtime_ns,
                             result.quick_hash, cls["collection"], cls["department"],
                             cls["year"], cls["doc_type"], status, result.error[:1000],
                             now_str()),
                        )
                        return status

                    parsed = result.parsed
                    if result.duplicate_of:
                        title = clean_title(parsed.title if parsed else "", path.stem)
                        author = (parsed.author if parsed else "") or ""
                        self._mark_duplicate(conn, existing, result, result.duplicate_of,
                                             title, author, cls,
                                             "quick:" + result.quick_hash)
                        return "duplicate"
                    assert parsed is not None
                    title = clean_title(parsed.title, path.stem)
                    author = parsed.author or ""
                    meta_json = json.dumps(parsed.meta, ensure_ascii=False, default=str)[:20000]

                    body = "\n".join(b.text for b in parsed.blocks)
                    content_hash = text_hash(body) if body else "quick:" + result.quick_hash
                    canonical = conn.execute(
                        "SELECT id FROM documents WHERE content_hash=? AND status='ok' "
                        "AND duplicate_of IS NULL AND id<>? LIMIT 1",
                        (content_hash, existing["id"] if existing else 0),
                    ).fetchone()
                    if canonical:
                        self._mark_duplicate(conn, existing, result, int(canonical["id"]),
                                             title, author, cls, content_hash)
                        return "duplicate"
                    needs_ocr = bool(parsed.needs_ocr)
                    ocr_image = bool(parsed.meta.get("ocr_image"))
                    transcribe = bool(parsed.meta.get("transcribe"))
                    ocr_status = "pending" if (needs_ocr or ocr_image) else "none"
                    transcript_status = "pending" if transcribe else "none"

                    if existing:
                        doc_id = int(existing["id"])
                        self._delete_doc_content(conn, doc_id)
                        conn.execute(
                            "UPDATE documents SET rel_path=?, name=?, ext=?, size=?, mtime_ns=?, "
                            "quick_hash=?, content_hash=?, collection=?, department=?, year=?, doc_type=?, "
                            "title=?, author=?, page_count=?, status='ok', error=NULL, parser=?, meta_json=?, "
                            "duplicate_of=NULL, ocr_status=?, transcript_status=?, vector_status=?, "
                            "vector_count=0, updated_at=?, indexed_at=? WHERE id=?",
                            (rel, name, ext, result.size, result.mtime_ns, result.quick_hash,
                             content_hash, cls["collection"], cls["department"], cls["year"],
                             cls["doc_type"], title, author, int(parsed.page_count or 0),
                             parsed.parser, meta_json, ocr_status, transcript_status,
                             "pending" if cfg.embedding.enabled else "none",
                             now_str(), now_str(), doc_id),
                        )
                    else:
                        cur = conn.execute(
                            "INSERT INTO documents(path, rel_path, name, ext, size, mtime_ns, quick_hash, "
                            "content_hash, collection, department, year, doc_type, title, author, page_count, "
                            "status, parser, meta_json, ocr_status, transcript_status, vector_status, "
                            "updated_at, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'ok', ?,?,?,?,?,?,?)",
                            (str(path), rel, name, ext, result.size, result.mtime_ns,
                             result.quick_hash, content_hash, cls["collection"], cls["department"],
                             cls["year"], cls["doc_type"], title, author,
                             int(parsed.page_count or 0), parsed.parser, meta_json, ocr_status,
                             transcript_status, "pending" if cfg.embedding.enabled else "none",
                             now_str(), now_str()),
                        )
                        doc_id = int(cur.lastrowid)

                    chunk_count = self._build_chunks(conn, doc_id, parsed, name, rel, title,
                                                     cls["doc_type"])
                    char_count = conn.execute(
                        "SELECT COALESCE(SUM(char_count),0) n FROM chunks WHERE doc_id=?",
                        (doc_id,)).fetchone()["n"]
                    conn.execute(
                        "UPDATE documents SET chunk_count=?, char_count=? WHERE id=?",
                        (chunk_count, int(char_count), doc_id),
                    )

                    self._export_tables(conn, doc_id, path, parsed)

                    if needs_ocr:
                        pages = parsed.needs_ocr[: int(self.cfg.ocr.max_pages or 2000) or 2000]
                        self._enqueue(conn, "ocr", doc_id, {"type": "pdf", "pages": pages},
                                      priority=150 if ext == ".caj" else 50)
                    elif ocr_image:
                        self._enqueue(conn, "ocr", doc_id, {"type": "image"}, priority=80)
                    if transcribe:
                        self._enqueue(conn, "transcribe", doc_id, {"type": "media"}, priority=200)
                    return "ok"
            except Exception as exc:
                log.exception("写入失败 %s: %s", path, exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                return "error"

    # ---------- main run ----------
    def run(self, full: bool = False, limit: int | None = None,
            path_filter: str | None = None, workers: int | None = None,
            retry_problems: bool = False, respect_budget: bool = True) -> dict:
        cfg = self.cfg
        db.init_db(cfg)
        conn = self.conn()
        cur = conn.execute(
            "INSERT INTO index_runs(started_at, mode, notes) VALUES(?,?,?)",
            (now_str(), "full" if full else "incremental", path_filter or ""),
        )
        run_id = int(cur.lastrowid)
        conn.commit()

        all_paths = list(self.scan_paths(limit=limit, path_filter=path_filter))
        if full:
            paths = [p for p in all_paths
                     if retry_problems or not self.is_problem_path(p)]
        else:
            paths = [p for p in all_paths if self._needs_work(conn, p)]
        skipped = len(all_paths) - len(paths)
        log.info("待处理 %d 个文件（跳过未变化 %d，总计 %d）", len(paths), skipped, len(all_paths))
        chunks_before = int(conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"])

        stats = {"ok": 0, "error": 0, "duplicate": 0, "chunks": 0}
        started = time.time()
        workers = workers or cfg.workers
        budget = float(getattr(cfg, "background_cpu_percent", 0) or 0) if respect_budget else 0
        if budget > 0:
            import psutil
            cores = psutil.cpu_count(logical=True) or 1
            workers = min(workers, max(1, int(cores * budget / 100.0)))
        done = 0
        from . import bg_cpu

        def _parse_guarded(path):
            if budget > 0:
                bg_cpu.wait_if_over(cfg)
            return self._parse_task(path)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for result in ex.map(_parse_guarded, paths):
                done += 1
                status = self.apply_result(result)
                stats[status] = stats.get(status, 0) + 1
                if done % 50 == 0 or done == len(paths):
                    stats["chunks"] = max(0, int(conn.execute(
                        "SELECT COUNT(*) n FROM chunks").fetchone()["n"]) - chunks_before)
                    elapsed = max(0.001, time.time() - started)
                    rate = done / elapsed
                    log.info("[%d/%d] ok=%d dup=%d err=%d | %.1f 文件/秒 | %s",
                             done, len(paths), stats["ok"], stats["duplicate"],
                             stats["error"], rate, time.strftime("%H:%M:%S"))
                    conn.execute(
                        "UPDATE index_runs SET files_seen=?, files_indexed=?, files_skipped=?, "
                        "files_failed=?, chunks_added=? WHERE id=?",
                        (len(all_paths), stats["ok"], skipped, stats["error"], stats["chunks"], run_id),
                    )
                    conn.commit()

        conn.execute(
            "UPDATE index_runs SET finished_at=?, files_seen=?, files_indexed=?, files_skipped=?, "
            "files_failed=?, chunks_added=? WHERE id=?",
            (now_str(), len(all_paths), stats["ok"], skipped, stats["error"], stats["chunks"], run_id),
        )
        conn.commit()
        try:
            self.dedupe_existing()
        except Exception as exc:
            log.warning("运行后去重失败: %s", exc)
        log.info("索引完成：%s", stats)
        return {"run_id": run_id, "seen": len(all_paths), "indexed": stats["ok"],
                "duplicates": stats["duplicate"], "failed": stats["error"],
                "chunks": stats["chunks"], "skipped": skipped,
                "elapsed_sec": round(time.time() - started, 1)}

    # ---------- deletions ----------
    def delete_path(self, path: Path) -> bool:
        with self.write_lock:
            conn = self.conn()
            row = conn.execute("SELECT id FROM documents WHERE path=?", (str(path),)).fetchone()
            if not row:
                return False
            with db.tx(conn):
                self._delete_doc_content(conn, int(row["id"]))
                conn.execute("DELETE FROM documents WHERE id=?", (row["id"],))
            return True

    def dedupe_existing(self) -> int:
        conn = self.conn()
        groups = conn.execute(
            "SELECT content_hash, COUNT(*) n FROM documents WHERE status='ok' "
            "AND duplicate_of IS NULL AND content_hash IS NOT NULL "
            "GROUP BY content_hash HAVING n > 1"
        ).fetchall()
        removed = 0
        for g in groups:
            rows = conn.execute(
                "SELECT id, path FROM documents WHERE content_hash=? AND status='ok' "
                "AND duplicate_of IS NULL ORDER BY LENGTH(path), id", (g["content_hash"],)
            ).fetchall()
            if len(rows) < 2:
                continue
            keep = int(rows[0]["id"])
            for r in rows[1:]:
                did = int(r["id"])
                with self.write_lock:
                    try:
                        with db.tx(conn):
                            self._delete_doc_content(conn, did)
                            conn.execute(
                                "UPDATE documents SET status='duplicate', duplicate_of=?, "
                                "chunk_count=0, char_count=0, vector_count=0, updated_at=? WHERE id=?",
                                (keep, now_str(), did),
                            )
                            conn.execute(
                                "INSERT OR REPLACE INTO aliases(content_hash, path, canonical_doc_id) "
                                "VALUES(?,?,?)", (g["content_hash"], r["path"], keep))
                        removed += 1
                    except Exception as exc:
                        log.warning("去重失败 doc=%s: %s", did, exc)
        if removed:
            log.info("去重完成：折叠 %d 个重复文档", removed)
        return removed

    def prune_missing(self, force: bool = False) -> int:
        if self.cfg.keep_deleted and not force:
            return 0
        conn = self.conn()
        rows = conn.execute("SELECT id, path FROM documents").fetchall()
        removed = 0
        for row in rows:
            if not Path(row["path"]).exists():
                try:
                    self.delete_path(Path(row["path"]))
                    removed += 1
                except Exception:
                    pass
        return removed

    # ---------- jobs ----------
    def _job_conn(self):
        return db.connect(self.cfg)

    def process_jobs(self, kind: str, limit: int | None = None) -> int:
        cfg = self.cfg
        conn = self._job_conn()
        processed = 0
        try:
            with self.write_lock:
                conn.execute(
                    "UPDATE jobs SET state='pending' WHERE state='running' AND "
                    "updated_at < datetime('now','-2 hours')")
                conn.commit()
            while True:
                remaining = None if limit is None else limit - processed
                if remaining is not None and remaining <= 0:
                    break
                batch = 10 if remaining is None else min(10, remaining)
                with self.write_lock:
                    jobs = conn.execute(
                        "SELECT * FROM jobs WHERE kind=? AND state='pending' "
                        "ORDER BY priority, id LIMIT ?", (kind, batch),
                    ).fetchall()
                    if not jobs:
                        break
                    for job in jobs:
                        conn.execute(
                            "UPDATE jobs SET state='running', attempts=attempts+1, "
                            "updated_at=CURRENT_TIMESTAMP WHERE id=?", (job["id"],))
                    conn.commit()

                for job in jobs:
                    try:
                        if kind == "ocr":
                            self._run_ocr_job(conn, job)
                        elif kind == "transcribe":
                            self._run_transcribe_job(conn, job)
                        with self.write_lock:
                            conn.execute(
                                "UPDATE jobs SET state='done', error=NULL, updated_at=CURRENT_TIMESTAMP "
                                "WHERE id=?", (job["id"],))
                            conn.commit()
                    except Exception as exc:
                        log.warning("任务失败 %s #%s: %s", kind, job["id"], exc)
                        with self.write_lock:
                            attempts = int(job["attempts"] or 0) + 1
                            state = "pending" if attempts < 2 else "failed"
                            conn.execute(
                                "UPDATE jobs SET state=?, error=?, updated_at=CURRENT_TIMESTAMP "
                                "WHERE id=?", (state, str(exc)[:1000], job["id"]))
                            if kind == "ocr" and job["doc_id"]:
                                conn.execute(
                                    "UPDATE documents SET ocr_status='error' WHERE id=?",
                                    (job["doc_id"],))
                            if kind == "transcribe" and job["doc_id"]:
                                conn.execute(
                                    "UPDATE documents SET transcript_status='error' WHERE id=?",
                                    (job["doc_id"],))
                            conn.commit()
                    processed += 1
                    if limit and processed >= limit:
                        return processed
        finally:
            conn.close()
        return processed

    def _ocr_pdf_child(self, path: Path, pages: list[int]) -> list[tuple[int, str, float]]:
        import subprocess
        import sys
        import tempfile
        worker = Path(__file__).resolve().parent / "ocr_worker.py"
        out_dir = self.cfg.cache_dir / "ocrjson"
        out_dir.mkdir(parents=True, exist_ok=True)
        fd, out_name = tempfile.mkstemp(suffix=".json", dir=str(out_dir))
        os.close(fd)
        out = Path(out_name)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.cfg.home)
        env["PYTHONIOENCODING"] = "utf-8"
        err = out.with_suffix(".err.txt")
        with open(err, "wb") as ef:
            proc = subprocess.Popen(
                [sys.executable, str(worker), str(path), ",".join(str(p) for p in pages), str(out)],
                cwd=str(self.cfg.home), env=env, stdout=subprocess.DEVNULL, stderr=ef,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                proc.wait(timeout=900)
                if proc.returncode != 0:
                    raise RuntimeError("PDF OCR 子进程失败: " + err.read_text(encoding="utf-8", errors="ignore")[-500:])
                data = json.loads(out.read_text(encoding="utf-8"))
                return [(int(a), str(b), float(c)) for a, b, c in data]
            finally:
                if proc.poll() is None:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for f in (out, err):
                    try:
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass

    def _run_ocr_job(self, conn, job) -> None:
        from .ocr import ocr_image, ocr_pdf_pages
        from . import bg_cpu

        lock_path = self.cfg.data_dir / "ocr_job.lock"
        deadline = time.time() + 3600
        got = False
        while time.time() < deadline:
            try:
                fd = lock_path.open("x")
                fd.close()
                got = True
                break
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 1800:
                        lock_path.unlink()
                        continue
                except Exception:
                    pass
                time.sleep(2)
        try:
            self._run_ocr_job_inner(conn, job, ocr_image, ocr_pdf_pages, bg_cpu)
        finally:
            if got:
                try:
                    lock_path.unlink()
                except Exception:
                    pass

    def _run_ocr_job_inner(self, conn, job, ocr_image, ocr_pdf_pages, bg_cpu) -> None:
        bg_cpu.wait_if_over(self.cfg)
        doc_id = int(job["doc_id"])
        row = conn.execute("SELECT path, ext, meta_json FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        path = Path(row["path"])
        payload = json.loads(job["payload"] or "{}")
        ext = row["ext"].lower()
        results: list[tuple[int, str, float]] = []
        if payload.get("type") == "image" or ext in IMAGE_EXTS:
            text, conf = ocr_image(path)
            if text.strip():
                results = [(0, text, conf)]
                seq_base = 1_500_000
        else:
            try:
                meta = json.loads(row["meta_json"] or "{}")
            except Exception:
                meta = {}
            ocr_path = Path(meta.get("converted_path") or path)
            pages = [int(p) for p in payload.get("pages", [])]
            results = self._ocr_pdf_child(ocr_path, pages)
            seq_base = 1_000_000

        with self.write_lock:
            with db.tx(conn):
                pages = {p for p, _, _ in results}
                if pages:
                    placeholders = ",".join("?" for _ in pages)
                    old = conn.execute(
                        f"SELECT id FROM chunks WHERE doc_id=? AND kind='ocr' AND page_start IN ({placeholders})",
                        (doc_id, *pages),
                    ).fetchall()
                    for r in old:
                        conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (r["id"],))
                    conn.execute(
                        f"DELETE FROM chunks WHERE doc_id=? AND kind='ocr' AND page_start IN ({placeholders})",
                        (doc_id, *pages),
                    )
                for page, text, _conf in results:
                    if page == 0:
                        seq = seq_base + 1
                    else:
                        seq = seq_base + page * 1000
                    # 避免与同页已有 OCR 分块冲突
                    while conn.execute("SELECT 1 FROM chunks WHERE doc_id=? AND seq=?",
                                       (doc_id, seq)).fetchone():
                        seq += 1
                    self._add_chunk(conn, doc_id, seq, text, "ocr", f"OCR 第{page}页" if page else "OCR 图片",
                                    page_start=page or None, page_end=page or None)
                total = conn.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(char_count),0) c FROM chunks WHERE doc_id=?",
                    (doc_id,)).fetchone()
                conn.execute(
                    "UPDATE documents SET ocr_status='done', chunk_count=?, char_count=?, "
                    "vector_status=CASE WHEN ? THEN 'partial' ELSE vector_status END WHERE id=?",
                    (int(total["n"]), int(total["c"]), 1 if self.cfg.embedding.enabled else 0, doc_id),
                )

    def _run_transcribe_job(self, conn, job) -> None:
        from .parsers.media import transcribe_media

        doc_id = int(job["doc_id"])
        row = conn.execute("SELECT path FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        path = Path(row["path"])
        segments = transcribe_media(path, self.cfg)
        with self.write_lock:
            with db.tx(conn):
                old = conn.execute(
                    "SELECT id FROM chunks WHERE doc_id=? AND kind='transcript'", (doc_id,)).fetchall()
                for r in old:
                    conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (r["id"],))
                conn.execute("DELETE FROM chunks WHERE doc_id=? AND kind='transcript'", (doc_id,))
                for i, (text, start_ms, end_ms) in enumerate(segments):
                    self._add_chunk(conn, doc_id, 2_000_000 + i, text, "transcript",
                                    "语音转写", start_ms=start_ms, end_ms=end_ms)
                total = conn.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(char_count),0) c FROM chunks WHERE doc_id=?",
                    (doc_id,)).fetchone()
                conn.execute(
                    "UPDATE documents SET transcript_status='done', chunk_count=?, char_count=?, "
                    "vector_status=CASE WHEN ? THEN 'partial' ELSE vector_status END WHERE id=?",
                    (int(total["n"]), int(total["c"]), 1 if self.cfg.embedding.enabled else 0, doc_id),
                )

    # ---------- embeddings ----------
    def embed_pending(self, limit: int | None = None, shard: int = 0,
                      num_shards: int = 1) -> int:
        from .embedding import Embedder

        cfg = self.cfg
        if not cfg.embedding.enabled:
            return 0
        conn = self.conn()
        total = 0
        embedder = None
        while True:
            take = cfg.embedding.batch_size
            if limit:
                take = max(1, min(take, limit - total))
            shard_sql = ""
            shard_params = []
            if num_shards and num_shards > 1:
                shard_sql = " AND (c.id % ?) = ?"
                shard_params = [int(num_shards), int(shard) % int(num_shards)]
            rows = conn.execute(
                "SELECT c.id, c.doc_id, c.text, c.title_path, c.kind, d.collection, d.year, "
                "d.doc_type, d.ext FROM chunks c JOIN documents d ON d.id=c.doc_id "
                "WHERE c.embedded=0 AND d.status='ok'" + shard_sql +
                " ORDER BY c.id LIMIT ?",
                (*shard_params, take),
            ).fetchall()
            if not rows:
                break
            if embedder is None:
                embedder = Embedder(cfg)
            texts = [((r["title_path"] or "") + "\n" + r["text"]) for r in rows]
            vectors = embedder.embed(texts)
            records = []
            for r, vec in zip(rows, vectors):
                records.append({
                    "chunk_id": int(r["id"]),
                    "doc_id": int(r["doc_id"]),
                    "vector": vec,
                    "collection": r["collection"] or "",
                    "year": int(r["year"] or 0),
                    "doc_type": r["doc_type"] or "",
                    "ext": r["ext"] or "",
                    "kind": r["kind"] or "",
                })
            vectorstore.upsert(cfg, records)
            with self.write_lock:
                with db.tx(conn):
                    for r in rows:
                        conn.execute("UPDATE chunks SET embedded=1 WHERE id=?", (r["id"],))
                    doc_ids = {int(r["doc_id"]) for r in rows}
                    for did in doc_ids:
                        conn.execute(
                            "UPDATE documents SET vector_count=(SELECT COUNT(*) FROM chunks "
                            "WHERE doc_id=? AND embedded=1), vector_status=CASE WHEN "
                            "(SELECT COUNT(*) FROM chunks WHERE doc_id=? AND embedded=1) >= "
                            "(SELECT COUNT(*) FROM chunks WHERE doc_id=?) THEN 'done' ELSE 'partial' END "
                            "WHERE id=?", (did, did, did, did),
                        )
            total += len(rows)
            if total % (cfg.embedding.batch_size * 10) == 0:
                log.info("向量化进度：%d 块", total)
            if limit and total >= limit:
                break
        return total

    def embed_all(self) -> int:
        return self.embed_pending(None)

    # ---------- stats ----------
    def status(self) -> dict:
        conn = self.conn()
        s = db.stats(conn)
        s["dbsize_mb"] = round(self.cfg.db_path.stat().st_size / 1024 / 1024, 1) if self.cfg.db_path.exists() else 0
        s["vectors"] = vectorstore.count(self.cfg)
        return s
