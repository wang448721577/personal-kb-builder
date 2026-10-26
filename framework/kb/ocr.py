from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from .config import Config
from .pymupdf_lock import PDF_LOCK

_lock = threading.Lock()
_engine = None
_engine_device = None


def _get_engine():
    global _engine, _engine_device
    if _engine is None:
        logging.getLogger("RapidOCR").setLevel(logging.ERROR)
        from rapidocr import RapidOCR
        from .config import load_config
        cfg = load_config()
        device = (os.environ.get("KB_OCR_DEVICE") or cfg.ocr.device or "cpu").lower()
        params = {"EngineConfig.onnxruntime.intra_op_num_threads": 4}
        if device in ("gpu", "npu"):
            params["EngineConfig.onnxruntime.use_dml"] = True
            if device == "npu":
                params["EngineConfig.onnxruntime.dml_ep_cfg"] = {"device_type": "NPU"}
        else:
            params["EngineConfig.onnxruntime.use_dml"] = False
        try:
            _engine = RapidOCR(params=params)
            _engine_device = device
        except Exception:
            _engine = RapidOCR(params={
                "EngineConfig.onnxruntime.use_dml": False,
                "EngineConfig.onnxruntime.intra_op_num_threads": 4,
            })
            _engine_device = "cpu"
    return _engine


def _device_slot():
    from .config import load_config
    from .devices import slot
    cfg = load_config()
    device = (_engine_device or "cpu").lower()
    if device == "gpu":
        return slot("gpu", cfg.gpu_percent)
    if device == "npu":
        return slot("npu", cfg.npu_percent)
    return slot("", 0)


def _join_result(result) -> tuple[str, float]:
    txts = getattr(result, "txts", None) or ()
    scores = getattr(result, "scores", None) or ()
    lines = []
    conf = []
    for i, t in enumerate(txts):
        t = str(t).strip()
        if not t:
            continue
        try:
            s = float(scores[i]) if i < len(scores) else 1.0
        except Exception:
            s = 1.0
        if s < 0.25:
            continue
        lines.append(t)
        conf.append(s)
    return "\n".join(lines), (sum(conf) / len(conf) if conf else 0.0)


def ocr_image(path: Path) -> tuple[str, float]:
    from PIL import Image
    with _lock:
        engine = _get_engine()
        try:
            with Image.open(str(path)) as img:
                img = img.convert("RGB")
                if img.width * img.height > 40_000_000:
                    scale = (40_000_000 / (img.width * img.height)) ** 0.5
                    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
                with _device_slot():
                    result = engine(img)
        except Exception:
            with _device_slot():
                result = engine(str(path))
    return _join_result(result)


def ocr_pdf_pages(path: Path, pages: list[int], cfg: Config) -> list[tuple[int, str, float]]:
    try:
        import pymupdf as fitz
    except Exception:
        import fitz  # type: ignore
    from PIL import Image

    out: list[tuple[int, str, float]] = []
    from . import bg_cpu
    with PDF_LOCK:
        doc = fitz.open(str(path))
        try:
            for page_no in pages:
                if page_no < 1 or page_no > doc.page_count:
                    continue
                bg_cpu.wait_if_over(cfg)
                page = doc[page_no - 1]
                scale = cfg.ocr.dpi / 72.0
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                                          colorspace=fitz.csRGB, alpha=False)
                    if pix.width * pix.height > cfg.ocr.max_pixels:
                        s = (cfg.ocr.max_pixels / (pix.width * pix.height)) ** 0.5
                        pix = page.get_pixmap(matrix=fitz.Matrix(scale * s, scale * s),
                                              colorspace=fitz.csRGB, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                except Exception:
                    continue
                with _lock:
                    try:
                        with _device_slot():
                            result = _get_engine()(img)
                        text, conf = _join_result(result)
                    except Exception:
                        text, conf = "", 0.0
                if text.strip():
                    out.append((page_no, text, conf))
        finally:
            try:
                doc.close()
            except Exception:
                pass
    return out
