# fix-core-pipeline — 技术设计

## Context

当前数据流是「录音线程每 64ms → `run_coroutine_threadsafe` → `transcribe` → `translate` → WS 广播」，没有任何缓冲、分段与背压。Whisper 对 64ms 音频做 VAD 只会返回空，整条管线空转；每个 chunk 新建协程，堆积无上限。

约束与前提：

- faster-whisper ≥ 0.10 自带 Silero VAD（`faster_whisper.vad.get_speech_timestamps` / `VadModel`，资产文件 `silero_vad.onnx` 随 wheel 分发），用户已明确采用此方案，不引入第三方 VAD 库
- 目标端到端延迟：语句说完后 1~2.5 秒内出字幕（替代 README 中不切实际的 "<1s" 宣传）
- Windows 10/11，GPU 可选，CPU 必须可用（int8）

## Goals / Non-Goals

**Goals:**

- 连续语音被切成语义完整的语句并逐句识别，静音期间零 ASR 调用
- 任意环节慢于实时输入时，系统降级为"丢旧保新"，永不 OOM
- 首次运行下载模型不再崩溃，进度可见
- 暂停/恢复/换模型从前端按键到后端行为全链路生效
- 管线核心逻辑（切句、背压、语言映射）有可运行的自动化测试

**Non-Goals:**

- 不做滑窗流式局部字幕（partial/hypothesis 修正）——复杂度与收益不成比例，留待后续
- 不做按进程音频捕获（soundcard 不支持，README 相关宣传在 change 2 中修正）
- 不改 Electron 前端架构与打包（change 2）

## Decisions

### D1: 语句切分——环形缓冲 + faster-whisper 内置 VAD

新增 `audio_buffer.py`：

```
录音线程                    事件循环线程
  │ 64ms chunk               │
  ├─► RingBuffer(30s) ───────┤
                             │ 每 250ms tick:
                             │   get_speech_timestamps(buffer)
                             │   ├─ 句尾静音 ≥600ms → 切出语句
                             │   └─ 句长 ≥15s → 强制切出
                             ▼
                    utterance queue (maxsize=8)
                             ▼
                    PipelineWorker: ASR → 翻译 → WS
```

- VAD 参数复用 faster-whisper 的 `VadOptions` 语义：`min_silence_duration_ms=600`、`speech_pad_ms=200`、`threshold=0.5`，全部入 `config.yaml` 的 `vad:` 段
- 语句切出时带 ≥300ms 头部预卷，避免切掉起音
- 备选方案（已否决）：固定窗口 + `transcribe(vad_filter=True)`——窗口边界切词、重叠区重复输出；本方案直接用同一内置 VAD 模型先定位句界，识别时 `vad_filter=False`，精度与成本都更优

### D2: 背压——有界队列，丢最旧

捕获回调只做一件事：`queue.put_nowait(utterance)`；满则丢**最旧**一条、记 WARNING、向 WS 广播 `pipeline_warning`（前端可显示"处理过载"）。严禁每个音频块 `create_task`。队列深度 8 可配置（`pipeline.queue_size`）。

### D3: 进度回调线程安全

`main.py` 启动时保存 `self._loop = asyncio.get_running_loop()`；`_on_model_progress` 内用 `self._loop.call_soon_threadsafe(...)` 调度到事件循环后再 `create_task` 发 WS。ASREngine/Translator 的 `_report_progress` 保持同步签名不变。

### D4: 模型分级加载

| 模型 | 时机 | 理由 |
|---|---|---|
| Whisper ASR | 启动即加载（eager） | 没有它整条管线无法工作 |
| 日中主模型 | ASR 就绪后后台 task 预载 | 主要场景，启动不被 300MB 下载卡住 |
| NLLB 全语种 | 首次用到非日中语言对时懒加载 | 1.3GB，多数用户用不到 |

加载由 `asyncio.Lock` 串行化；`translate()` 遇到模型未就绪 → 等待同一锁（期间进度消息持续推给前端）；`config.yaml` 加 `translation.lazy_load: true`、`translation.preload_primary: true` 两个开关。

### D5: 暂停语义

暂停期间录音线程**继续读流但直接丢弃**（避免 WASAPI 缓冲区溢出导致恢复时爆音），不进入 RingBuffer，ASR/翻译完全静默。恢复后从最新实况音频开始，不回放暂停期内容。

### D6: 语言认定与代码映射

- 源语言唯一依据：faster-whisper 的 `info.language`（ISO 639-1，稳定输出 `zh/ja/en`）；从管线中移除 langdetect 二次猜测（短句不可靠），依赖一并删除
- 规范化层：`zh-cn/zh-tw/zh-hans/zh-hant → zh` 等，集中在 `language-handling` 的一个纯函数，便于测试
- NLLB 目标代码**只查 `LANGUAGE_MAP`**，查不到返回"未支持的语言对"占位串——删除现有 `f'{lang}_Latn'` 拼接（`zh-cn_Latn` 会让 NLLB 直接抛错）

### D7: 重采样

`soxr.resample(x, in_rate, out_rate, quality='HQ')` 替换 `np.interp`。WASAPI 按 16kHz 直接开流时此路径不触发，但设备不支持 16kHz 时（部分蓝牙设备）质量差异直接影响识别率。新增 `soxr>=0.3` 依赖。

### D8: 控制协议

沿用现有 `{type:'control', action, ...}` 形态，补齐实现：

- `pause` / `resume`：前端 overlay 在收到 IPC 后发送；后端切换捕获丢弃标志
- `change_model`：携带 `model_size`；非法值回 `{type:'error', code:'invalid_model'}`；切换由锁串行化，切换期间语句按 D2 规则丢弃
- 未知 action：记日志、不回执、不崩溃

## Risks / Trade-offs

- [faster-whisper 不同小版本 VAD 模块 API 存在差异] → 实现时先 `from faster_whisper.vad import get_speech_timestamps` 做导入探测；若目标版本无此符号，降级为定窗 + `transcribe(vad_filter=True)`，接口保持不变；requirements 将 faster-whisper 下限锁到 ≥1.0
- [VAD 每 tick 扫全缓冲区的 CPU 开销] → Silero CPU 上单帧 <1ms，30s 缓冲区约千帧，250ms 周期下占用可忽略；仍提供 `pipeline.buffer_seconds` 下调空间
- [懒加载让首条非日中翻译等待模型下载（最长分钟级）] → 进度消息全程推送，前端有进度 UI；可用 `lazy_load: false` 回退到全量预载
- [丢旧保新在过载时丢句子] → 这是实时系统的正确降级方向；`pipeline_warning` 让用户可感知，文档建议降低模型档位
- [打包时 `silero_vad.onnx` 需随 PyInstaller 分发] → 已在 change 2 的 release-packaging 能力中列为收集项，本 change 不处理

## Migration Plan

纯行为修复，无数据迁移。配置向后兼容：新增的 `pipeline:`/`vad:` 段缺省时用代码内默认值；`langdetect` 从依赖删除前确认无其他引用。

## Open Questions

- VAD 切句在强背景音乐场景（游戏/直播）的误切率，需要实测调 `threshold`——验收阶段用真实音源回归
