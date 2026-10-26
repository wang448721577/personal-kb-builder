# 架构说明

## 总览

```
资料目录(只读) ──watchdog──▶ 守护进程(kb daemon) ──解析──▶ SQLite(documents/chunks/FTS5)
                               │ 问题文件→跳过            │
图片/扫描件 ──▶ OCR队列(jobs) ◀─ 低负载工人(kb low)      │
新文本/OCR结果 ──▶ 嵌入队列 ──▶ LanceDB(向量, 512维)      │
屏幕记忆(Screenpipe) ──每5分钟──▶ 屏幕记忆集合            │
微信(只读解密) ──每30分钟──▶ 微信聊天集合                 │
智能体 ──MCP/HTTP/CLI──▶ 混合检索(RRF: FTS5+向量) ──▶ 带引用结果
```

## 存储

- **SQLite（WAL）**：`documents`（元数据+状态）、`chunks`（正文块）、`chunks_fts`（FTS5+jieba 分词）、`jobs`（OCR/转写队列）、`doc_tables`（Excel 导出表）、`aliases`、`index_runs`、`meta`
- **LanceDB**：float32 512 维向量，与 chunks 一一对应；文档删除时同步清理
- **表数据**：Excel/CSV 在索引时导出到 `data/tables/`，查询时用 DuckDB 跑 SQL
- 关键索引：`chunks(doc_id, embedded)` 复合索引（曾因缺它导致文档状态更新全表扫描、每批 55 秒——新增索引后为 0 秒）

## 更新机制

1. **文件监听**：watchdog 递归监听 root，新建/修改 5 秒防抖后增量解析；删除/移动事件按 `keep_deleted` 处理（true=库内保留）
2. **周期对账**：每 `rescan_hours`（6 小时）快哈希全扫 + 清理缺失文件（受 keep_deleted 控制）
3. **队列接力**：解析产出自动入队——OCR（图片/扫描件）→低负载工人；嵌入→NPU/CPU 工人
4. **微信**：按 `wechat_last_ts` 游标增量；**屏幕记忆**：按 5 分钟桶聚合、窗口内去重

## 检索

- 混合排序：FTS5（BM25，jieba 中文分词）+ 向量（bge-small-zh，查询加前缀）+ RRF 融合
- 加分项：标题命中加权；目录页/点线块降权（`text.count("·")+text.count("....")>20 → ×0.2`）
- 引用：每个命中带 `doc_id/chunk_id/路径/页码或工作表或时间`，用 `kb_get_context`/`kb_read_document` 核对原文

## 设计决策（血泪教训）

- PDF 解析/OCR/Office 转换全部走**子进程+超时+PID 清理**：PyMuPDF 多线程崩溃、Word COM 死锁都源于进程内混跑
- 问题文件策略：打不开/加密/损坏 → 立即关闭、`status='skipped'`、绝不自动重试（隐私+稳定性）
- 去重：`quick_hash`（大小+mtime）判变化，`content_hash` 判重复，重复文件折叠到 canonical
- 向量分片：`chunk_id % num_shards`，多进程不重复嵌入
- **onnxruntime-directml 安装后 rapidocr 会自动优先 DML 且更慢 4×**——必须显式 `use_dml: False`
- 多进程同时写 LanceDB/SQLite 会锁竞争：向量化用「单进程宽批」（256/批）远快于 5 分片小批
