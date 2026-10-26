from __future__ import annotations

import threading
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import load_config
from .search import Searcher

cfg = load_config()
app = FastAPI(
    title="Personal Knowledge Base API",
    description="本地个人知识库统一 HTTP 接口：文件、微信聊天、屏幕记忆。",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_lock = threading.RLock()
_searcher: Optional[Searcher] = None


def get_searcher() -> Searcher:
    global _searcher
    with _lock:
        if _searcher is None:
            _searcher = Searcher(cfg)
        return _searcher


def require_token(x_kb_token: str | None = Header(default=None, alias="X-KB-Token"),
                  authorization: str | None = Header(default=None)):
    token = cfg.server.api_token
    if not token:
        return
    supplied = x_kb_token or ""
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if supplied != token:
        raise HTTPException(status_code=401, detail="invalid X-KB-Token")


class TableQuery(BaseModel):
    doc_id: int
    sql: Optional[str] = None
    sheet: Optional[str] = None
    limit: int = 100


@app.get("/health")
def health():
    return {"status": "ok", "database": str(cfg.db_path)}


@app.get("/stats", dependencies=[Depends(require_token)])
def stats():
    with _lock:
        return get_searcher().stats()


@app.get("/search", dependencies=[Depends(require_token)])
def search(q: str = Query(..., min_length=1), top_k: int = 8,
           collection: str = "", year: int = 0, doc_type: str = "",
           path_prefix: str = "", mode: str = "hybrid"):
    with _lock:
        return get_searcher().search(
            q, top_k=top_k, collection=collection or None, year=year or None,
            doc_type=doc_type or None, path_prefix=path_prefix or None,
            mode=mode or "hybrid", source="http",
        )


@app.get("/read", dependencies=[Depends(require_token)])
def read(doc_id: int = 0, path: str = "", pages: str = "", max_chars: int = 30000):
    page_list = None
    if pages:
        page_list = [int(x) for x in pages.split(",") if x.strip()]
    with _lock:
        return get_searcher().read_document(doc_id=doc_id or None, path=path or None,
                                            pages=page_list, max_chars=max_chars)


@app.get("/timeline", dependencies=[Depends(require_token)])
def timeline(start: str, end: str, source: str = "screen", limit: int = 50):
    with _lock:
        return get_searcher().timeline(start, end, source=source, limit=limit)


@app.get("/evidence", dependencies=[Depends(require_token)])
def evidence(q: str, max_docs: int = 20, collection: str = "", year: int = 0):
    with _lock:
        return get_searcher().build_evidence_pack(
            q, max_docs=max_docs, collection=collection or None, year=year or None)


@app.post("/table", dependencies=[Depends(require_token)])
def table(body: TableQuery):
    with _lock:
        return get_searcher().query_table(body.doc_id, sql=body.sql or None,
                                          sheet=body.sheet or None, limit=body.limit)


def run(host: str | None = None, port: int | None = None) -> None:
    import uvicorn
    uvicorn.run(app, host=host or cfg.server.host, port=port or cfg.server.api_port,
                log_level="info")
