from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import jieba

jieba.setLogLevel(20)

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_WORD = re.compile(r"[A-Za-z0-9_]+")
_HEADING = re.compile(r"^(heading|标题)\s*(\d+)$", re.I)
_YEAR = re.compile(r"(20\d{2})")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    words = len(_WORD.findall(text))
    return int(cjk * 0.75 + words * 1.3 + len(text) * 0.02)


def tokenize_for_fts(text: str) -> str:
    if not text:
        return ""
    tokens: list[str] = []
    for tok in jieba.cut_for_search(text):
        tok = tok.strip()
        if not tok:
            continue
        if not _WORD.fullmatch(tok) and not _CJK.search(tok):
            continue
        tokens.append(tok.lower())
    return " ".join(tokens)


def build_fts_queries(query: str) -> tuple[str, str, list[str]]:
    raw_tokens = tokenize_for_fts(query).split()
    terms: list[str] = []
    for t in raw_tokens:
        if t not in terms:
            terms.append(t)
    terms = terms[:24]

    def q(t: str) -> str:
        return '"' + t.replace('"', '""') + '"'

    and_q = " AND ".join(q(t) for t in terms)
    or_q = " OR ".join(q(t) for t in terms)
    return and_q, or_q, terms


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*", text)
    return [p for p in parts if p.strip()]


def _tail_overlap(text: str, n: int) -> str:
    if n <= 0 or not text:
        return ""
    tail = text[-n:]
    sentences = split_sentences(tail)
    if len(sentences) > 1:
        return "".join(sentences[1:]).strip() or tail.strip()
    return tail.strip()


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    cur = ""
    for sent in split_sentences(text):
        if len(sent) > max_chars:
            if cur:
                pieces.append(cur)
                cur = ""
            for i in range(0, len(sent), max_chars):
                pieces.append(sent[i:i + max_chars])
        elif len(cur) + len(sent) + 1 > max_chars:
            pieces.append(cur)
            cur = sent
        else:
            cur = f"{cur}\n{sent}".strip() if cur else sent
    if cur:
        pieces.append(cur)
    return [p.strip() for p in pieces if p.strip()]


def chunk_text(text: str, max_chars: int = 800, overlap: int = 120) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            units.append(para)
        else:
            units.extend(_split_long(para, max_chars))

    chunks: list[str] = []
    cur = ""
    for unit in units:
        if cur and len(cur) + len(unit) + 1 > max_chars:
            chunks.append(cur.strip())
            tail = _tail_overlap(cur, overlap)
            cur = f"{tail}\n{unit}".strip() if tail else unit
        else:
            cur = f"{cur}\n{unit}".strip() if cur else unit
        while len(cur) > max_chars * 2:
            chunks.append(cur[:max_chars].strip())
            cur = cur[max_chars:].strip()
    if cur:
        chunks.append(cur.strip())
    return [c for c in chunks if c]


def make_snippet(text: str, terms: list[str], max_len: int = 320) -> tuple[str, str]:
    text = normalize_text(text)
    if not text:
        return "", ""
    if not terms:
        quote = text[:max_len]
        return quote, quote + ("…" if len(text) > max_len else "")

    lower = text.lower()
    best = -1
    for term in sorted(terms, key=len, reverse=True):
        pos = lower.find(term.lower())
        if pos >= 0 and (best < 0 or pos < best):
            best = pos
            break
    if best < 0:
        quote = text[:max_len]
        return quote, quote + ("…" if len(text) > max_len else "")

    start = max(0, best - max_len // 3)
    if start > 0:
        next_nl = text.find("\n", start, best)
        if next_nl > 0:
            start = next_nl + 1
    end = min(len(text), start + max_len)
    if end < len(text):
        prev_nl = text.rfind("\n", end - 80, end)
        if prev_nl > 0:
            end = prev_nl
    quote = text[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return quote, f"{prefix}{quote}{suffix}"


TYPE_RULES: list[tuple[str, list[str]]] = [
    ("论文", ["论文", "期刊", "文献", "paper", "research article", "sci", "ssci", "投稿", "审稿", "参考文献"]),
    ("项目", ["基金", "项目", "申报", "申请书", "任务书", "结项", "立项", "nsfc", "国自然", "社科"]),
    ("教学", ["教学", "课程", "课件", "教案", "上课", "备课", "期末", "试卷", "培养方案", "毕设", "答辩", "学生作业"]),
    ("会议", ["会议", "论坛", "纪要", "参会", "研讨", "讲座", "报告会"]),
    ("人事", ["人事", "招聘", "职称", "考核", "聘任", "工资", "社保", "编制"]),
    ("财务", ["财务", "报销", "发票", "预算", "经费", "采购", "合同"]),
    ("公文", ["通知", "办法", "制度", "规定", "请示", "批复", "函", "公示", "公告", "红头"]),
    ("学生", ["招生", "就业", "毕业", "奖助", "奖学金", "研究生", "本科生", "学籍", "培养"]),
    ("科研", ["科研", "成果", "专利", "软著", "获奖", "学科建设", "平台建设"]),
]

DEPT_RULES: list[tuple[str, list[str]]] = [
    ("研究生院", ["研究生院", "研究生工作", "学位"]),
    ("计算机学院", ["计算机学院", "信息学院", "人工智能学院", "大数据"]),
    ("教务处", ["教务", "教学运行", "考试"]),
]


def classify(rel_path: str, ext: str, mtime: float | None = None) -> dict:
    lower = rel_path.lower()
    parts = Path(rel_path).parts
    collection = parts[0] if len(parts) > 1 else "(根目录)"

    doc_type = "其他"
    for name, keys in TYPE_RULES:
        if any(k.lower() in lower for k in keys):
            doc_type = name
            break
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}:
        doc_type = "图片"
    elif ext in {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".mp3", ".wav", ".m4a", ".aac", ".flac"}:
        doc_type = "音视频"

    dept = collection
    for name, keys in DEPT_RULES:
        if any(k.lower() in lower for k in keys):
            dept = name
            break

    years = _YEAR.findall(rel_path)
    if years:
        year = int(years[-1])
    elif mtime:
        year = datetime.fromtimestamp(mtime).year
    else:
        year = None
    return {"collection": collection, "department": dept, "year": year, "doc_type": doc_type}


def is_heading_style(style_name: str) -> tuple[bool, int]:
    m = _HEADING.match((style_name or "").strip())
    if m:
        return True, int(m.group(2))
    return False, 0


def clean_title(title: str, fallback: str) -> str:
    title = normalize_text(title or "")
    if len(title) > 200:
        title = title[:200].rstrip()
    return title or fallback


def format_ts(ms: int | None) -> str:
    if ms is None:
        return ""
    if int(ms) > 100_000_000_000:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    s = max(0, int(ms // 1000))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
