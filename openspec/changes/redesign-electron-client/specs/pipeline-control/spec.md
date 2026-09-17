# pipeline-control 增量规格

## ADDED Requirements

### Requirement: subtitle 消息时间戳

`subtitle` 广播 SHALL 携带 `ts_start` 与 `ts_end` 字段（浮点秒），值为该语句在切句器时间线上的起止时刻，来源于 VAD 切句结果并随语句对象透传到广播。时间戳字段 MUST NOT 改变既有字段（original、source_language、active_language、translations）的语义；旧客户端收到新增字段 MUST 不受影响。

#### Scenario: 消息形状

- **WHEN** 一条语句完成识别与翻译并广播
- **THEN** 消息包含 type=subtitle 及 ts_start、ts_end、original、source_language、active_language、translations 字段，ts_end 大于 ts_start 且差值约等于语句时长

#### Scenario: 旧客户端兼容

- **WHEN** 不识别时间戳字段的旧版前端收到新版后端广播
- **THEN** 字幕显示功能与升级前完全一致，无解析错误

### Requirement: VAD 状态广播

后端 SHALL 仅在切句周期检测到 VAD 语音状态翻转时广播 `{type:'vad_state', state:'speech'|'silence'}`，MUST NOT 周期性或按音频块广播。speech 起始 SHALL 立即广播；silence 起始 SHALL 去抖（持续静音约 300ms）后广播，避免语句间隙抖动。无客户端连接时广播跳过 MUST NOT 影响切句与识别管线。

#### Scenario: 翻转事件

- **WHEN** 静音环境中用户开始说话，随后停顿超过去抖窗口
- **THEN** 客户端先后收到 vad_state=speech 与 vad_state=silence 各一条，期间无重复消息

#### Scenario: 短间隙不抖动

- **WHEN** 说话中出现 200ms 的短停顿后继续说话
- **THEN** 不产生 silence 广播（被去抖吞并），客户端聆听指示保持连续

#### Scenario: 无客户端安全

- **WHEN** 无任何 WebSocket 客户端连接时音频持续产生 VAD 翻转
- **THEN** 管线正常运行，无消息发送错误与性能劣化
