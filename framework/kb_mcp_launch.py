from __future__ import annotations

import os
import sys
from pathlib import Path

# 知识库主目录：默认 D:\PersonalKB，可用环境变量 KB_HOME 覆盖
HOME = Path(os.environ.get("KB_HOME", r"D:\PersonalKB"))
sys.path.insert(0, str(HOME))
os.environ.setdefault("KB_CONFIG", str(HOME / "config.yaml"))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from kb.mcp_server import run_server
run_server()
