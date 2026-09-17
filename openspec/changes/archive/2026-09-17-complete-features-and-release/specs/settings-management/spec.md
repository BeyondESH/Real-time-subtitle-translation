# settings-management — 设置面板与配置同步

## ADDED Requirements

### Requirement: 设置窗口生命周期

托盘菜单"设置" SHALL 打开设置窗口，加载并展示当前配置。设置窗口 SHALL 单例（重复打开聚焦已有窗口）。保存 SHALL 写入 electron-store，取消 SHALL 丢弃改动。

#### Scenario: 打开设置

- **WHEN** 用户点击托盘菜单"设置"
- **THEN** 设置窗口打开，各控件展示当前已保存的配置值

#### Scenario: 取消不落盘

- **WHEN** 用户修改若干项后点击"取消"
- **THEN** electron-store 内容不变，字幕窗样式不变

### Requirement: 配置双源边界与同步

electron-store SHALL 是用户偏好（字幕样式、窗口位置、激活语言、快捷键、锁定状态）的唯一来源；config.yaml SHALL 仅作为后端管线默认值。前端 WebSocket 连接建立后 SHALL 发送 `config_sync` 消息（含 target_languages、active_language），后端 SHALL 以该消息覆盖自身对应配置。

#### Scenario: 启动同步

- **WHEN** 前端启动并连上后端 WebSocket
- **THEN** 后端收到 config_sync，后续翻译使用前端同步的目标语言与激活语言

### Requirement: 设置实时生效

保存设置后：字幕样式 SHALL 经 IPC 实时推送到字幕窗；目标语言、激活语言、Whisper 模型、音频源 SHALL 经 WebSocket 推送后端并在下一次处理周期生效，无需重启应用。模型与音频源变更 SHALL 有成功/失败回执。

#### Scenario: 切换音频源

- **WHEN** 用户在设置面板选择另一回环设备并保存
- **THEN** 后端切换到该设备继续捕获，失败时设置面板显示错误提示

### Requirement: 音频源枚举

设置面板的音频源下拉 SHALL 通过 WebSocket 请求/响应（`get_audio_sources`）从后端实时拉取设备列表，拉取失败 SHALL 显示可重试的错误态而非假数据。

#### Scenario: 拉取设备列表

- **WHEN** 设置窗口打开
- **THEN** 音频源下拉展示后端返回的全部回环设备名称

#### Scenario: 后端未连接

- **WHEN** 设置窗口打开时后端 WebSocket 未连接
- **THEN** 音频源下拉显示"后端未连接"并提供重试入口
