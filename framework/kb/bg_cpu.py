from __future__ import annotations

import time

import psutil

_procs: dict[int, psutil.Process] = {}
_last = {"t": 0.0, "v": 0.0}

_BG_KEYS = (
    "-m kb daemon",
    "-m kb low",
    "-m kb vector",
    "-m kb shots",
    "kb\\ocr_worker.py",
    "kb\\pdf_worker.py",
    "kb\\office_convert_worker.py",
    "screenpipe.exe",
    "screenpipe_tray.pyw",
)


def _is_background(cmd: str) -> bool:
    c = cmd.lower()
    return any(k in c for k in _BG_KEYS)


def share() -> float:
    global _last
    now = time.time()
    if now - _last["t"] < 1.0:
        return _last["v"]
    total = 0.0
    seen: set[int] = set()
    for p in psutil.process_iter(["pid", "cmdline"]):
        cmd = " ".join(p.info["cmdline"] or [])
        if not cmd or not _is_background(cmd):
            continue
        pid = int(p.info["pid"])
        seen.add(pid)
        pr = _procs.get(pid)
        if pr is None:
            try:
                pr = psutil.Process(pid)
                _procs[pid] = pr
            except Exception:
                continue
        try:
            total += pr.cpu_percent(None)
        except Exception:
            pass
    for pid in list(_procs):
        if pid not in seen:
            _procs.pop(pid, None)
    cores = psutil.cpu_count(logical=True) or 1
    value = total / (cores * 100.0)
    _last = {"t": now, "v": value}
    return value


def limit(cfg) -> float:
    return float(getattr(cfg, "background_cpu_percent", 0) or 0)


def pause_seconds(cfg, max_sleep: float = 10.0) -> float:
    lim = limit(cfg)
    if lim <= 0:
        return 0.0
    s = share()
    if s <= lim / 100.0 * 0.8:
        return 0.0
    over = (s - lim / 100.0 * 0.8) / max(lim / 100.0, 0.001)
    return min(max_sleep, 0.5 + over * 4.0)


def wait_if_over(cfg, max_sleep: float = 10.0) -> float:
    s = pause_seconds(cfg, max_sleep)
    if s > 0:
        time.sleep(s)
    return s


def pace_device(elapsed: float, limit_pct: float = 10.0, max_sleep: float = 180.0) -> float:
    if elapsed <= 0 or limit_pct <= 0:
        return 0.0
    ratio = 100.0 / limit_pct - 1.0
    nap = min(max_sleep, elapsed * ratio)
    if nap > 0:
        time.sleep(nap)
    return nap
