# 硬件加速实测结论（Core Ultra + Intel Arc 环境）

来源：Intel Core Ultra（16 核，带 NPU）+ Intel Arc B 系列独立显卡环境的实测。你的机器不同结论可能不同，**接入前先按同样方法复测**。

## 任务 × 设备矩阵

| 任务 | CPU | GPU（Arc，DirectML/Vulkan） | NPU（Intel AI Boost，DirectML） |
|---|---|---|---|
| OCR（PP-OCRv6） | **1.26s/页（最快，默认）** | 4.9s/页（小矩阵调度开销大，慢 4×） | 2.11s/页（fp32 模型，慢 1.7×） |
| 嵌入（bge-small-zh） | 48ms/批 | 92ms/批（模型太小） | **20ms/批（快 2.4×，默认）** |
| 转写（whisper small） | RTF 0.363 | **RTF 0.075（Vulkan，快 4.8×）** | 不适合（自回归小矩阵逐 token 开销） |
| 解析/Office/微信/屏幕 | ✅ 唯一实现 | — | — |

## 结论

- **OCR 必须留在 CPU**；装 onnxruntime-directml 后 rapidocr 会自动抢 GPU 且更慢——框架已锁 `use_dml: False`
- **NPU 只值得跑嵌入**（批量小卷积）；OCR/转写压到 NPU 反而更慢
- **GPU 只值得跑转写**；3 进程并发即打满（3D 引擎 97%），再多进程无收益
- 转写 GPU 安装：Const-me/Whisper CLI（`whisper-gpu/main.exe`）+ `ggml-small.bin`（约 466MB）；无 VAD，靠重复文本过滤（同文本超 5 次丢弃）

## 设备占用上限

- 后台自动任务：CPU ≤ `background_cpu_percent`（默认 10%，实测 6-8%），GPU/NPU ≤ `gpu_percent`/`npu_percent`（默认 10%，60 秒窗口时间占比，`kb/devices.py` 统一执行）
- **智能体检索/读取不受限**
- 多设备并发 OCR 会 DirectML 初始化挂起——OCR 任务有全局串行锁，默认只开一个 OCR 工人
