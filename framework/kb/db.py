from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .config import Config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    rel_path TEXT NOT NULL,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    quick_hash TEXT,
    content_hash TEXT,
    collection TEXT,
    department TEXT,
    year INTEGER,
    doc_type TEXT,
    title TEXT,
    author TEXT,
    page_count INTEGER DEFAULT 0,
    char_count INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    parser TEXT,
    error TEXT,
    duplicate_of INTEGER,
    meta_json TEXT,
    ocr_status TEXT DEFAULT 'none',
    transcript_status TEXT DEFAULT 'none',
    vector_status TEXT DEFAULT 'none',
    vector_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_quick ON documents(size, quick_hash);
CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection, year);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_dup ON documents(duplicate_of);

CREATE TABLE IF NOT EXISTS aliases (
    content_hash TEXT NOT NULL,
    path TEXT NOT NULL,
    canonical_doc_id INTEGER,
    PRIMARY KEY (content_hash, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    title_path TEXT,
    page_start INTEGER,
    page_end INTEGER,
    sheet TEXT,
    slide INTEGER,
    start_ms INTEGER,
    end_ms INTEGER,
    text TEXT NOT NULL,
    text_hash TEXT,
    token_count INTEGER DEFAULT 0,
    char_count INTEGER DEFAULT 0,
    embedded INTEGER DEFAULT 0,
    UNIQUE(doc_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(text_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_embedded ON chunks(embedded);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_emb ON chunks(doc_id, embedded);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text_tok,
    chunk_id UNINDEXED,
    doc_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    doc_id INTEGER,
    page INTEGER DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER DEFAULT 100,
    attempts INTEGER DEFAULT 0,
    error TEXT,
    payload TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kind, doc_id, page)
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(kind, state, priority);

CREATE TABLE IF NOT EXISTS doc_tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    sheet TEXT,
    path TEXT NOT NULL,
    format TEXT NOT NULL,
    rows INTEGER DEFAULT 0,
    cols INTEGER DEFAULT 0,
    columns_json TEXT,
    UNIQUE(doc_id, sheet)
);
CREATE INDEX IF NOT EXISTS idx_tables_doc ON doc_tables(doc_id);

CREATE TABLE IF NOT EXISTS index_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    mode TEXT,
    files_seen INTEGER DEFAULT 0,
    files_indexed INTEGER DEFAULT 0,
    files_skipped INTEGER DEFAULT 0,
    files_failed INTEGER DEFAULT 0,
    chunks_added INTEGER DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS query_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    query TEXT,
    mode TEXT,
    hits INTEGER,
    elapsed_ms INTEGER,
    source TEXT
);
"""


def connect(cfg: Config, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(str(cfg.db_path), timeout=30, check_same_thread=False)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(str(cfg.db_path), timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-200000")
        conn.execute("PRAGMA mmap_size=1073741824")
    return conn


def init_db(cfg: Config) -> None:
    cfg.ensure_dirs()
    with connect(cfg) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def count_chunks(conn: sqlite3.Connection, doc_id: int) -> int:
    row = conn.execute("SELECT COUNT(*) n FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()
    return int(row["n"])


def list_chunk_ids(conn: sqlite3.Connection, doc_id: int) -> list[int]:
    return [int(r["id"]) for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]


def delete_document_rows(conn: sqlite3.Connection, doc_id: int) -> None:
    ids = list_chunk_ids(conn, doc_id)
    for cid in ids:
        conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM doc_tables WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM jobs WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))


def build_filters(
    collection: str | None = None,
    year: int | None = None,
    doc_type: str | None = None,
    ext: str | None = None,
    path_prefix: str | None = None,
    doc_id: int | None = None,
) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if collection:
        clauses.append("d.collection = ?")
        params.append(collection)
    if year:
        clauses.append("d.year = ?")
        params.append(int(year))
    if doc_type:
        clauses.append("d.doc_type = ?")
        params.append(doc_type)
    if ext:
        e = ext if ext.startswith(".") else "." + ext
        clauses.append("d.ext = ?")
        params.append(e.lower())
    if path_prefix:
        clauses.append("d.rel_path LIKE ?")
        params.append(path_prefix.replace("%", "").replace("_", "\\_") + "%")
    if doc_id:
        clauses.append("d.id = ?")
        params.append(int(doc_id))
    sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def search_fts(
    conn: sqlite3.Connection,
    match_query: str,
    limit: int,
    filters_sql: str = "",
    filters_params: Iterable = (),
) -> list[sqlite3.Row]:
    sql = f"""
        SELECT c.id AS chunk_id, c.doc_id, c.seq, c.kind, c.title_path,
               c.page_start, c.page_end, c.sheet, c.slide, c.start_ms, c.end_ms,
               c.text, c.char_count,
               d.path, d.rel_path, d.name, d.title, d.ext, d.collection,
               d.department, d.year, d.doc_type, d.page_count,
               bm25(chunks_fts) AS bm25
        FROM chunks_fts f
        JOIN chunks c ON c.id = f.rowid
        JOIN documents d ON d.id = c.doc_id
        WHERE chunks_fts MATCH ? AND d.status = 'ok' {filters_sql}
        ORDER BY bm25
        LIMIT ?
    """
    try:
        return list(conn.execute(sql, (match_query, *filters_params, limit)))
    except sqlite3.OperationalError:
        return []


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql: str, *args):
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else 0

    by_status = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM documents GROUP BY status")}
    by_type = {r["doc_type"]: r["n"] for r in conn.execute(
        "SELECT doc_type, COUNT(*) n FROM documents WHERE status='ok' GROUP BY doc_type ORDER BY n DESC")}
    by_collection = {r["collection"]: r["n"] for r in conn.execute(
        "SELECT collection, COUNT(*) n FROM documents WHERE status='ok' GROUP BY collection ORDER BY n DESC")}
    return {
        "documents": one("SELECT COUNT(*) FROM documents"),
        "documents_ok": one("SELECT COUNT(*) FROM documents WHERE status='ok'"),
        "chunks": one("SELECT COUNT(*) FROM chunks"),
        "chunks_embedded": one("SELECT COUNT(*) FROM chunks WHERE embedded=1"),
        "chunks_pending_embed": one("SELECT COUNT(*) FROM chunks WHERE embedded=0"),
        "fts_rows": one("SELECT COUNT(*) FROM chunks_fts"),
        "jobs_pending": one("SELECT COUNT(*) FROM jobs WHERE state='pending'"),
        "jobs_running": one("SELECT COUNT(*) FROM jobs WHERE state='running'"),
        "jobs_failed": one("SELECT COUNT(*) FROM jobs WHERE state='failed'"),
        "by_status": by_status,
        "by_type": by_type,
        "by_collection": by_collection,
        "last_index_run": dict(conn.execute(
            "SELECT * FROM index_runs ORDER BY id DESC LIMIT 1").fetchone() or {}),
    }
