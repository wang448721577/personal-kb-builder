from __future__ import annotations

import threading
from typing import Iterable

from .config import Config

_lock = threading.Lock()
_model = None
_model_name = None


class Embedder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        global _model, _model_name
        with _lock:
            if _model is None or _model_name != cfg.embedding.model:
                from fastembed import TextEmbedding
                kw = dict(model_name=cfg.embedding.model,
                          cache_dir=str(cfg.model_dir),
                          threads=cfg.embedding.threads)
                device = (cfg.embedding.device or "npu").lower()
                providers = None
                if device == "npu":
                    providers = [("DmlExecutionProvider", {"device_type": "NPU"})]
                elif device == "gpu":
                    providers = [("DmlExecutionProvider", {"device_type": "GPU"})]
                try:
                    _model = TextEmbedding(**kw, providers=providers) if providers else TextEmbedding(**kw)
                except Exception:
                    _model = TextEmbedding(**kw)
                _model_name = cfg.embedding.model
            self.model = _model

    @property
    def dim(self) -> int:
        return self.cfg.embedding.dim

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        cleaned = []
        for t in texts:
            t = (t or "").strip()
            if len(t) > self.cfg.embedding.max_chars:
                t = t[: self.cfg.embedding.max_chars]
            cleaned.append(t or " ")
        if not cleaned:
            return []
        vectors = list(self.model.embed(cleaned, batch_size=self.cfg.embedding.batch_size))
        return [[float(x) for x in v] for v in vectors]

    def embed_query(self, query: str) -> list[float]:
        prefix = self.cfg.embedding.query_prefix or ""
        return self.embed([prefix + query])[0]
