from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_HOME = Path(os.environ.get("KB_HOME", r"D:\PersonalKB"))
DEFAULT_CONFIG = Path(os.environ.get("KB_CONFIG", DEFAULT_HOME / "config.yaml"))

DOC_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".caj", ".djvu",
    ".txt", ".md", ".markdown", ".rtf", ".html", ".htm", ".xml", ".csv", ".json",
    ".jsonl", ".eml", ".msg", ".ipynb", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".sql", ".sh", ".ps1",
    ".tex", ".bib", ".yaml", ".yml", ".toml", ".ini", ".log", ".epub", ".odt",
    ".ods", ".odp", ".pages", ".numbers", ".key",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}
MEDIA_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".m4v", ".webm", ".mpg", ".mpeg",
              ".mp3", ".wav", ".m4a", ".aac", ".flac", ".wma", ".ogg", ".opus"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}
SKIP_EXTS = {
    ".exe", ".msi", ".iso", ".cab", ".dll", ".so", ".dylib", ".apk", ".dmg",
    ".appimage", ".rpm", ".deb", ".zst", ".vhdx", ".vhd", ".bin", ".img",
    ".ttf", ".otf", ".woff", ".woff2", ".eot", ".ps", ".eps", ".ai", ".indd",
    ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".index", ".backup",
    ".lnk", ".url", ".ico", ".icns", ".lock", ".pid", ".pyc", ".pyd", ".class",
    ".jar", ".war", ".node", ".wasm", ".map",
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class OCRConfig:
    enabled: bool = True
    min_chars_page: int = 40
    dpi: int = 170
    max_pages: int = 0
    max_pixels: int = 30_000_000
    images: bool = True
    embedded_images: bool = False
    threads: int = 4
    device: str = "cpu"


@dataclass
class AudioConfig:
    enabled: bool = True
    model: str = "small"
    language: str = "zh"
    compute_type: str = "int8"
    max_duration_min: int = 240
    vad: bool = True
    device: str = "gpu"


@dataclass
class EmbeddingConfig:
    enabled: bool = True
    model: str = "BAAI/bge-small-zh-v1.5"
    batch_size: int = 32
    threads: int = 16
    max_chars: int = 1000
    start_delay_sec: int = 30
    dim: int = 512
    query_prefix: str = "为这个句子生成表示以用于检索相关文章："
    device: str = "npu"


@dataclass
class SearchConfig:
    chunk_chars: int = 800
    overlap_chars: int = 120
    max_chunk_chars: int = 3000
    top_k: int = 10
    candidate_k: int = 40
    rrf_k: int = 60
    rerank: bool = False
    max_chunks_per_doc: int = 3


@dataclass
class WechatConfig:
    enabled: bool = True
    full_history: bool = True
    recent_limit: int = 1000
    max_messages_per_session: int = 100000
    exclude_prefixes: list[str] = field(default_factory=list)
    exclude_chats: list[str] = field(default_factory=list)
    poll_minutes: int = 30


@dataclass
class ScreenpipeConfig:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:3030"
    token: str = ""
    data_dir: str = ""
    bucket_minutes: int = 5
    lookback_minutes: int = 15
    poll_seconds: int = 300
    retention_days: int = 365
    exclude_apps: list[str] = field(default_factory=list)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    api_port: int = 8766
    api_token: str = ""


@dataclass
class Config:
    home: Path
    root: Path
    data_dir: Path
    log_dir: Path
    db_path: Path
    lance_dir: Path
    table_dir: Path
    cache_dir: Path
    model_dir: Path
    include_dirs: list[str] = field(default_factory=list)
    exclude_dirs: list[str] = field(default_factory=list)
    include_exts: set[str] = field(default_factory=lambda: set(DOC_EXTS | IMAGE_EXTS | MEDIA_EXTS))
    max_doc_mb: int = 800
    max_media_mb: int = 3000
    max_image_mb: int = 100
    workers: int = 6
    legacy_workers: int = 2
    commit_every: int = 40
    rescan_hours: float = 6
    watch_debounce: float = 5
    keep_deleted: bool = True
    background_cpu_percent: float = 10.0
    gpu_percent: float = 10.0
    npu_percent: float = 10.0
    ocr: OCRConfig = field(default_factory=OCRConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    screenpipe: ScreenpipeConfig = field(default_factory=ScreenpipeConfig)
    wechat: WechatConfig = field(default_factory=WechatConfig)
    table_export: bool = True
    max_rows_text: int = 200
    max_table_rows: int = 500000
    raw: dict = field(default_factory=dict)

    @property
    def excluded_set(self) -> set[str]:
        return {s.lower() for s in self.exclude_dirs}

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.log_dir, self.lance_dir, self.table_dir,
                  self.cache_dir, self.model_dir):
            p.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "convert").mkdir(parents=True, exist_ok=True)

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(path)

    def collection_of(self, path: Path) -> str:
        rel = self.rel(path)
        parts = Path(rel).parts
        return parts[0] if len(parts) > 1 else "(根目录)"

    def max_size_for(self, ext: str) -> int:
        if ext in IMAGE_EXTS:
            return self.max_image_mb * 1024 * 1024
        if ext in MEDIA_EXTS:
            return self.max_media_mb * 1024 * 1024
        return self.max_doc_mb * 1024 * 1024


def _section(cls, data: dict):
    valid = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in valid})


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    home = Path(raw.get("home") or DEFAULT_HOME)
    root = Path(raw.get("root") or str(DEFAULT_HOME.parent / "MyDocuments"))
    data_dir = Path(raw.get("data_dir") or home / "data")
    log_dir = Path(raw.get("log_dir") or home / "logs")

    cfg = Config(
        home=home,
        root=root,
        data_dir=data_dir,
        log_dir=log_dir,
        db_path=Path(raw.get("db_path") or data_dir / "kb.sqlite"),
        lance_dir=Path(raw.get("lance_dir") or data_dir / "lancedb"),
        table_dir=Path(raw.get("table_dir") or data_dir / "tables"),
        cache_dir=Path(raw.get("cache_dir") or data_dir / "cache"),
        model_dir=Path(raw.get("model_dir") or data_dir / "models"),
        include_dirs=list(raw.get("include_dirs") or []),
        exclude_dirs=list(raw.get("exclude_dirs") or []),
        include_exts=set(raw.get("include_exts") or (DOC_EXTS | IMAGE_EXTS | MEDIA_EXTS)),
        max_doc_mb=int(raw.get("max_doc_mb", 800)),
        max_media_mb=int(raw.get("max_media_mb", 3000)),
        max_image_mb=int(raw.get("max_image_mb", 100)),
        workers=int(raw.get("workers", 6)),
        legacy_workers=int(raw.get("legacy_workers", 2)),
        commit_every=int(raw.get("commit_every", 40)),
        rescan_hours=float(raw.get("rescan_hours", 6)),
        watch_debounce=float(raw.get("watch_debounce", 5)),
        keep_deleted=bool(raw.get("keep_deleted", True)),
        background_cpu_percent=float(raw.get("background_cpu_percent", 10.0)),
        gpu_percent=float(raw.get("gpu_percent", 10.0)),
        npu_percent=float(raw.get("npu_percent", 10.0)),
        ocr=_section(OCRConfig, raw.get("ocr", {})),
        audio=_section(AudioConfig, raw.get("audio", {})),
        embedding=_section(EmbeddingConfig, raw.get("embedding", {})),
        search=_section(SearchConfig, raw.get("search", {})),
        server=_section(ServerConfig, raw.get("server", {})),
        screenpipe=_section(ScreenpipeConfig, raw.get("screenpipe", {})),
        wechat=_section(WechatConfig, raw.get("wechat", {})),
        table_export=bool((raw.get("tables") or {}).get("export", True)),
        max_rows_text=int((raw.get("tables") or {}).get("max_rows_text", 200)),
        max_table_rows=int((raw.get("tables") or {}).get("max_table_rows", 500000)),
        raw=raw,
    )
    cfg.ensure_dirs()
    return cfg
