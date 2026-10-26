from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime

import psutil

DB = r"D:\PersonalKB\data\kb.sqlite"


def read_state() -> dict:
    conn = sqlite3.connect(DB, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        v = dict(conn.execute(
            "SELECT COUNT(*) total, SUM(embedded) embedded FROM chunks").fetchone())
        jobs = {}
        for r in conn.execute("SELECT kind,state,COUNT(*) n FROM jobs GROUP BY kind,state"):
            jobs[f"{r['kind']}_{r['state']}"] = r["n"]
    finally:
        conn.close()
    vectors = 0
    for p in psutil.process_iter(["cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
        except Exception:
            continue
        if "-m kb vector" in cmd:
            vectors += 1
    mem = psutil.virtual_memory()
    return {
        "total": int(v.get("total") or 0),
        "embedded": int(v.get("embedded") or 0),
        "jobs": jobs,
        "cpu": psutil.cpu_percent(interval=None),
        "ram": mem.percent,
        "free_gb": round(mem.available / 1024 ** 3, 1),
        "vectors": vectors,
    }


def fmt_eta(seconds: float | None) -> str:
    if seconds is None or seconds <= 0 or seconds > 86400 * 30:
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def bar(pct: float, width: int = 32) -> str:
    filled = int(width * pct / 100)
    return "#" * filled + "-" * (width - filled)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    print("=" * 100)
    print("个人知识库 · 向量化实时进度")
    print("数据源：D:\\AI-KnowledgeBase\\data\\kb.sqlite  |  每 2 秒刷新，每 60 秒留一行快照")
    print("=" * 100)
    print()

    samples: deque[tuple[float, int]] = deque(maxlen=120)
    last_snapshot = 0.0
    while True:
        now = time.time()
        try:
            s = read_state()
        except Exception as exc:
            print(f"\r读取数据库失败：{exc}", end="", flush=True)
            time.sleep(5)
            continue
        total, emb = s["total"], s["embedded"]
        pct = 100.0 * emb / total if total else 0.0
        samples.append((now, emb))
        rate = 0.0
        if len(samples) >= 2 and now > samples[0][0]:
            rate = (emb - samples[0][1]) / (now - samples[0][0])
        eta = fmt_eta((total - emb) / rate) if rate > 0 else "--"
        line = (
            f"\r向量化 [{bar(pct)}] {pct:6.2f}%  "
            f"{emb:>7,}/{total:,}  "
            f"速度={rate * 60:,.0f}/分  ETA={eta:<7}  "
            f"OCR待={s['jobs'].get('ocr_pending',0):>5}  "
            f"转写待={s['jobs'].get('transcribe_pending',0):>4}  "
            f"CPU={s['cpu']:.0f}% 内存={s['ram']:.0f}% 空闲={s['free_gb']}G"
        )
        print(line, end="", flush=True)
        if now - last_snapshot >= 60:
            print(
                f"\n[{datetime.now():%H:%M:%S}] 快照：向量 {emb:,}/{total:,} "
                f"({pct:.2f}%)，速度 {rate*60:,.0f}/分，ETA {eta}，"
                f"OCR待 {s['jobs'].get('ocr_pending',0)}，转写待 {s['jobs'].get('transcribe_pending',0)}"
            )
            last_snapshot = now
        if total and emb >= total:
            print("\n\n全部向量化完成。")
            break
        time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出进度显示。")
