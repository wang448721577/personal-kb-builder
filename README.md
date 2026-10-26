# Personal KB Builder · 个人电脑知识库搭建技能

把任意用户的本地资料目录建成「全息数字记忆」：Office/PDF/图片 OCR/Excel 数据/微信聊天/屏幕记忆统一索引，混合检索带可核引用，任意智能体经 MCP/HTTP/CLI 使用，数据全本地。

## 特性

- 全盘资料索引（数万文档 / 数十万文本块的规模验证过）
- 中文优化：jieba 分词 FTS5 + bge-small-zh 向量 + RRF 混合排序
- 引用可核：页码 / 工作表 / 幻灯片 / 聊天时间，证据包一键生成
- Excel/CSV 直接 DuckDB SQL 分析
- 可选：微信聊天只读解密、屏幕记忆（Screenpipe）、音视频转写（GPU 4.8×）
- 资源友好：后台任务 CPU/GPU/NPU 各 ≤10%，智能体访问不限速
- 隐私：全本地、无遥测；加密/损坏文件自动跳过，源文件绝不修改

## 快速开始

见 [SKILL.md](SKILL.md)。最短路径：

```bat
:: 1. 复制 framework 到 D:\PersonalKB
:: 2. 写 config.yaml（root = 你的资料目录）
:: 3. 安装依赖并初始化
python -m pip install -r framework\requirements.txt
python -m kb init
python -m kb index
:: 4. 启动后台
cscript start_daemon.vbs
cscript start_low_worker.vbs
:: 5. 验证
kb.cmd search "测试关键词"
```

## 目录结构

```
SKILL.md                搭建主流程（任何智能体按此执行）
references/
  architecture.md       架构与设计决策
  operations.md         运维、各 Agent 接入、迁移备份
  hardware.md           GPU/NPU/CPU 实测与设备选择
framework/
  kb/                   知识库核心包（约 30 个模块）
  config.template.yaml  配置模板
  kb_mcp_launch.py      MCP stdio 启动器
  requirements.txt      依赖清单
  scripts/              后台启动脚本模板
```

## 许可

MIT。欢迎提 Issue/PR。
