from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .db import init_db
from .indexer import Indexer
from .search import Searcher


def setup_logging(cfg, console: bool = True) -> None:
    logger = logging.getLogger("kb")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(cfg.log_dir / "kb.log", maxBytes=20 * 1024 * 1024,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    for name in ("RapidOCR", "httpx", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _cfg(args):
    return load_config(getattr(args, "config", None))


def cmd_init(args):
    cfg = _cfg(args)
    init_db(cfg)
    print(json.dumps({
        "db": str(cfg.db_path), "lance": str(cfg.lance_dir),
        "tables": str(cfg.table_dir), "root": str(cfg.root),
    }, ensure_ascii=False, indent=2))


def cmd_index(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    idx = Indexer(cfg)
    result = idx.run(full=args.full, limit=args.limit, path_filter=args.path,
                     workers=args.workers, retry_problems=args.retry_problems,
                     respect_budget=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_search(args):
    cfg = _cfg(args)
    s = Searcher(cfg)
    try:
        res = s.search(args.query, top_k=args.top, collection=args.collection,
                       year=args.year, doc_type=args.type, ext=args.ext,
                       path_prefix=args.prefix, mode=args.mode,
                       include_meta=args.include_meta, source="cli")
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        print(f"查询：{res['query']}  模式：{res['mode']}  命中：{res['hits']}  "
              f"耗时：{res['elapsed_ms']}ms")
        for i, r in enumerate(res["results"], 1):
            print(f"\n[{i}] {r['citation']}  score={r['score']}")
            print(f"    {r['snippet']}")
            print(f"    {r['path']}")
            print(f"    doc_id={r['doc_id']} chunk_id={r['chunk_id']} "
                  f"{r['collection']} {r['year']} {r['doc_type']}")
        if not res["results"]:
            print(res.get("note") or "无结果")
    finally:
        s.close()


def cmd_evidence(args):
    cfg = _cfg(args)
    s = Searcher(cfg)
    try:
        res = s.build_evidence_pack(args.question, subquestions=args.sub,
                                    max_docs=args.max, collection=args.collection,
                                    year=args.year)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    finally:
        s.close()


def cmd_read(args):
    cfg = _cfg(args)
    s = Searcher(cfg)
    try:
        pages = [int(x) for x in args.pages.split(",")] if args.pages else None
        res = s.read_document(doc_id=args.doc_id, path=args.path, pages=pages,
                              max_chars=args.max_chars)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(res.get("text") or res.get("error", ""))
            if res.get("truncated"):
                print("\n…（已截断）")
    finally:
        s.close()


def cmd_list(args):
    cfg = _cfg(args)
    s = Searcher(cfg)
    try:
        res = s.list_documents(query=args.query, collection=args.collection,
                               year=args.year, doc_type=args.type,
                               limit=args.limit, offset=args.offset)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    finally:
        s.close()


def cmd_table(args):
    cfg = _cfg(args)
    s = Searcher(cfg)
    try:
        res = s.query_table(args.doc_id, sql=args.sql, sheet=args.sheet,
                            limit=args.limit)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    finally:
        s.close()


def cmd_timeline(args):
    cfg = _cfg(args)
    s = Searcher(cfg)
    try:
        res = s.timeline(args.start, args.end, source=args.source, limit=args.limit)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    finally:
        s.close()


def cmd_status(args):
    cfg = _cfg(args)
    init_db(cfg)
    idx = Indexer(cfg)
    print(json.dumps(idx.status(), ensure_ascii=False, indent=2, default=str))


def cmd_ocr(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    idx = Indexer(cfg)
    n = idx.process_jobs("ocr", limit=args.limit)
    print(json.dumps({"processed": n}, ensure_ascii=False))


def cmd_transcribe(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    idx = Indexer(cfg)
    n = idx.process_jobs("transcribe", limit=args.limit)
    print(json.dumps({"processed": n}, ensure_ascii=False))


def cmd_embed(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    idx = Indexer(cfg)
    n = idx.embed_pending(limit=args.limit)
    print(json.dumps({"embedded": n}, ensure_ascii=False))


def cmd_prune(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    idx = Indexer(cfg)
    n = idx.prune_missing(force=getattr(args, "force", False))
    print(json.dumps({"removed": n}, ensure_ascii=False))


def cmd_dedupe(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    idx = Indexer(cfg)
    n = idx.dedupe_existing()
    print(json.dumps({"folded": n}, ensure_ascii=False))


def cmd_wechat(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    from .wechat import WeChatIngestor
    ing = WeChatIngestor(cfg)
    if args.action == "sync":
        full = True if args.full else (False if args.incremental else None)
        print(json.dumps(ing.sync(full=full, recent=args.recent), ensure_ascii=False, indent=2))
    elif args.action == "import":
        if not args.file:
            raise SystemExit("--file 必填")
        print(json.dumps(ing.import_file(Path(args.file), delete=args.delete),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(ing.status(), ensure_ascii=False, indent=2))


def cmd_shots(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    from .screen_shots import run
    run(interval=args.interval, once=args.once, force=args.force)


def cmd_screenpipe(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    from .screenpipe import ScreenpipeIngestor, _parse_ts
    ing = ScreenpipeIngestor(cfg)
    if args.action == "ingest":
        since = _parse_ts(args.since) if args.since else None
        until = _parse_ts(args.until) if args.until else None
        print(json.dumps(ing.ingest(since=since, until=until), ensure_ascii=False, indent=2))
    elif args.action == "prune":
        print(json.dumps({"removed": ing.prune(args.days)}, ensure_ascii=False))
    else:
        print(json.dumps(ing.status(), ensure_ascii=False, indent=2))


def cmd_low(args):
    cfg = _cfg(args)
    from .low_worker import run
    run(limit_ocr=args.ocr, limit_audio=args.audio, limit_embed=args.embed,
        do_ocr=not args.no_ocr, do_audio=not args.no_audio, do_embed=not args.no_embed,
        max_mode=args.max, cores=args.cores, cpu_start=args.cpu_start,
        instance=args.instance, target_cpu=args.target_cpu, max_sleep=args.max_sleep,
        no_busy=args.no_busy, ocr_device=args.ocr_device,
        embed_device=args.embed_device, audio_device=args.audio_device)


def cmd_vector(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    from .vector_worker import main as vector_main
    import sys
    argv = ["kb-vector", "--cores", str(args.cores), "--threads", str(args.threads),
            "--batch", str(args.batch), "--sleep", str(args.sleep),
            "--shard", str(args.shard), "--num-shards", str(args.num_shards),
            "--min-free", str(args.min_free), "--max-cpu", str(args.max_cpu),
            "--target-cpu", str(args.target_cpu), "--max-sleep", str(args.max_sleep)]
    if args.cpu_start is not None:
        argv += ["--cpu-start", str(args.cpu_start)]
    sys.argv = argv
    vector_main()


def cmd_progress(args):
    cfg = _cfg(args)
    from .progress import main as progress_main
    import sys
    sys.argv = ["kb-progress", "--interval", str(args.interval)] + (["--once"] if args.once else [])
    progress_main()


def cmd_api(args):
    cfg = _cfg(args)
    setup_logging(cfg, console=False)
    from .api_server import run
    run(host=args.host, port=args.port)


def cmd_mcp(args):
    cfg = _cfg(args)
    setup_logging(cfg, console=False)
    from .mcp_server import run_server
    run_server(http=args.http, host=args.host, port=args.port)


def cmd_doctor(args):
    import importlib
    cfg = _cfg(args)
    checks = {}
    for mod in ("jieba", "watchdog", "lancedb", "fastembed", "fastmcp", "duckdb",
                "fitz", "docx", "openpyxl", "pptx", "rapidocr", "faster_whisper",
                "win32com", "blake3"):
        try:
            importlib.import_module(mod)
            checks[mod] = "OK"
        except Exception as exc:
            checks[mod] = f"MISSING: {exc}"
    from .utils import find_ffmpeg, find_ffprobe
    checks["ffmpeg"] = find_ffmpeg() or "MISSING"
    checks["ffprobe"] = find_ffprobe() or "MISSING"
    checks["root"] = str(cfg.root) if cfg.root.exists() else "MISSING"
    checks["winword"] = "OK" if Path(r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE").exists() else "MISSING"
    init_db(cfg)
    idx = Indexer(cfg)
    status = idx.status()
    model_dir = cfg.model_dir
    model_size = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file()) if model_dir.exists() else 0
    out = {
        "checks": checks,
        "db": str(cfg.db_path),
        "db_exists": cfg.db_path.exists(),
        "model_size_gb": round(model_size / 1024 ** 3, 2),
        "status": status,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


# ---------------- daemon ----------------
def _is_indexable(cfg, path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        rel = cfg.rel(path)
    except Exception:
        return False
    if any(part.lower() in cfg.excluded_set for part in Path(rel).parts[:-1]):
        return False
    ext = path.suffix.lower()
    if ext not in cfg.include_exts:
        return False
    name = path.name.lower()
    if name.startswith("~$") or name.startswith(".") or name.endswith((".tmp", ".bak", ".part")):
        return False
    try:
        st = path.stat()
        return 0 < st.st_size <= cfg.max_size_for(ext)
    except Exception:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid) in (out.stdout or "")
    except Exception:
        return False


def _acquire_daemon_lock(cfg):
    lock = cfg.data_dir / "kb-daemon.pid"
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
            if pid != os.getpid() and _pid_alive(pid):
                print(json.dumps({"error": f"守护进程已在运行 (PID {pid})"}, ensure_ascii=False))
                return None
        except Exception:
            pass
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def cmd_daemon(args):
    cfg = _cfg(args)
    setup_logging(cfg)
    log = logging.getLogger("kb.daemon")
    lock = _acquire_daemon_lock(cfg)
    if lock is None:
        return
    init_db(cfg)
    idx = Indexer(cfg)
    stop = threading.Event()
    ready = threading.Event()

    def initial_scan():
        try:
            log.info("启动增量扫描…")
            idx.run(full=False)
            log.info("启动扫描完成")
        except Exception as exc:
            log.exception("启动扫描失败: %s", exc)
        finally:
            ready.set()

    def ocr_loop():
        from . import bg_cpu
        while not stop.is_set():
            try:
                bg_cpu.wait_if_over(cfg)
                n = idx.process_jobs("ocr", limit=20)
                if not n:
                    stop.wait(10)
            except Exception as exc:
                log.warning("OCR 队列异常: %s", exc)
                stop.wait(20)

    def transcribe_loop():
        from . import bg_cpu
        while not stop.is_set():
            try:
                bg_cpu.wait_if_over(cfg)
                n = idx.process_jobs("transcribe", limit=1)
                if not n:
                    stop.wait(30)
            except Exception as exc:
                log.warning("转写队列异常: %s", exc)
                stop.wait(60)

    def wechat_loop():
        if not cfg.wechat.enabled:
            return
        from .wechat import WeChatIngestor
        from . import bg_cpu
        ing = WeChatIngestor(cfg)
        while not stop.is_set():
            try:
                bg_cpu.wait_if_over(cfg)
                r = ing.sync()
                log.info("微信聊天入库：%s", r)
            except Exception as exc:
                log.warning("微信聊天入库失败: %s", exc)
            stop.wait(max(300, cfg.wechat.poll_minutes * 60))

    def screen_loop():
        if not cfg.screenpipe.enabled:
            return
        from .screenpipe import ScreenpipeIngestor
        from . import bg_cpu
        ing = ScreenpipeIngestor(cfg)
        while not stop.is_set():
            try:
                bg_cpu.wait_if_over(cfg)
                r = ing.ingest()
                if r.get("written"):
                    log.info("屏幕记忆入库：%s", r)
            except Exception as exc:
                log.warning("屏幕记忆入库失败: %s", exc)
            stop.wait(max(60, cfg.screenpipe.poll_seconds))

    def embed_loop():
        from . import bg_cpu, devices
        while not stop.is_set():
            try:
                bg_cpu.wait_if_over(cfg)
                dev = (cfg.embedding.device or "npu").lower()
                cap = cfg.npu_percent if dev == "npu" else (cfg.gpu_percent if dev == "gpu" else 0)
                with devices.slot(dev if cap > 0 else "", cap, est=2.0):
                    n = idx.embed_pending(limit=cfg.embedding.batch_size * 8)
                if not n:
                    stop.wait(30)
            except Exception as exc:
                log.warning("向量化队列异常: %s", exc)
                stop.wait(60)

    def watch_loop():
        ready.wait()
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception as exc:
            log.error("watchdog 不可用: %s", exc)
            return

        events: "queue.Queue[tuple[str, str]]" = queue.Queue()

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    events.put(("upsert", event.src_path))

            def on_modified(self, event):
                if not event.is_directory:
                    events.put(("upsert", event.src_path))

            def on_moved(self, event):
                if not event.is_directory:
                    events.put(("move", event.src_path))
                    events.put(("upsert", event.dest_path))

            def on_deleted(self, event):
                if not event.is_directory:
                    events.put(("delete", event.src_path))

        observer = Observer()
        observer.schedule(Handler(), str(cfg.root), recursive=True)
        observer.start()
        log.info("文件监听已启动: %s", cfg.root)
        pending: dict[str, float] = {}
        last_reconcile = time.time()
        try:
            while not stop.is_set():
                try:
                    kind, p = events.get(timeout=1)
                    path = Path(p)
                    if kind in ("delete", "move"):
                        pending.pop(str(path), None)
                        if kind == "move" or not cfg.keep_deleted:
                            if idx.delete_path(path):
                                log.info("已移除: %s", path)
                        else:
                            log.info("文件已删除，知识库保留记录: %s", path)
                    elif _is_indexable(cfg, path):
                        pending[str(path)] = time.time() + cfg.watch_debounce
                except queue.Empty:
                    pass
                now = time.time()
                due = [p for p, t in pending.items() if t <= now]
                for p in due:
                    pending.pop(p, None)
                    path = Path(p)
                    if not _is_indexable(cfg, path):
                        continue
                    if idx.is_problem_path(path):
                        continue
                    try:
                        from . import bg_cpu
                        bg_cpu.wait_if_over(cfg)
                        res = idx._parse_task(path)
                        st = idx.apply_result(res)
                        log.info("增量索引 %s: %s", st, path)
                    except Exception as exc:
                        log.warning("增量索引失败 %s: %s", path, exc)
                if cfg.rescan_hours and now - last_reconcile > cfg.rescan_hours * 3600:
                    last_reconcile = now
                    log.info("开始周期性对账…")
                    try:
                        idx.run(full=False)
                        idx.prune_missing()
                    except Exception as exc:
                        log.warning("周期性对账失败: %s", exc)
        finally:
            observer.stop()
            try:
                observer.join(timeout=5)
            except Exception:
                pass

    threads = [
        threading.Thread(target=initial_scan, name="initial-scan", daemon=True),
        threading.Thread(target=watch_loop, name="watch", daemon=True),
    ]
    if not getattr(args, "no_ocr", False):
        threads.append(threading.Thread(target=ocr_loop, name="ocr", daemon=True))
    if not getattr(args, "no_transcribe", False):
        threads.append(threading.Thread(target=transcribe_loop, name="transcribe", daemon=True))
    if not getattr(args, "no_embed", False):
        threads.append(threading.Thread(target=embed_loop, name="embed", daemon=True))
    if not getattr(args, "no_screen", False):
        threads.append(threading.Thread(target=screen_loop, name="screen", daemon=True))
    if not getattr(args, "no_wechat", False):
        threads.append(threading.Thread(target=wechat_loop, name="wechat", daemon=True))
    for t in threads:
        t.start()
    log.info("知识库守护进程已启动，PID=%s", os.getpid())
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("收到退出信号")
        stop.set()
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass


def build_parser():
    p = argparse.ArgumentParser(prog="kb", description="个人电脑本地知识库")
    p.add_argument("--config", help="配置文件路径", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="初始化数据库")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("index", help="扫描并建立/更新索引")
    sp.add_argument("--full", action="store_true", help="忽略 mtime，全部重新解析")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--path", type=str, default=None, help="只处理相对路径包含该字符串的文件")
    sp.add_argument("--workers", type=int, default=None)
    sp.add_argument("--retry-problems", action="store_true",
                    help="连之前打开失败/加密/跳过的问题文件也重试（默认永不重试）")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("search", help="检索知识库")
    sp.add_argument("query")
    sp.add_argument("--top", type=int, default=10)
    sp.add_argument("--collection", default=None)
    sp.add_argument("--year", type=int, default=None)
    sp.add_argument("--type", dest="type", default=None)
    sp.add_argument("--ext", default=None)
    sp.add_argument("--prefix", default=None)
    sp.add_argument("--mode", default="hybrid")
    sp.add_argument("--include-meta", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("evidence", help="生成报告证据包")
    sp.add_argument("question")
    sp.add_argument("--sub", nargs="*", default=None)
    sp.add_argument("--max", type=int, default=20)
    sp.add_argument("--collection", default=None)
    sp.add_argument("--year", type=int, default=None)
    sp.set_defaults(func=cmd_evidence)

    sp = sub.add_parser("read", help="读取文档正文")
    sp.add_argument("--doc-id", type=int, default=None)
    sp.add_argument("--path", default=None)
    sp.add_argument("--pages", default=None)
    sp.add_argument("--max-chars", type=int, default=30000)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("list", help="列出文档")
    sp.add_argument("--query", default=None)
    sp.add_argument("--collection", default=None)
    sp.add_argument("--year", type=int, default=None)
    sp.add_argument("--type", dest="type", default=None)
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--offset", type=int, default=0)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("table", help="查询 Excel/CSV 数据")
    sp.add_argument("--doc-id", type=int, required=True)
    sp.add_argument("--sql", default=None)
    sp.add_argument("--sheet", default=None)
    sp.add_argument("--limit", type=int, default=100)
    sp.set_defaults(func=cmd_table)

    sp = sub.add_parser("timeline", help="按时间范围列出屏幕/聊天记忆")
    sp.add_argument("--start", required=True)
    sp.add_argument("--end", required=True)
    sp.add_argument("--source", choices=["screen", "chat", "all"], default="screen")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_timeline)

    sp = sub.add_parser("status", help="知识库状态")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("ocr", help="处理 OCR 队列")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_ocr)

    sp = sub.add_parser("transcribe", help="处理音视频转写队列")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_transcribe)

    sp = sub.add_parser("embed", help="处理向量化队列")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_embed)

    sp = sub.add_parser("prune", help="清理已删除文件的索引（默认遵守 keep_deleted）")
    sp.add_argument("--force", action="store_true", help="忽略 keep_deleted，强制清理")
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("dedupe", help="按内容哈希折叠重复文档")
    sp.set_defaults(func=cmd_dedupe)

    sp = sub.add_parser("wechat", help="微信聊天记录导入/状态")
    sp.add_argument("action", choices=["sync", "status", "import"], nargs="?", default="status")
    sp.add_argument("--full", action="store_true")
    sp.add_argument("--incremental", action="store_true")
    sp.add_argument("--recent", type=int, default=None)
    sp.add_argument("--file", default=None)
    sp.add_argument("--delete", action="store_true")
    sp.set_defaults(func=cmd_wechat)

    sp = sub.add_parser("screenpipe", help="屏幕记忆导入/状态/清理")
    sp.add_argument("action", choices=["ingest", "status", "prune"], nargs="?", default="status")
    sp.add_argument("--since", default=None)
    sp.add_argument("--until", default=None)
    sp.add_argument("--days", type=int, default=None)
    sp.set_defaults(func=cmd_screenpipe)

    sp = sub.add_parser("shots", help="截屏回退工人（文本采集不到的窗口才截图）")
    sp.add_argument("--interval", type=int, default=45)
    sp.add_argument("--once", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_shots)

    sp = sub.add_parser("low", help="低优先级后台处理 OCR/转写/向量化")
    sp.add_argument("--ocr", type=int, default=1)
    sp.add_argument("--audio", type=int, default=1)
    sp.add_argument("--embed", type=int, default=8)
    sp.add_argument("--no-ocr", action="store_true")
    sp.add_argument("--no-audio", action="store_true")
    sp.add_argument("--no-embed", action="store_true")
    sp.add_argument("--max", action="store_true")
    sp.add_argument("--cores", type=int, default=3)
    sp.add_argument("--cpu-start", type=int, default=None)
    sp.add_argument("--instance", type=int, default=0)
    sp.add_argument("--target-cpu", type=float, default=0.0)
    sp.add_argument("--max-sleep", type=float, default=15.0)
    sp.add_argument("--no-busy", action="store_true")
    sp.add_argument("--ocr-device", default=None, choices=["cpu", "gpu", "npu"])
    sp.add_argument("--embed-device", default=None, choices=["cpu", "gpu", "npu"])
    sp.add_argument("--audio-device", default=None, choices=["cpu", "gpu"])
    sp.set_defaults(func=cmd_low)

    sp = sub.add_parser("vector", help="高吞吐向量化工人（占用指定核心）")
    sp.add_argument("--cores", type=int, default=12)
    sp.add_argument("--threads", type=int, default=12)
    sp.add_argument("--batch", type=int, default=64)
    sp.add_argument("--sleep", type=float, default=1.0)
    sp.add_argument("--shard", type=int, default=0)
    sp.add_argument("--num-shards", type=int, default=1)
    sp.add_argument("--cpu-start", type=int, default=None)
    sp.add_argument("--min-free", type=float, default=4.0)
    sp.add_argument("--max-cpu", type=float, default=92.0)
    sp.add_argument("--target-cpu", type=float, default=0.0)
    sp.add_argument("--max-sleep", type=float, default=15.0)
    sp.set_defaults(func=cmd_vector)

    sp = sub.add_parser("progress", help="每分钟打印知识库任务进度")
    sp.add_argument("--interval", type=int, default=60)
    sp.add_argument("--once", action="store_true")
    sp.set_defaults(func=cmd_progress)

    sp = sub.add_parser("api", help="启动统一 HTTP API 服务")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.set_defaults(func=cmd_api)

    sp = sub.add_parser("mcp", help="启动 MCP 服务")
    sp.add_argument("--http", action="store_true")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("daemon", help="启动守护进程：监听 + OCR + 转写 + 向量化")
    sp.add_argument("--no-ocr", action="store_true")
    sp.add_argument("--no-transcribe", action="store_true")
    sp.add_argument("--no-embed", action="store_true")
    sp.add_argument("--no-screen", action="store_true")
    sp.add_argument("--no-wechat", action="store_true")
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("doctor", help="环境自检")
    sp.set_defaults(func=cmd_doctor)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)


if __name__ == "__main__":
    main()
