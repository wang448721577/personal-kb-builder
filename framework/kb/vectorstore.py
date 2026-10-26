from __future__ import annotations

import threading
from typing import Any

from .config import Config
from .db import build_filters

_lock = threading.Lock()
_db = None
_table = None


def _open(cfg: Config):
    global _db, _table
    if _db is None:
        import lancedb
        cfg.lance_dir.mkdir(parents=True, exist_ok=True)
        _db = lancedb.connect(str(cfg.lance_dir))
    names = []
    try:
        names = _db.table_names()
    except Exception:
        try:
            names = _db.list_tables()
        except Exception:
            names = []
    if "chunks" not in names:
        import pyarrow as pa
        schema = pa.schema([
            pa.field("chunk_id", pa.int64()),
            pa.field("doc_id", pa.int64()),
            pa.field("vector", pa.list_(pa.float32(), cfg.embedding.dim)),
            pa.field("collection", pa.string()),
            pa.field("year", pa.int32()),
            pa.field("doc_type", pa.string()),
            pa.field("ext", pa.string()),
            pa.field("kind", pa.string()),
        ])
        _table = _db.create_table("chunks", schema=schema)
    else:
        _table = _db.open_table("chunks")
    return _table


def get_table(cfg: Config):
    global _table
    with _lock:
        if _table is None:
            _open(cfg)
        return _table


def upsert(cfg: Config, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    import pyarrow as pa
    table = get_table(cfg)
    schema = table.schema
    vector_field = schema.field("vector")
    dim = vector_field.type.list_size
    for rec in records:
        vec = rec.get("vector") or []
        if len(vec) != dim:
            raise ValueError(f"向量维度不匹配: {len(vec)} != {dim}")
    tbl = pa.Table.from_pylist(records, schema=schema)
    with _lock:
        table.merge_insert("chunk_id").when_matched_update_all().when_not_matched_insert_all().execute(tbl)


def delete_doc(cfg: Config, doc_id: int) -> None:
    try:
        table = get_table(cfg)
        table.delete(f"doc_id = {int(doc_id)}")
    except Exception:
        pass


def search(cfg: Config, vector: list[float], limit: int,
           collection: str | None = None, year: int | None = None,
           doc_type: str | None = None) -> list[dict]:
    try:
        table = get_table(cfg)
        q = table.search(vector, vector_column_name="vector").limit(max(1, limit))
        clauses = []
        if collection:
            clauses.append("collection = '" + collection.replace("'", "''") + "'")
        if year:
            clauses.append(f"year = {int(year)}")
        if doc_type:
            clauses.append("doc_type = '" + doc_type.replace("'", "''") + "'")
        if clauses:
            q = q.where(" AND ".join(clauses))
        return q.to_list()
    except Exception:
        return []


def count(cfg: Config) -> int:
    try:
        return int(get_table(cfg).count_rows())
    except Exception:
        return 0
