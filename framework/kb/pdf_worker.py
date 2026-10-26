from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: pdf_worker.py <input.pdf> <output.json>", file=sys.stderr)
        return 2
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from kb.config import load_config
    from kb.parsers.pdf_fast import parse_pdf_fast
    try:
        doc = parse_pdf_fast(src, load_config())
        out.write_text(json.dumps(dataclasses.asdict(doc), ensure_ascii=False),
                       encoding="utf-8")
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
