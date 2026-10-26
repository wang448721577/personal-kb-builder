---
name: personal-kb-builder
description: 为任何用户从零搭建「个人电脑知识库」——索引本地 Office/PDF/图片/Excel/聊天/屏幕记忆，提供 MCP/HTTP/CLI 统一接口供任意智能体检索、写报告、做数据分析。当用户说"搭建个人知识库/本地资料检索/知识库构建/全盘索引/按我的资料写报告"时使用。
---

# 个人知识库搭建（Personal KB Builder）

把用户电脑的资料目录建成一个**本地、永远更新、任意智能体可用的知识库**：混合检索（BM25+向量）、带页码/工作表/时间戳的可核引用、Excel 直接 SQL 分析、可选微信聊天与屏幕记忆接入，全部数据不出本机。

## 前置条件

- Windows 10/11，Python 3.11+（推荐 3.13），约 10GB 磁盘（含向量库与模型）
- 可选：Intel Arc GPU（转写加速）、带 NPU 的 CPU（嵌入加速）——没有也能跑
- 目标目录：用户要索引的资料根目录（默认 `D:\MyDocuments` 之类）

## 快速开始（约 15 分钟）

1. **建主目录**：`D:\PersonalKB`（代码与数据库都放这里；资料目录不用动）
2. **复制框架**：把本技能目录下的 `framework/kb/` 整体拷入 `D:\PersonalKB\kb\`；把 `framework/kb_mcp_launch.py`、`framework/scripts/*` 拷到 `D:\PersonalKB\`
3. **写配置**：`cp framework/config.template.yaml D:\PersonalKB\config.yaml`，至少改三行：
   - `home: D:/PersonalKB`
   - `root: <用户的资料根目录>`（只索引这个目录，原文件只读不修改）
   - `exclude_dirs`：按需排除（安装包、缓存、聊天记录原目录等）
4. **装依赖**：`python -m pip install -r framework/requirements.txt`（安装 `onnxruntime-directml` 后 OCR 会自动想用 GPU，框架已强制 `use_dml: False` 保持 CPU，勿改）
5. **建库索引**：`cd D:\PersonalKB && python -m kb init && python -m kb index --full`
   - 大目录先跑 `python -m kb index` 增量；问题文件（加密/损坏/需密码）自动跳过并永不自动重试
6. **启动后台**：`cscript start_daemon.vbs`（文件监听+屏幕记忆+微信+嵌入），`cscript start_low_worker.vbs`（OCR+向量化低负载）
7. **验证**：`python -m kb status`；`kb.cmd search "关键词"` 应有结果

## 配置速查（config.yaml 关键项）

| 键 | 含义 | 默认 |
|---|---|---|
| `root` | 索引的资料根目录 | 必改 |
| `exclude_dirs` | 排除目录（安装包/缓存等） | 见模板 |
| `keep_deleted: true` | 源文件删除后**库内保留记录** | true（尊重用户"删文件不删知识"） |
| `background_cpu_percent: 10` | 所有自动任务 CPU 合计上限 | 10（智能体查询不受限） |
| `gpu_percent / npu_percent: 10` | GPU/NPU 后台占用上限（时间占比） | 10 |
| `ocr.device` | OCR 设备 cpu/gpu/npu | cpu（实测最快） |
| `embedding.device` | 嵌入设备 npu/cpu/gpu | npu |
| `audio.enabled` | 是否转写音视频 | true；用户若说"不识别音视频"改 false |
| `screenpipe.*` / `wechat.*` | 屏幕记忆/微信聊天（可选模块） | 见模板 |

## 可选模块

- **OCR**：图片与扫描件自动排队识别，CPU 上约 1.2s/页；`--ocr-device` 可切 GPU/NPU（实测更慢，默认 CPU）
- **音视频转写**：CPU faster-whisper 或 GPU whisper（RTF 0.075，需另下载 `whisper-gpu` CLI 与 ggml 模型，见 `references/hardware.md`）；NPU 不适合转写
- **微信聊天**：只读解密本机微信 4.x 数据库，按"会话+月份"聚合，`wechat://` 路径；敏感数据只在本机
- **屏幕记忆（Screenpipe）**：文本模式采集窗口/应用/URL；默认"采到文字不截图、采不到才截图回退 OCR"；托盘 `screenpipe_tray.pyw` 带设置与开机自启；注意截图/录音默认关闭、遥测关闭
- **截图回退**：`python -m kb shots`，文本读不到的窗口才截一张图并 OCR 入库

## Agent 接入（三种接口，任选）

- **MCP（推荐）**：stdio 服务器，8 个工具（search / get_context / read_document / list_documents / query_table / build_evidence_pack / timeline / stats）
  - Qoder/Hermes/Claude Code 等：`mcp add personal-kb -- python D:\PersonalKB\kb_mcp_launch.py`（各平台命令略有差异，见 `references/operations.md`）
- **HTTP**：`pythonw -m kb api`，默认 `http://127.0.0.1:8766`，文档在 `/docs`；跨机必须设 token
- **CLI**：`kb.cmd search/read/table/evidence/status/timeline ...`
- 检索引用规范：正文引用 `（《文件名》第N页/工作表「X」/第N页幻灯片/[时间]）`，数字必须用 `query_table` 对 Excel 复核，找不到就说"知识库中未找到"，严禁编造

## 资源与设备策略（本框架实测结论）

- 自动任务（守护/OCR/向量/截图回退）合计 CPU ≤10%，由 `kb/bg_cpu.py` 统一节流；智能体检索读取**不限速**
- 设备分工实测：转写 GPU（4.8×CPU）> 嵌入 NPU（2.4×CPU）> OCR CPU（GPU/NPU 反而慢 1.7–4×）
- OCR 任务全局串行（`data/ocr_job.lock`）；多设备并发 OCR 会 DML 初始化挂起，不要开多 OCR 工人

## 安全红线

- 资料根目录**只读**：解析用独立临时/缓存目录，绝不修改、删除或上传源文件
- 微信/屏幕记忆等敏感数据只在本地库；无 ACL，接入的每个智能体看到同样内容——授权前提醒用户
- 打不开/加密/损坏文件：立即关闭、标记跳过、不自动重试（`--retry-problems` 才重试）
- 默认不联网（模型缓存在本地）；远程访问必须 token

## 常见问题

- 搜索没结果 → 先 `kb status` 看索引进度；新文件要等守护进程（5 秒防抖）
- 检索明显过期 → 手动 `python -m kb index` 增量一次
- 后台卡住/重启 → 删 `data/kb-*.pid` 后重跑对应 start 脚本；队列卡 running → `python -m kb doctor`
- 换设备/换盘 → 见 `references/operations.md` 的迁移步骤

## 参考文档

- `references/architecture.md`：架构、存储、检索与更新机制
- `references/operations.md`：启停、各 Agent 接入命令、迁移备份
- `references/hardware.md`：GPU/NPU 实测数据与设备选择
