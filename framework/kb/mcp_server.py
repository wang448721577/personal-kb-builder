from __future__ import annotations

import threading
from typing import Optional

from fastmcp import FastMCP

from .config import load_config
from .search import Searcher

mcp = FastMCP(
    "Personal Knowledge Base",
    instructions=(
        "这是用户电脑 D:\\用户本地资料目录 的本地知识库。"
        "回答与用户本人文件、科研、教学、项目、会议、数据有关的问题时，先调用 kb_search 检索；"
        "需要原文时调用 kb_read_document；需要统计 Excel 数据时调用 kb_query_table；"
        "写报告/总结时先调用 kb_build_evidence_pack 获取证据包。"
        "所有引用必须带 citation（书名号标题 + 页码/工作表/幻灯片/时间）与文件路径，"
        "不得编造未在 quote 中出现的内容。"
    ),
)

_cfg = None
_searcher: Optional[Searcher] = None
_lock = threading.Lock()


def _s() -> Searcher:
    global _cfg, _searcher
    with _lock:
        if _searcher is None:
            _cfg = load_config()
            _searcher = Searcher(_cfg)
        return _searcher


@mcp.tool()
def kb_search(query: str, top_k: int = 8, collection: str = "",
              year: int = 0, doc_type: str = "", path_prefix: str = "",
              mode: str = "hybrid", include_meta: bool = False) -> dict:
    """检索本地知识库，返回带引用和原文片段的证据。

    参数：
    - query：中文/英文检索词，尽量具体（主题 + 年份 + 类型）
    - top_k：返回条数，默认 8，最大 50
    - collection：限定一级目录，例如“计算机学院”“研究生院”
    - year：限定年份，例如 2026；0 表示不限
    - doc_type：限定类型：论文/项目/教学/会议/人事/财务/公文/学生/科研/图片/音视频
    - path_prefix：限定相对路径前缀，例如“计算机学院/张三科研”
    - mode：hybrid（默认）/ fts / vector
    - include_meta：是否包含仅文件名/路径的元数据块
    返回结果中的 quote 是原文精确片段，citation 可直接用于报告引用。
    """
    return _s().search(query, top_k=top_k or 8,
                       collection=collection or None,
                       year=year or None,
                       doc_type=doc_type or None,
                       path_prefix=path_prefix or None,
                       mode=mode or "hybrid", include_meta=include_meta,
                       source="mcp")


@mcp.tool()
def kb_get_context(chunk_id: int, before: int = 1, after: int = 1) -> dict:
    """获取某个检索命中块前后的上下文（同一文档内相邻块），用于核对语义和页码。"""
    return _s().get_context(chunk_id, before=before, after=after)


@mcp.tool()
def kb_read_document(doc_id: int = 0, path: str = "", pages: Optional[list[int]] = None,
                     max_chars: int = 30000) -> dict:
    """读取一篇文档的正文，带页码/工作表/幻灯片标记，便于引用。

    doc_id 与 path 至少提供一个。pages 可指定 PDF 页码列表（从 1 开始）。
    max_chars 控制返回上限，默认 30000，避免撑爆上下文。
    """
    return _s().read_document(doc_id=doc_id or None, path=path or None,
                              pages=pages, max_chars=max_chars)


@mcp.tool()
def kb_list_documents(query: str = "", collection: str = "", year: int = 0,
                      doc_type: str = "", limit: int = 50, offset: int = 0) -> dict:
    """按文件名/标题/路径浏览知识库文档清单，不返回正文。"""
    return _s().list_documents(query=query or None,
                               collection=collection or None,
                               year=year or None,
                               doc_type=doc_type or None,
                               limit=limit, offset=offset)


@mcp.tool()
def kb_query_table(doc_id: int, sql: str = "", sheet: str = "", limit: int = 100) -> dict:
    """对 Excel/CSV 表格做 SQL 数据分析（DuckDB）。

    不传 sql 时返回工作表列表、列名和样例数据；传 sql 时只允许单条 SELECT/WITH。
    可用视图：t（当前工作表）、t0/t1…（各工作表）。示例：
    SELECT 学院, COUNT(*) AS 人数 FROM t GROUP BY 学院 ORDER BY 人数 DESC
    涉及统计数字时务必用本工具复核，不要只根据文本片段推断。
    """
    return _s().query_table(doc_id, sql=sql or None, sheet=sheet or None, limit=limit)


@mcp.tool()
def kb_build_evidence_pack(question: str, subquestions: Optional[list[str]] = None,
                           max_docs: int = 20, collection: str = "",
                           year: int = 0) -> dict:
    """为报告/总结生成证据包：自动多路检索、按文档去重，返回 EID/原文 quote/引用/路径。

    写报告时先调用本工具，然后只依据 evidence 中的 quote 写作；
    正文用 [E001] 标注，末尾附“证据与来源”表。
    """
    return _s().build_evidence_pack(question, subquestions=subquestions,
                                    max_docs=max_docs,
                                    collection=collection or None,
                                    year=year or None)


@mcp.tool()
def kb_timeline(start: str, end: str, source: str = "screen", limit: int = 50) -> dict:
    """按时间范围列出“屏幕记忆”或“微信聊天”文档，适合回答“我昨天下午做了什么”。

    start/end 用 ISO 时间，例如 2026-10-25T14:00:00+08:00；
    source 可选 screen / chat / all；返回 doc_id 后可继续 kb_read_document 看细节。
    该时间线只覆盖 Screenpipe 已采集、且已由后台导入的时段。
    """
    return _s().timeline(start, end, source=source or "screen", limit=limit)


@mcp.tool()
def kb_stats() -> dict:
    """查看知识库状态：文档数、分块数、已向量化块数、待处理任务、最近索引记录。"""
    return _s().stats()


def run_server(http: bool = False, host: str | None = None, port: int | None = None) -> None:
    cfg = load_config()
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if http:
        h = host or cfg.server.host
        p = port or cfg.server.port
        try:
            mcp.run(transport="http", host=h, port=p)
        except Exception:
            mcp.run(transport="streamable-http", host=h, port=p)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    run_server()
