from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path


def _clean_content(content: str, mtype: str) -> str:
    content = content or ""
    if "<appmsg" in content or "<msg>" in content:
        parts = []
        for tag in ("title", "des", "url", "filename"):
            m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", content, re.S)
            if m:
                v = html.unescape(m.group(1)).strip()
                if v and v not in parts:
                    parts.append(v)
        if parts:
            return " | ".join(parts)[:2000]
    if mtype != "文本" and (not content.strip() or content.strip() == f"[{mtype}]"):
        return f"[{mtype}]"
    content = re.sub(r"^[\w@.\-]{6,}:\n", "", content)
    return content[:3000]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--since", type=int, default=0)
    ap.add_argument("--recent", type=int, default=0, help="0 = full history")
    ap.add_argument("--max-sessions", type=int, default=2000)
    ap.add_argument("--max-per-session", type=int, default=100000)
    ap.add_argument("--exclude-prefix", action="append", default=[])
    ap.add_argument("--exclude-chat", action="append", default=[])
    args = ap.parse_args()

    tmp = os.environ.get("KB_WECHAT_TMP")
    if tmp:
        Path(tmp).mkdir(parents=True, exist_ok=True)
        os.environ["TEMP"] = tmp
        os.environ["TMP"] = tmp

    from wechatauto.db import WeChatDB

    db = WeChatDB()
    self_info = db.get_self_info() or {}
    self_username = self_info.get("username") or ""
    sessions = db.get_sessions(limit=args.max_sessions)
    exclude_prefix = [x.lower() for x in args.exclude_prefix if x]
    exclude_chat = [x.lower() for x in args.exclude_chat if x]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    name_cache: dict[str, str] = {}
    count_sessions = 0
    count_messages = 0
    started = time.time()

    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "account", "username": self_username,
                            "nick_name": self_info.get("nick_name", "")},
                           ensure_ascii=False) + "\n")
        for s in sessions:
            user = str(s.get("username") or "")
            if not user:
                continue
            lower = user.lower()
            if args.since and int(s.get("last_time") or 0) < args.since - 86400:
                continue
            if any(lower.startswith(p) for p in exclude_prefix):
                continue
            if any(x in lower for x in exclude_chat):
                continue
            nickname = db.get_nickname(user) or user
            if any(x in nickname.lower() for x in exclude_chat):
                continue
            if user not in name_cache:
                name_cache[user] = nickname

            msgs = []
            if args.recent and args.recent > 0:
                msgs = db.get_messages(user, limit=args.recent, offset=0) or []
            else:
                offset = 0
                while offset < args.max_per_session:
                    page = db.get_messages(user, limit=1000, offset=offset) or []
                    if not page:
                        break
                    msgs.extend(page)
                    offset += len(page)
                    oldest = min((int(m.get("create_time") or 0) for m in page), default=0)
                    if args.since and oldest and oldest < args.since:
                        break
                    if len(page) < 1000:
                        break
            if not msgs:
                continue
            filtered = []
            for m in msgs:
                try:
                    ts = int(m.get("create_time") or 0)
                except Exception:
                    ts = 0
                if args.since and ts < args.since:
                    continue
                content = _clean_content(str(m.get("content") or ""), str(m.get("type") or ""))
                if not content.strip():
                    continue
                sender = str(m.get("sender_username") or "")
                if sender and sender not in name_cache:
                    try:
                        name_cache[sender] = db.get_nickname(sender) or sender
                    except Exception:
                        name_cache[sender] = sender
                obj = {
                    "local_id": m.get("local_id"),
                    "sort_seq": m.get("sort_seq"),
                    "ts": ts,
                    "msg_type": m.get("type") or "",
                    "sender_username": sender,
                    "sender_name": name_cache.get(sender, sender),
                    "content": content,
                }
                filtered.append(obj)
            if not filtered:
                continue
            filtered.sort(key=lambda x: (int(x.get("ts") or 0), int(x.get("local_id") or 0)))
            f.write(json.dumps({"type": "session", "username": user,
                                "nickname": nickname, "is_group": user.endswith("@chatroom"),
                                "messages": len(filtered)}, ensure_ascii=False) + "\n")
            for obj in filtered:
                f.write(json.dumps({"type": "message", "username": user, **obj},
                                   ensure_ascii=False) + "\n")
            count_sessions += 1
            count_messages += len(filtered)
            print(f"[wechat] {nickname[:30]} {len(filtered)} msgs", file=sys.stderr)
    print(json.dumps({"sessions": count_sessions, "messages": count_messages,
                      "elapsed_sec": round(time.time() - started, 1),
                      "out": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
