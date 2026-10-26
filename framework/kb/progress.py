from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

import psutil

from .config import load_config


def snapshot() -> dict:
    cfg = load_config()
    conn = sqlite3.connect(str(cfg.db_path), timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        docs = dict(conn.execute(
            "SELECT COUNT(*) total, SUM(status='ok') ok, SUM(status='skipped') skipped, "
            "SUM(status='error') err FROM documents").fetchone())
        chunks = dict(conn.execute(
            "SELECT COUNT(*) total, SUM(embedded) embedded FROM chunks").fetchone())
        jobs = {}
        for r in conn.execute("SELECT kind,state,COUNT(*) n FROM jobs GROUP BY kind,state"):
            jobs[f"{r['kind']}_{r['state']}"] = r["n"]
    finally:
        conn.close()
    total = chunks.get("total") or 0
    emb = chunks.get("embedded") or 0
    mem = psutil.virtual_memory()
    proc_counts = {"vector": 0, "low": 0, "daemon": 0}
    for p in psutil.process_iter(["cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
        except Exception:
            continue
        if "-m kb vector" in cmd:
            proc_counts["vector"] += 1
        elif "-m kb low" in cmd:
            proc_counts["low"] += 1
        elif "-m kb daemon" in cmd:
            proc_counts["daemon"] += 1
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "documents": docs,
        "chunks": chunks,
        "embed_percent": round(100 * emb / total, 2) if total else 0,
        "jobs": jobs,
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": mem.percent,
        "ram_free_gb": round(mem.available / 1024 ** 3, 1),
        "workers": proc_counts,
    }


def line(s: dict) -> str:
    d, c, j = s["documents"], s["chunks"], s["jobs"]
    return (
        f"{s['time']} | docs ok={d.get('ok',0)} skipped={d.get('skipped',0)} "
        f"| vectors {c.get('embedded',0):,}/{c.get('total',0):,} ({s['embed_percent']}%) "
        f"| OCR pending={j.get('ocr_pending',0)} running={j.get('ocr_running',0)} "
        f"| ASR pending={j.get('transcribe_pending',0)} running={j.get('transcribe_running',0)} "
        f"| CPU={s['cpu_percent']:.0f}% RAM={s['ram_percent']:.0f}% free={s['ram_free_gb']}GB "
        f"| workers vector={s['workers']['vector']} low={s['workers']['low']}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    lock = cfg.data_dir / "kb-progress.pid"
    if not args.once:
        if lock.exists():
            try:
                old = int(lock.read_text(encoding="utf-8").strip())
                if old != os.getpid() and psutil.pid_exists(old):
                    return
            except Exception:
                pass
        lock.write_text(str(os.getpid()), encoding="utf-8")
    out = cfg.log_dir / "progress.log"
    try:
        while True:
            s = snapshot()
            text = line(s)
            with open(out, "a", encoding="utf-8") as f:
                f.write(text + "\n")
            (cfg.data_dir / "progress.json").write_text(
                json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            print(text, flush=True)
            if args.once:
                break
            time.sleep(max(10, args.interval))
    finally:
        if not args.once:
            try:
                lock.unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    main()
