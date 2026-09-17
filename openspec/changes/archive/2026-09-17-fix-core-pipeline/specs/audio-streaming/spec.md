# audio-streaming — 音频流缓冲与切句

## ADDED Requirements

### Requirement: 音频缓冲与 VAD 语句切分

系统 SHALL 将捕获的音频块累积进容量可配置的环形缓冲区（默认 30 秒），并周期性（默认每 250ms）使用 faster-whisper 内置的 Silero VAD 对缓冲音频做语句边界检测。系统 SHALL 在以下任一条件满足时切出一条完整语句送交识别：语句尾部静音持续达到 `vad.min_silence_duration_ms`（默认 600ms）；或语句长度达到 `pipeline.max_utterance_s`（默认 15s）强制切分。切出的语句 MUST 包含至少 300ms 的头部预卷音频。系统 MUST NOT 将未经切分的原始音频块直接送交 ASR。

#### Scenario: 正常说话切句

- **WHEN** 用户连续说话 3 秒后停止，静音持续超过 600ms
- **THEN** 系统切出一条包含该段语音（含头部预卷）的语句并送交 ASR，且该语句时长约 3.3 秒

#### Scenario: 超长语句强制切分

- **WHEN** 用户连续说话超过 15 秒且无足够静音间隙
- **THEN** 系统在 15 秒处强制切出一条语句送交 ASR，后续语音继续累积为下一条语句

#### Scenario: 纯静音不触发识别

- **WHEN** 音频源持续播放但内容仅为静音或低于 VAD 阈值的背景噪声
- **THEN** 系统不产生任何语句切出，ASR 不被调用

### Requirement: 背压控制

语句队列 SHALL 为有界队列（默认深度 8，可配置）。当队列满时，系统 SHALL 丢弃最旧的待处理语句、记录 WARNING 日志，并向所有 WebSocket 客户端广播 `pipeline_warning` 消息。系统 MUST NOT 为每个音频块无界创建并发任务。

#### Scenario: ASR 处理慢于实时输入

- **WHEN** ASR 处理速度持续慢于语句产生速度，队列达到上限
- **THEN** 最旧的待处理语句被丢弃，客户端收到 `type=pipeline_warning` 的消息，系统内存占用保持稳定

#### Scenario: 负载恢复后继续工作

- **WHEN** 过载状态结束，ASR 处理速度恢复
- **THEN** 新产生的语句正常入队并被处理，无需重启

### Requirement: 暂停语义

暂停期间录音线程 SHALL 继续读取音频流但直接丢弃（防止 WASAPI 缓冲区溢出），音频 MUST NOT 进入缓冲区，ASR 与翻译 MUST NOT 被调用。恢复后系统 SHALL 从当时的实况音频开始处理，不补播暂停期间的内容。

#### Scenario: 暂停期间零识别开销

- **WHEN** 用户触发暂停并持续 60 秒
- **THEN** 该期间 ASR 与翻译调用次数为零，CPU/GPU 推理负载降至空闲水位

#### Scenario: 恢复后即时可用

- **WHEN** 用户从暂停状态恢复并立即说话
- **THEN** 恢复后的语音被正常切句识别，且不包含暂停期间残留的旧音频

### Requirement: 重采样质量

当音频设备实际采样率与目标采样率（16kHz）不一致时，系统 SHALL 使用 soxr 多相滤波进行重采样。系统 MUST NOT 使用线性插值重采样。

#### Scenario: 48kHz 设备降采样

- **WHEN** 音频源以 48000Hz 输出，目标采样率为 16000Hz
- **THEN** 重采样输出长度符合采样率比例，且无线性插值引入的高频镜像噪声

### Requirement: 音频源选择

系统 SHALL 能枚举全部回环音频设备（id、名称、声道、采样率），默认选用第一个回环设备。用户可按设备 id 切换音频源；非法 id SHALL 抛出明确错误。README 中"按应用捕获音频"的描述与实际能力不符，相关宣传 MUST 移除（文档修正在 change complete-features-and-release 完成）。

#### Scenario: 枚举音频源

- **WHEN** 前端请求音频源列表
- **THEN** 返回全部回环设备的 id、name、is_loopback、channels（soundcard 不暴露采样率，采集按目标采样率开流）

#### Scenario: 非法音频源

- **WHEN** 以不存在的设备 id 设置音频源
- **THEN** 系统抛出包含该 id 的 ValueError，当前音频源保持不变
