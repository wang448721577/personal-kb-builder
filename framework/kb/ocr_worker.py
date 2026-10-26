from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: ocr_worker.py <file> <pages_csv> <out.json>", file=sys.stderr)
        return 2
    try:
        import psutil
        p = psutil.Process()
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        mask = p.cpu_affinity()
        if len(mask) > 1:
            p.cpu_affinity([mask[p.pid % len(mask)]])
    except Exception:
        pass
    src, pages_csv, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from kb.config import load_config
    from kb.ocr import ocr_pdf_pages
    pages = [int(x) for x in pages_csv.split(",") if x.strip()]
    try:
        results = ocr_pdf_pages(src, pages, load_config())
        out.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
