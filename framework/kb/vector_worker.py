from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

import psutil

from .config import load_config
from .indexer import Indexer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cores", type=int, default=12)
    ap.add_argument("--threads", type=int, default=12)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--num-shards", type=int, default=1)
    ap.add_argument("--cpu-start", type=int, default=None)
    ap.add_argument("--min-free", type=float, default=4.0)
    ap.add_argument("--max-cpu", type=float, default=92.0)
    ap.add_argument("--target-cpu", type=float, default=0.0)
    ap.add_argument("--max-sleep", type=float, default=15.0)
    args = ap.parse_args()

    cfg = load_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(cfg.log_dir / "vector_worker.log", encoding="utf-8"),
                  logging.StreamHandler()],
    )
    log = logging.getLogger("kb.vector")
    lock = cfg.data_dir / ("kb-vector.pid" if args.num_shards <= 1
                           else f"kb-vector-{args.shard}.pid")
    if lock.exists():
        try:
            old = int(lock.read_text(encoding="utf-8").strip())
            if old != os.getpid() and psutil.pid_exists(old):
                log.warning("向量化工人已在运行 (PID %s)", old)
                return
        except Exception:
            pass
    lock.write_text(str(os.getpid()), encoding="utf-8")
    try:
        p = psutil.Process()
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        n = psutil.cpu_count(logical=True) or 16
        start = args.cpu_start if args.cpu_start is not None else max(0, n - args.cores)
        p.cpu_affinity(list(range(start, min(n, start + args.cores))))
        cfg.embedding.threads = max(1, args.threads)
        cfg.embedding.batch_size = max(8, args.batch)
        idx = Indexer(cfg)
        log.info("向量化工人启动：shard=%s/%s cores=%s threads=%s batch=%s affinity=%s",
                 args.shard, args.num_shards, args.cores, args.threads, args.batch,
                 p.cpu_affinity())
        idle = 0
        duty = 0.0
        psutil.cpu_percent(interval=None)
        while True:
            try:
                cpu = psutil.cpu_percent(interval=None)
                free = psutil.virtual_memory().available / 1024 ** 3
                if free < args.min_free or (args.target_cpu <= 0 and cpu > args.max_cpu):
                    log.warning("资源紧张暂停：CPU=%s%% 空闲=%.1fGB", cpu, free)
                    time.sleep(20)
                    continue
                from . import bg_cpu, devices
                dev = (cfg.embedding.device or "npu").lower()
                cap = cfg.npu_percent if dev == "npu" else (cfg.gpu_percent if dev == "gpu" else 0)
                t_batch = time.time()
                with devices.slot(dev if cap > 0 else "", cap, est=2.0):
                    n = idx.embed_pending(limit=args.batch, shard=args.shard,
                                          num_shards=args.num_shards)
                if n:
                    idle = 0
                    log.info("embedded %d chunks in one batch", n)
                    if args.target_cpu > 0:
                        cpu = psutil.cpu_percent(interval=None)
                        err = cpu - args.target_cpu
                        if err > 1.0:
                            duty = min(args.max_sleep, max(0.3, duty + err / 40.0))
                        elif err < -1.0:
                            duty = max(0.0, duty * 0.65 - 0.03)
                    time.sleep(max(duty, args.sleep))
                    time.sleep(bg_cpu.pause_seconds(cfg, max_sleep=10))
                else:
                    idle += 1
                    time.sleep(60)
            except Exception as exc:
                log.warning("vector batch failed: %s", exc)
                time.sleep(5)
            if idle > 10:
                log.info("没有待向量化数据，工人休眠")
                time.sleep(300)
    finally:
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
