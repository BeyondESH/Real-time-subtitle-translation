# fix-core-pipeline — 修复核心管线，让软件真正可用

## Why

当前实现存在三个致命缺陷，导致软件按现状**无法正常产出字幕**：

1. 音频管线无缓冲/切句策略——每 64ms 音频块直接调 Whisper 推理，VAD 必然判为静音，识别结果永远为空，且协程无界堆积最终耗尽内存；
2. 模型下载进度回调在 worker 线程里调用 `asyncio.create_task`，首次下载模型时初始化必然崩溃（被误报为"加载模型失败"）；
3. 控制链路断裂——前端暂停/切换模型从不发送 WebSocket 消息，后端空跑，GPU 白烧。

另有语言代码映射错误（`zh-cn` → 拼出非法 NLLB 代码）、线性插值重采样损伤识别精度、三个翻译/识别模型全量预加载拖垮启动速度等次生问题。这些不修，上层功能无从谈起。

## What Changes

- **重构音频管线**：新增环形缓冲区 + 基于 faster-whisper 内置 Silero VAD 的语句切分器，整句送 ASR；捕获与识别之间有界队列背压，过载时丢最旧语句并告警
- **修复进度回调线程 bug**：worker 线程经 `loop.call_soon_threadsafe` 切回事件循环再发消息
- **贯通控制链路**：前端暂停/恢复/切换模型通过 WebSocket control 消息送达后端并真正生效
- **语言处理纠偏**：源语言以 Whisper 输出为唯一依据，规范化 `zh-cn/zh-tw`，NLLB 语言代码只查表不再拼接；移除 langdetect 依赖
- **重采样升级**：线性插值换为 soxr 多相滤波
- **翻译模型分级加载**：ASR 启动即加载；日中主模型后台预载；NLLB 首次用到才加载；加载期间翻译请求串行等待
- **建立测试体系**：切句器/背压/语言规范化单元测试 + 管线集成测试（mock 模型）
- 依赖变更：新增 `soxr`、`pytest`、`pytest-asyncio`；移除 `langdetect`、`asyncio-throttle`

## Capabilities

### New Capabilities

- `audio-streaming`: 音频采集后的缓冲、VAD 语句切分、背压控制、暂停语义、重采样与音频源选择
- `model-lifecycle`: ASR/翻译模型的加载时机（eager/预载/懒加载）、线程安全的进度上报、模型切换的原子性、设备回退
- `pipeline-control`: 前端到后端的控制消息协议（pause/resume/change_model）及其端到端语义
- `language-handling`: 源语言认定、语言代码规范化、翻译语言对映射与跳过规则

### Modified Capabilities

（无——openspec/specs/ 当前为空，全部为新增能力）

## Impact

- **后端**：`audio_capture.py`（回调改喂缓冲区、暂停丢弃语义）、`asr_engine.py`（语句级输入）、`translator.py`（懒加载、语言映射）、`main.py`（管线装配、线程安全回调）、新增 `audio_buffer.py` / `pipeline_worker.py`
- **前端**：`overlay.html` 增加 control 消息发送
- **依赖**：`backend/requirements.txt` 增删
- **配置**：`config.yaml` 新增 `pipeline:`、`vad:` 配置段与 `translation.lazy_load/preload_primary`
- **测试**：`backend/tests/` 扩充为真正的单元+集成测试
- **不涉及**：设置面板、拖拽、打包分发（在 change `complete-features-and-release` 中处理）
