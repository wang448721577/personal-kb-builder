from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import psutil

from .config import load_config
from .indexer import Indexer


def _set_low_priority(cores: int = 3, cpu_start: int | None = None) -> None:
    try:
        p = psutil.Process()
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        n = psutil.cpu_count(logical=True) or 4
        start = cpu_start if cpu_start is not None else max(0, n - cores)
        p.cpu_affinity(list(range(start, min(n, start + cores))))
    except Exception:
        pass


def _busy(max_mode: bool = False) -> bool:
    try:
        cpu = psutil.cpu_percent(interval=1)
        free = psutil.virtual_memory().available / 1024 ** 3
        if max_mode:
            return cpu > 99 or free < 1.5
        return cpu > 60 or free < 5
    except Exception:
        return False


def _throttle(target_cpu: float, max_sleep: float, duty: list[float]) -> float:
    try:
        cpu = psutil.cpu_percent(interval=None)
        free = psutil.virtual_memory().available / 1024 ** 3
        if free < 3.0:
            duty[0] = min(max_sleep * 2.0, duty[0] + 0.5)
            return duty[0]
        err = cpu - target_cpu
        if err > 1.0:
            duty[0] = min(max_sleep, duty[0] + err / 40.0)
        elif err < -1.0:
            duty[0] = max(0.0, duty[0] * 0.65 - 0.03)
    except Exception:
        pass
    return duty[0]


def run(limit_ocr: int = 1, limit_audio: int = 1, limit_embed: int = 8,
        do_ocr: bool = True, do_audio: bool = True, do_embed: bool = True,
        max_mode: bool = False, cores: int = 3, cpu_start: int | None = None,
        instance: int = 0, target_cpu: float = 0.0, max_sleep: float = 15.0,
        no_busy: bool = False, ocr_device: str | None = None,
        embed_device: str | None = None, audio_device: str | None = None) -> None:
    cfg = load_config()
    if ocr_device:
        cfg.ocr.device = ocr_device
        os.environ["KB_OCR_DEVICE"] = ocr_device
    if embed_device:
        cfg.embedding.device = embed_device
    if audio_device:
        cfg.audio.device = audio_device
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(cfg.log_dir / "low_worker.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    log = logging.getLogger("kb.low")
    lock = cfg.data_dir / ("kb-low.pid" if instance <= 0 else f"kb-low-{instance}.pid")
    if lock.exists():
        try:
            old = int(lock.read_text(encoding="utf-8").strip())
            if old != os.getpid() and psutil.pid_exists(old):
                log.warning("低优先级工人已在运行 (PID %s)", old)
                return
        except Exception:
            pass
    lock.write_text(str(os.getpid()), encoding="utf-8")
    if not max_mode:
        _set_low_priority(cores, cpu_start)
    else:
        try:
            psutil.Process().nice(psutil.NORMAL_PRIORITY_CLASS)
        except Exception:
            pass
    idx = Indexer(cfg)
    stop = threading.Event()
    lock = threading.Lock()

    def make_loop(kind: str, limit: int, base_sleep: float):
        def loop():
            duty = [0.0]
            psutil.cpu_percent(interval=None)
            while not stop.is_set():
                try:
                    if kind == "embed":
                        from . import devices
                        dev = (cfg.embedding.device or "npu").lower()
                        cap = cfg.npu_percent if dev == "npu" else (cfg.gpu_percent if dev == "gpu" else 0)
                        with devices.slot(dev if cap > 0 else "", cap, est=2.0):
                            n = idx.embed_pending(limit=limit)
                    else:
                        n = idx.process_jobs(kind, limit=limit)
                except Exception as exc:
                    log.warning("low %s: %s", kind, exc)
                    stop.wait(60)
                    continue
                if target_cpu > 0:
                    stop.wait(max(_throttle(target_cpu, max_sleep, duty), base_sleep))
                elif no_busy:
                    try:
                        free = psutil.virtual_memory().available / 1024 ** 3
                    except Exception:
                        free = 99.0
                    if free < 3.0:
                        stop.wait(20)
                        continue
                    stop.wait(base_sleep if n else 120)
                else:
                    if _busy(max_mode):
                        stop.wait(60 if not max_mode else 5)
                        continue
                    stop.wait(base_sleep if n else 120)
                from . import bg_cpu
                stop.wait(bg_cpu.pause_seconds(cfg))
        return loop

    threads = []
    if do_ocr:
        threads.append(threading.Thread(target=make_loop("ocr", limit_ocr, 5), name="low-ocr", daemon=True))
    if do_audio:
        threads.append(threading.Thread(target=make_loop("transcribe", limit_audio, 5), name="low-audio", daemon=True))
    if do_embed:
        threads.append(threading.Thread(target=make_loop("embed", limit_embed, 10), name="low-embed", daemon=True))
    for t in threads:
        t.start()
    log.info("低优先级工人已启动：instance=%s target_cpu=%s，CPU 核心 %s",
             instance, target_cpu, psutil.Process().cpu_affinity())
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        stop.set()
    finally:
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    run()
