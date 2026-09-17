# pipeline-control — 增量：激活语言与音频源控制

## ADDED Requirements

### Requirement: 激活目标语言控制

控制协议 SHALL 新增 `set_language` action，格式 `{type:'control', action:'set_language', language: <code>}`。后端收到后 SHALL 将其设为激活目标语言，且仅当该语言在目标语言列表内时生效；非法语言 SHALL 回 `{type:'error', code:'invalid_language'}` 回执。后续 subtitle 消息 SHALL 携带 `active_language` 字段，且 `translations` 仅含激活语言的译文。

#### Scenario: 切换激活语言

- **WHEN** 目标语言列表为 [zh, en]，收到 `set_language` en
- **THEN** 后续 subtitle 消息的 active_language 为 en，translations 只含 en 译文

#### Scenario: 非法语言回执

- **WHEN** 收到 `set_language` 且语言不在目标列表
- **THEN** 客户端收到 invalid_language 错误回执，激活语言保持不变

### Requirement: 音频源切换控制

控制协议 SHALL 新增 `set_audio_source` action，格式 `{type:'control', action:'set_audio_source', source_id: <id>}`。后端 SHALL 切换到指定回环设备；捕获运行中切换时 SHALL 重启捕获流。设备 id 非法 SHALL 回 `{type:'error', code:'invalid_audio_source'}` 回执。

#### Scenario: 运行中切换音频源

- **WHEN** 捕获运行中收到合法的 set_audio_source
- **THEN** 捕获流在新设备上重启，管线不中断

#### Scenario: 非法音频源

- **WHEN** 收到不存在设备 id 的 set_audio_source
- **THEN** 客户端收到 invalid_audio_source 错误回执，当前捕获不受影响
