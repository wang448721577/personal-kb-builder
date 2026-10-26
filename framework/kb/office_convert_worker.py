from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: office_convert_worker.py <input> <output> <kind>", file=sys.stderr)
        return 2
    src, out, kind = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from kb.parsers.office import _save_with_office
    try:
        _save_with_office(src, out, kind)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
