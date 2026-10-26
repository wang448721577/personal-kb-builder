# 运维手册

## 启停

| 操作 | 命令 |
|---|---|
| 启动守护（监听+屏幕+微信+嵌入） | `cscript start_daemon.vbs` |
| 启动低负载工人（OCR+嵌入） | `cscript start_low_worker.vbs` |
| 启动 HTTP API | `pythonw start_api.pyw` |
| 启动进度日志 | `cscript start_progress.vbs` |
| 手动增量索引 | `python -m kb index` |
| 手动全量索引 | `python -m kb index --full` |
| 状态 | `python -m kb status` |
| 体检修复 | `python -m kb doctor` |
| 强制清理已删除文件记录 | `python -m kb prune --force`（默认遵守 keep_deleted 不清理） |
| 停止全部后台 | 结束 pythonw 的 `-m kb daemon/low/vector/progress` 进程；删除 `data/kb-*.pid`；`UPDATE jobs SET state='pending' WHERE state='running'` |

## 各 Agent 接入命令（MCP stdio）

- **Hermes**：`hermes mcp add personal-kb --command python --args <路径>\kb_mcp_launch.py`；验证 `hermes mcp test personal-kb`
- **Claude Code**：`claude mcp add personal-kb -- python <路径>\kb_mcp_launch.py`
- **Qoder/Cursor 等**：在 MCP 设置中添加 stdio 服务器，command=python，args=kb_mcp_launch.py（按各应用 UI 填写）
- **通用**：任何支持 MCP 的软件都可用；不支持 MCP 的用 HTTP API（8766）或 CLI

## 队列与进度

- `logs/progress.log` 每 60 秒一行：文档数/向量进度/OCR 待办/CPU 内存
- `data/progress.json` 供程序读取
- 队列卡住：查 `jobs` 表 `state='running'` 超过 2 小时自动回 pending；失败 2 次置 failed（不自动重试）

## 备份与迁移

- **备份**：`data/kb.sqlite*`（含 -wal/-shm，先停止写入）+ `data/lancedb/` + `config.yaml`；模型在 `data/models/` 可重新下载
- **迁移**：整体复制 `D:\PersonalKB` 到新盘 → 改 `config.yaml` 的 home/root → 启动即用（路径都从配置读取）
- **升级框架**：用新版本 `framework/kb/` 覆盖旧 `kb/`（先备份）

## 资源限制调整

- 临时全速：`background_cpu_percent: 0`（或改大）；用完记得改回
- 只要 CPU 池：`cscript start_low_worker.vbs` 并停用嵌入
- 向量化大追赶：单进程宽批 `python -m kb vector --cores 15 --threads 12 --batch 256 --target-cpu 85`（把 background_cpu_percent 临时置 0 再跑）
