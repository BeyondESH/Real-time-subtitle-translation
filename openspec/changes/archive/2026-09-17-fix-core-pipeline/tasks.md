# fix-core-pipeline — 实现任务

## 1. 依赖与配置

- [x] 1.1 `requirements.txt`：新增 `soxr>=0.3`、`pytest`、`pytest-asyncio`；移除 `langdetect`、`asyncio-throttle`；`faster-whisper` 下限提升到 `>=1.0`
- [x] 1.2 `config.yaml`：新增 `pipeline:` 段（buffer_seconds/tick_ms/max_utterance_s/queue_size）、`vad:` 段（threshold/min_silence_duration_ms/speech_pad_ms）、`translation.lazy_load`、`translation.preload_primary`；`main.py` 默认值同步

## 2. 音频管线重构

- [x] 2.1 新增 `backend/audio_buffer.py`：`RingBuffer`（定长、线程安全 append/snapshot）+ `UtteranceSegmenter`（封装 faster-whisper `get_speech_timestamps`，按 D1 规则切句，含 300ms 头卷）
- [x] 2.2 `audio_capture.py`：回调改为只写 RingBuffer；暂停时继续读取但丢弃；重采样替换为 `soxr.resample`；删除 `_resample_audio` 线性插值实现
- [x] 2.3 新增 `backend/pipeline_worker.py`：有界 `asyncio.Queue` + 消费协程（ASR→翻译→WS 发送）；队满丢最旧 + 广播 `pipeline_warning`
- [x] 2.4 `main.py`：装配 capture→buffer→（250ms tick 切句）→queue→worker 链路；保存 `asyncio.get_running_loop()` 引用

## 3. 模型生命周期修复

- [x] 3.1 `main.py` 的 `_on_model_progress` 改为 `loop.call_soon_threadsafe` 调度，消除 worker 线程 `create_task` 崩溃
- [x] 3.2 `translator.py`：拆分 `initialize()` 为 `ensure_primary()` / `ensure_nllb()`，`asyncio.Lock` 串行化；`translate()` 按语言对触发懒加载并等待
- [x] 3.3 主模型后台预载：ASR 就绪后 `create_task` 预载（受 `preload_primary` 开关控制），预载失败降级为纯懒加载并记日志
- [x] 3.4 `asr_engine.change_model` 加锁；非法模型名通过 WS 返回 `{type:'error', code:'invalid_model'}`

## 4. 语言处理纠偏

- [x] 4.1 新增 `backend/language_codes.py`：`normalize_lang()` 纯函数 + NLLB 显式映射表（从 translator.py 迁入并扩充 zh 变体）
- [x] 4.2 `translator.py`：删除 langdetect 导入与 `detect_language` 热路径调用；源语言一律取 ASR 结果；删除 `f'{lang}_Latn'` 拼接，未映射语言对返回占位串
- [x] 4.3 `main.py`：`_on_audio_data` 链路确认 source_language 只来自 ASR

## 5. 控制链路贯通

- [x] 5.1 `overlay.html`：暂停/恢复 IPC 触发时发送 WS `{type:'control', action:'pause'|'resume'}`
- [x] 5.2 `main.py` `_on_transcription`：未知 action 记日志忽略；change_model 错误回执
- [x] 5.3 `audio_capture.py` 增加 `discard` 标志位与 pause/resume 联动

## 6. 测试体系

- [x] 6.1 `tests/test_segmenter.py`：合成音频（静音/语音/静音拼接、20s 长语音）验证切句边界、头卷、强制切分
- [x] 6.2 `tests/test_pipeline_worker.py`：队满丢最旧 + `pipeline_warning` 广播；恢复后正常消费
- [x] 6.3 `tests/test_language_codes.py`：zh 变体归一、未映射语言降级、同语言跳过
- [x] 6.4 `tests/test_progress_callback.py`：在无线程事件循环中触发回调不抛异常（回归 D3 bug）
- [x] 6.5 重写 `tests/test_integration.py`：mock ASR/Translator 跑通 capture→subtitle 消息全链路
- [x] 6.6 新增 `pytest.ini`（asyncio_mode=auto），`pytest` 全绿

## 7. 收尾

- [x] 7.1 清理 `translator.py` 重复 import 与未使用的 `NllbTokenizer`
- [ ] 7.2 真实音源回归：播放日语视频 2 分钟，字幕延迟 ≤2.5s 且无明显漏句
