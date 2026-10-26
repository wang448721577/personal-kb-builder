from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import db, vectorstore
from .config import Config
from .textutil import build_fts_queries, format_ts, make_snippet, normalize_text
from .utils import now_str


def _locator(row: dict) -> str:
    if row.get("start_ms") is not None:
        return f" [{format_ts(row.get('start_ms'))}]"
    if row.get("slide"):
        return f" 第{row['slide']}页幻灯片"
    if row.get("sheet"):
        return f" 工作表「{row['sheet']}」"
    if row.get("page_start"):
        p1 = row.get("page_start")
        p2 = row.get("page_end") or p1
        return f" 第{p1}页" if p1 == p2 else f" 第{p1}-{p2}页"
    return ""


def _citation(row: dict) -> str:
    title = row.get("title") or row.get("name") or "(无标题)"
    return f"《{title}》" + _locator(row)


class Searcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = db.connect(cfg, readonly=True)
        self._embedder = None

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _embedder_obj(self):
        if self._embedder is None:
            from .embedding import Embedder
            self._embedder = Embedder(self.cfg)
        return self._embedder

    def _fts(self, query: str, filters_sql: str, params: list, limit: int):
        and_q, or_q, terms = build_fts_queries(query)
        if not and_q:
            return [], terms
        rows = db.search_fts(self.conn, and_q, limit, filters_sql, params)
        if len(rows) < min(5, limit) and or_q and or_q != and_q:
            rows2 = db.search_fts(self.conn, or_q, limit, filters_sql, params)
            seen = {r["chunk_id"] for r in rows}
            for r in rows2:
                if r["chunk_id"] not in seen:
                    rows.append(r)
                    seen.add(r["chunk_id"])
        if not rows:
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            sql = f"""
                SELECT c.id AS chunk_id, c.doc_id, c.seq, c.kind, c.title_path,
                       c.page_start, c.page_end, c.sheet, c.slide, c.start_ms, c.end_ms,
                       c.text, c.char_count,
                       d.path, d.rel_path, d.name, d.title, d.ext, d.collection,
                       d.department, d.year, d.doc_type, d.page_count,
                       1000.0 AS bm25
                FROM chunks c JOIN documents d ON d.id=c.doc_id
                WHERE d.status='ok' AND c.text LIKE ? ESCAPE '\\' {filters_sql}
                LIMIT ?
            """
            rows = list(self.conn.execute(sql, (like, *params, limit)))
        return rows, terms

    def _fetch_rows(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""SELECT c.id AS chunk_id, c.doc_id, c.seq, c.kind, c.title_path,
                       c.page_start, c.page_end, c.sheet, c.slide, c.start_ms, c.end_ms,
                       c.text, c.char_count,
                       d.path, d.rel_path, d.name, d.title, d.ext, d.collection,
                       d.department, d.year, d.doc_type, d.page_count
                FROM chunks c JOIN documents d ON d.id=c.doc_id
                WHERE c.id IN ({ph})""",
            ids,
        ).fetchall()
        return {int(r["chunk_id"]): dict(r) for r in rows}

    def search(self, query: str, top_k: int | None = None,
               collection: str | None = None, year: int | None = None,
               doc_type: str | None = None, ext: str | None = None,
               path_prefix: str | None = None, mode: str = "hybrid",
               include_meta: bool = False, source: str = "mcp") -> dict:
        cfg = self.cfg
        started = time.time()
        query = (query or "").strip()
        top_k = int(top_k or cfg.search.top_k)
        top_k = max(1, min(top_k, 50))
        candidate_k = max(cfg.search.candidate_k, top_k * 3)
        filters_sql, params = db.build_filters(
            collection=collection, year=year, doc_type=doc_type, ext=ext,
            path_prefix=path_prefix)
        if not include_meta:
            filters_sql += " AND c.kind <> 'meta'"

        fts_rows: list = []
        terms: list[str] = []
        vec_rows: list = []
        if mode in ("hybrid", "fts", "keyword"):
            fts_rows, terms = self._fts(query, filters_sql, params, candidate_k)
        if mode in ("hybrid", "vector", "semantic") and cfg.embedding.enabled \
                and vectorstore.count(cfg) > 0:
            try:
                qvec = self._embedder_obj().embed_query(query)
                vec_rows = vectorstore.search(cfg, qvec, candidate_k,
                                              collection=collection, year=year,
                                              doc_type=doc_type)
            except Exception:
                vec_rows = []
        if not terms:
            _a, _o, terms = build_fts_queries(query)

        cand: dict[int, dict] = {}
        for rank, row in enumerate(fts_rows, 1):
            cid = int(row["chunk_id"])
            item = cand.setdefault(cid, {"chunk_id": cid, "fts_rank": None,
                                         "vec_rank": None, "bm25": None})
            if item["fts_rank"] is None:
                item["fts_rank"] = rank
                item["bm25"] = float(row["bm25"])
        for rank, row in enumerate(vec_rows, 1):
            cid = int(row["chunk_id"])
            item = cand.setdefault(cid, {"chunk_id": cid, "fts_rank": None,
                                         "vec_rank": None, "bm25": None})
            if item["vec_rank"] is None:
                item["vec_rank"] = rank

        k = cfg.search.rrf_k
        for item in cand.values():
            score = 0.0
            if item["fts_rank"]:
                score += 1.0 / (k + item["fts_rank"])
            if item["vec_rank"]:
                score += 1.0 / (k + item["vec_rank"])
            item["score"] = score
        ids = list(cand.keys())
        row_map = self._fetch_rows(ids)
        for cid, item in cand.items():
            row = row_map.get(cid)
            if row:
                item["row"] = row
                if row.get("kind") == "meta":
                    item["score"] *= 0.35
                text = row.get("text") or ""
                if text.count("·") + text.count("....") > 20:
                    item["score"] *= 0.2
                title_blob = ((row.get("title") or "") + " " + (row.get("title_path") or "")).lower()
                if terms and any(t.lower() in title_blob for t in terms):
                    item["score"] *= 1.25
        ranked = sorted((x for x in cand.values() if x.get("row")),
                        key=lambda x: (-x["score"], x["chunk_id"]))

        per_doc: dict[int, int] = {}
        results = []
        for item in ranked:
            row = item["row"]
            did = int(row["doc_id"])
            if per_doc.get(did, 0) >= cfg.search.max_chunks_per_doc:
                continue
            per_doc[did] = per_doc.get(did, 0) + 1
            quote, snippet = make_snippet(row["text"], terms)
            res = {
                "chunk_id": int(row["chunk_id"]),
                "doc_id": did,
                "title": row.get("title") or row.get("name"),
                "citation": _citation(row),
                "snippet": snippet,
                "quote": quote,
                "path": row.get("path"),
                "rel_path": row.get("rel_path"),
                "collection": row.get("collection"),
                "year": row.get("year"),
                "doc_type": row.get("doc_type"),
                "ext": row.get("ext"),
                "page": row.get("page_start"),
                "page_end": row.get("page_end"),
                "sheet": row.get("sheet"),
                "slide": row.get("slide"),
                "start_ms": row.get("start_ms"),
                "end_ms": row.get("end_ms"),
                "title_path": row.get("title_path"),
                "kind": row.get("kind"),
                "score": round(float(item["score"]), 6),
                "fts_rank": item["fts_rank"],
                "vector_rank": item["vec_rank"],
            }
            results.append(res)
            if len(results) >= top_k:
                break

        elapsed = int((time.time() - started) * 1000)
        return {
            "query": query,
            "mode": mode,
            "hits": len(results),
            "elapsed_ms": elapsed,
            "results": results,
            "filters": {k: v for k, v in {
                "collection": collection, "year": year, "doc_type": doc_type,
                "ext": ext, "path_prefix": path_prefix}.items() if v},
            "note": None if results else "未找到匹配内容，可尝试更短的关键词或去掉筛选条件。",
        }

    def get_context(self, chunk_id: int, before: int = 1, after: int = 1) -> dict:
        base = self.conn.execute(
            """SELECT c.*, d.path, d.rel_path, d.name, d.title, d.collection, d.year,
                      d.doc_type FROM chunks c JOIN documents d ON d.id=c.doc_id
               WHERE c.id=?""", (chunk_id,)).fetchone()
        if not base:
            return {"error": "chunk 不存在"}
        rows = self.conn.execute(
            """SELECT c.*, d.path, d.rel_path, d.name, d.title, d.collection, d.year,
                      d.doc_type FROM chunks c JOIN documents d ON d.id=c.doc_id
               WHERE c.doc_id=? AND c.seq BETWEEN ? AND ? ORDER BY c.seq""",
            (base["doc_id"], max(0, base["seq"] - before), base["seq"] + after),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            items.append({
                "chunk_id": int(d["id"]), "seq": d["seq"], "kind": d["kind"],
                "citation": _citation(d), "text": d["text"],
                "title_path": d.get("title_path"), "page": d.get("page_start"),
                "sheet": d.get("sheet"), "slide": d.get("slide"),
                "start_ms": d.get("start_ms"), "end_ms": d.get("end_ms"),
            })
        return {"chunk_id": chunk_id, "doc_id": int(base["doc_id"]),
                "title": base["title"] or base["name"], "path": base["path"],
                "rel_path": base["rel_path"], "items": items}

    def read_document(self, doc_id: int | None = None, path: str | None = None,
                      pages: list[int] | None = None, max_chars: int = 30000) -> dict:
        if doc_id is None and path:
            row = self.conn.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
            if not row:
                row = self.conn.execute(
                    "SELECT id FROM documents WHERE rel_path=? OR name=? LIMIT 1",
                    (path, Path(path).name)).fetchone()
            if not row:
                return {"error": "文档不存在"}
            doc_id = int(row["id"])
        doc = self.conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not doc:
            return {"error": "文档不存在"}
        chunks = self.conn.execute(
            "SELECT * FROM chunks WHERE doc_id=? ORDER BY COALESCE(page_start,0), "
            "COALESCE(slide,0), seq", (doc_id,)).fetchall()
        parts = []
        last_key = None
        truncated = False
        total = 0
        for c in chunks:
            if pages:
                p = c["page_start"] or 0
                if p not in pages:
                    continue
            key = (c["page_start"], c["sheet"], c["slide"])
            if key != last_key:
                marker = ""
                if c["slide"]:
                    marker = f"\n\n【第{c['slide']}页幻灯片】\n"
                elif c["sheet"]:
                    marker = f"\n\n【工作表：{c['sheet']}】\n"
                elif c["page_start"]:
                    marker = f"\n\n【第{c['page_start']}页】\n"
                elif c["start_ms"] is not None:
                    marker = f"\n\n【{format_ts(c['start_ms'])}】\n"
                if marker:
                    parts.append(marker)
                last_key = key
            text = c["text"]
            if total + len(text) > max_chars:
                parts.append(text[: max(0, max_chars - total)])
                truncated = True
                break
            parts.append(text)
            total += len(text)
        title = doc["title"] or doc["name"]
        return {
            "doc_id": int(doc["id"]), "title": title, "path": doc["path"],
            "rel_path": doc["rel_path"], "collection": doc["collection"],
            "year": doc["year"], "doc_type": doc["doc_type"],
            "page_count": doc["page_count"], "char_count": doc["char_count"],
            "truncated": truncated, "pages_filter": pages,
            "text": "".join(parts).strip(),
            "note": "引用时必须标注页码/工作表/幻灯片，并给出文件路径。",
        }

    def list_documents(self, query: str | None = None, collection: str | None = None,
                       year: int | None = None, doc_type: str | None = None,
                       ext: str | None = None, limit: int = 50,
                       offset: int = 0) -> dict:
        filters_sql, params = db.build_filters(
            collection=collection, year=year, doc_type=doc_type, ext=ext)
        where = "d.status='ok'" + filters_sql
        if query:
            where += " AND (d.name LIKE ? OR d.title LIKE ? OR d.rel_path LIKE ?)"
            q = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([q, q, q])
        rows = self.conn.execute(
            f"""SELECT d.id, d.name, d.title, d.rel_path, d.collection, d.year,
                       d.doc_type, d.ext, d.page_count, d.chunk_count, d.size, d.mtime_ns
                FROM documents d WHERE {where}
                ORDER BY d.year DESC, d.mtime_ns DESC LIMIT ? OFFSET ?""",
            (*params, max(1, min(limit, 200)), max(0, offset)),
        ).fetchall()
        return {"hits": len(rows), "documents": [
            {"doc_id": int(r["id"]), "name": r["name"], "title": r["title"],
             "rel_path": r["rel_path"], "collection": r["collection"],
             "year": r["year"], "doc_type": r["doc_type"], "ext": r["ext"],
             "page_count": r["page_count"], "chunk_count": r["chunk_count"],
             "size": r["size"]}
            for r in rows]}

    def query_table(self, doc_id: int, sql: str | None = None,
                    sheet: str | None = None, limit: int = 100) -> dict:
        import duckdb

        tables = self.conn.execute(
            "SELECT * FROM doc_tables WHERE doc_id=? ORDER BY id", (doc_id,)).fetchall()
        if not tables:
            return {"error": "该文档没有可查询的表格数据（仅支持 Excel/CSV）"}
        doc = self.conn.execute("SELECT name, title FROM documents WHERE id=?", (doc_id,)).fetchone()
        limit = max(1, min(int(limit), 1000))
        con = duckdb.connect()
        try:
            views = {}
            for i, t in enumerate(tables):
                p = str(t["path"]).replace("'", "''")
                view = f"t{i}"
                con.execute(f"CREATE VIEW {view} AS SELECT * FROM read_csv_auto('{p}', header=true, sample_size=20000)")
                views[view] = {"sheet": t["sheet"], "rows": t["rows"], "cols": t["cols"],
                               "columns": json.loads(t["columns_json"] or "[]")}
            chosen = "t0"
            if sheet:
                for v, meta in views.items():
                    if meta["sheet"] == sheet:
                        chosen = v
                        break
            elif len(views) == 1:
                chosen = next(iter(views))
            con.execute(f"CREATE VIEW t AS SELECT * FROM {chosen}")

            if not sql:
                cols = [r[0] for r in con.execute(f"DESCRIBE {chosen}").fetchall()]
                rows = con.execute(f"SELECT * FROM {chosen} LIMIT {limit}").fetchall()
                return {"doc_id": doc_id, "title": doc["title"] or doc["name"],
                        "sheets": [{"view": v, **m} for v, m in views.items()],
                        "current_view": chosen, "columns": cols,
                        "rows": [[_jsonable(x) for x in row] for row in rows],
                        "row_count": len(rows), "limit": limit,
                        "note": "可用视图 t（当前表）及 t0/t1…；SQL 仅允许 SELECT/WITH。"}
            bad = re.search(r"\b(attach|detach|copy|install|load|export|import|create|insert|update|delete|drop|alter|pragma|call|set|begin|commit|rollback|read_csv|read_parquet|read_json|read_json_auto|glob|getenv)\b",
                            sql, re.I)
            if bad or not re.match(r"^\s*(select|with)\b", sql, re.I) or ";" in sql.strip().rstrip(";"):
                return {"error": "只允许单条 SELECT/WITH 查询，且不得包含写操作关键字。"}
            sql2 = sql.replace("{table}", chosen).strip().rstrip(";")
            wrapped = f"SELECT * FROM ({sql2}) AS _q LIMIT {limit}"
            cur = con.execute(wrapped)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return {"doc_id": doc_id, "title": doc["title"] or doc["name"],
                    "columns": cols, "rows": [[_jsonable(x) for x in row] for row in rows],
                    "row_count": len(rows), "limit": limit,
                    "views": [{"view": v, **m} for v, m in views.items()]}
        except Exception as exc:
            return {"error": f"表格查询失败: {exc}"}
        finally:
            con.close()

    def build_evidence_pack(self, question: str, subquestions: list[str] | None = None,
                            max_docs: int = 20, collection: str | None = None,
                            year: int | None = None) -> dict:
        queries = [question]
        if subquestions:
            queries.extend(subquestions)
        try:
            import jieba.analyse
            tags = jieba.analyse.extract_tags(question, topK=12)
            if tags:
                queries.append(" ".join(tags[:8]))
        except Exception:
            pass
        seen_docs: set[int] = set()
        evidence = []
        for q in queries:
            if len(evidence) >= max_docs:
                break
            res = self.search(q, top_k=15, collection=collection, year=year,
                              mode="hybrid", include_meta=False, source="evidence")
            for r in res.get("results", []):
                if r["doc_id"] in seen_docs:
                    continue
                seen_docs.add(r["doc_id"])
                evidence.append({
                    "eid": f"E{len(evidence) + 1:03d}",
                    "doc_id": r["doc_id"], "chunk_id": r["chunk_id"],
                    "title": r["title"], "citation": r["citation"],
                    "quote": r["quote"], "path": r["path"], "rel_path": r["rel_path"],
                    "page": r["page"], "sheet": r["sheet"], "slide": r["slide"],
                    "start_ms": r["start_ms"], "year": r["year"],
                    "doc_type": r["doc_type"], "score": r["score"],
                })
                if len(evidence) >= max_docs:
                    break
        return {
            "question": question,
            "evidence_count": len(evidence),
            "evidence": evidence,
            "rules": [
                "每个结论必须绑定至少一个 EID；正文引用格式：[E001]",
                "引用原文必须逐字来自 quote 字段，不得改写或拼接",
                "报告末尾列出“证据与来源”表：EID | 文件名 | 页码/工作表/时间 | 路径",
                "没有证据支持的判断必须标为“推测”，不得写成事实",
                "涉及统计数字时，优先用 kb_query_table 复核 Excel 原表",
            ],
        }

    def timeline(self, start: str, end: str, source: str = "screen",
                 limit: int = 50) -> dict:
        from datetime import datetime
        def parse(v: str):
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.astimezone()
                return int(dt.timestamp() * 1000)
            except Exception:
                return None
        a, b = parse(start), parse(end)
        if a is None or b is None:
            return {"error": "start/end 需要 ISO 时间，例如 2026-10-25T14:00:00+08:00"}
        prefix = {"screen": "screenpipe://%", "chat": "wechat://%", "all": "%"}.get(source, "screenpipe://%")
        rows = self.conn.execute(
            "SELECT id, name, title, rel_path, path, collection, year, doc_type, "
            "chunk_count, mtime_ns FROM documents WHERE status='ok' AND path LIKE ? "
            "AND mtime_ns BETWEEN ? AND ? ORDER BY mtime_ns LIMIT ?",
            (prefix, a * 1_000_000, b * 1_000_000, max(1, min(limit, 200))),
        ).fetchall()
        return {"source": source, "start": start, "end": end, "hits": len(rows),
                "documents": [
                    {"doc_id": int(r["id"]), "title": r["title"] or r["name"],
                     "path": r["path"], "rel_path": r["rel_path"],
                     "collection": r["collection"], "doc_type": r["doc_type"],
                     "chunk_count": r["chunk_count"],
                     "time": datetime.fromtimestamp(int(r["mtime_ns"]) / 1e9).strftime("%Y-%m-%d %H:%M:%S")}
                    for r in rows]}

    def stats(self) -> dict:
        s = db.stats(self.conn)
        s["vectors"] = vectorstore.count(self.cfg)
        s["db_mb"] = round(self.cfg.db_path.stat().st_size / 1024 / 1024, 1) if self.cfg.db_path.exists() else 0
        return s


def _jsonable(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    try:
        return str(v)
    except Exception:
        return repr(v)
