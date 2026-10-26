from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "device_duty.json"
LOCK_FILE = STATE_FILE.with_suffix(".lock")
WINDOW = 60.0


def _locked(fn):
    for _ in range(100):
        try:
            fd = LOCK_FILE.open("x")
            try:
                return fn()
            finally:
                fd.close()
                LOCK_FILE.unlink(missing_ok=True)
        except FileExistsError:
            time.sleep(0.05)
        except Exception:
            return fn()
    return fn()


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        pass


def busy_seconds(device: str) -> float:
    d = _load()
    now = time.time()
    arr = [r for r in d.get(device, []) if now - float(r.get("t", 0)) < WINDOW]
    return sum(float(r.get("busy", 0)) for r in arr)


def busy_ratio(device: str) -> float:
    return min(1.0, busy_seconds(device) / WINDOW)


def report(device: str, seconds: float) -> None:
    def _do():
        d = _load()
        now = time.time()
        arr = [r for r in d.get(device, []) if now - float(r.get("t", 0)) < WINDOW]
        arr.append({"t": now, "busy": max(0.0, float(seconds))})
        d[device] = arr
        _save(d)
    _locked(_do)


def wait_slot(device: str, cap_pct: float, est: float = 5.0) -> None:
    if cap_pct <= 0 or not device:
        return
    cap = cap_pct / 100.0
    for _ in range(120):
        used = _locked(lambda: busy_ratio(device))
        if used + est / WINDOW <= cap:
            return
        wait = min(10.0, max(1.0, (used + est / WINDOW - cap) * WINDOW))
        time.sleep(wait)


@contextmanager
def slot(device: str, cap_pct: float, est: float | None = None):
    if cap_pct <= 0 or not device:
        yield
        return
    wait_slot(device, cap_pct, est if est is not None else 5.0)
    t0 = time.time()
    try:
        yield
    finally:
        report(device, time.time() - t0)
