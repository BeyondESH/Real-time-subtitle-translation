# client-gateway-state 增量规格（新能力）

## ADDED Requirements

### Requirement: 主进程唯一 WebSocket 连接

Electron 主进程 SHALL 持有到后端 WebSocket 服务的唯一连接（Gateway）。任何渲染进程 MUST NOT 直连后端 WebSocket。Gateway 断线 SHALL 以指数退避自动重连（250ms 起，上限 30s），连接状态（connecting/open/reconnecting/down）SHALL 进入 AppState 并广播。字幕窗隐藏、关闭或销毁 MUST NOT 影响该连接的生命周期。后端进程由 BackendManager 托管（既有职责）：端口探活持续失败时由 BackendManager 重启后端进程，Gateway 只负责 WS 层重连，两者状态互不冒充。

#### Scenario: 字幕窗隐藏后控制通道仍在

- **WHEN** 用户隐藏字幕悬浮窗且此刻模型正在下载
- **THEN** model_progress 消息继续被 Gateway 接收并写入 AppState，主窗口随时可见进度

#### Scenario: 后端进程被杀后恢复

- **WHEN** 后端进程被外部杀死后又重新启动
- **THEN** Gateway 在退避周期内自动重连成功，AppState 的 connection 依次经历 down→reconnecting→open，所有窗口状态显示同步

### Requirement: 请求/响应桥

Gateway SHALL 提供 `request(method, params) → Promise<result>` 能力：为每个请求分配唯一 id 并记入 pending map，收到 `{type:'response'}` 按 id 路由解析；超时（默认 10s，可配置）SHALL 以超时错误拒绝；后端返回 `ok:false` SHALL 以携带错误信息的类型化错误拒绝。渲染进程 SHALL 仅通过 preload 暴露的 invoke 通道发起请求，MUST NOT 自行构造 WS 帧。

#### Scenario: 音频源列表经桥获取

- **WHEN** 设置页音频分段请求 get_audio_sources
- **THEN** Gateway 发出带 id 的 request 帧，收到匹配 response 后以设备数组 resolve，UI 渲染列表

#### Scenario: 请求超时

- **WHEN** 后端 10 秒内未对某 request 响应
- **THEN** Promise 以超时错误拒绝，pending map 中该项被清除，UI 显示可重试错误态

### Requirement: AppState 单一状态机

主进程 SHALL 持有唯一的应用状态 AppState，至少包含：connection、capture（running/paused）、vad（speech/silence）、model、modelDownload（名称+进度或 null）、activeLanguage、targetLanguages、audioSource、locked、overlayVisible、lastWarning、droppedCount。状态变更 SHALL 只能通过 `dispatch(action)` 进行；托盘菜单、全局快捷键、所有窗口 UI MUST 经同一 dispatch 入口，MUST NOT 各自维护平行状态。每次变更 SHALL 计算增量 patch 并经 IPC 广播到所有存活窗口；新打开的窗口 SHALL 先获取全量状态快照再收增量。

#### Scenario: 三源暂停一致

- **WHEN** 用户分别通过全局快捷键、托盘菜单、主窗口暂停按钮触发暂停
- **THEN** 三种路径产生相同的状态迁移，托盘标签、主窗口状态点、悬浮窗暂停徽标、后端捕获暂停全部一致

#### Scenario: 新窗口全量同步

- **WHEN** 主窗口在应用运行中途打开
- **THEN** 窗口首帧即获得完整状态快照（含进行中的模型下载进度），随后正常接收增量 patch

#### Scenario: 状态广播合帧

- **WHEN** 短时间内发生多次状态变更（如 vad 翻转与字幕到达交叠）
- **THEN** patch 以合帧方式广播（约 16ms 批量），渲染层不出现逐字段闪烁

### Requirement: 广播事件路由

Gateway 收到的后端广播 SHALL 按类型路由：`subtitle` → 写入历史存储（见 session-history）并经专用 IPC 通道分发给订阅窗口（主窗口直播流、悬浮字幕窗）；`model_progress` → 更新 AppState.modelDownload；`pipeline_warning` → 更新 lastWarning/droppedCount 并分发告警事件；`error` → 记日志并分发类型化错误事件；`vad_state` → 更新 AppState.vad。任何单一窗口的销毁 MUST NOT 影响路由与其它窗口接收。

#### Scenario: 双窗口同收字幕

- **WHEN** 一条 subtitle 到达且主窗口与悬浮窗同时打开
- **THEN** 两个窗口均渲染该条字幕，历史记录写入一份且不重复

### Requirement: 连接建立后的偏好对齐

Gateway 在 WS 连接建立（含重连成功）后 SHALL 主动将后端运行态对齐到 electron-store 中的用户偏好：发送 `config_sync`（target_languages、active_language）；当 store 中 Whisper 模型与后端当前模型不一致时发送 `change_model`；当 store 中音频源与后端当前音频源不一致时发送 `set_audio_source`。对齐动作 SHALL 幂等，重复连接不产生副作用。渲染窗口 MUST NOT 自行发送 config_sync。

#### Scenario: 重连后语言恢复

- **WHEN** 用户曾把激活语言切为 en，随后后端重启、Gateway 重连成功
- **THEN** Gateway 自动重发 config_sync，后端激活语言恢复为 en，无需用户操作

#### Scenario: 无差异不发消息

- **WHEN** 连接建立且 store 偏好与后端运行态完全一致
- **THEN** 仅发送 config_sync，不产生多余的 change_model/set_audio_source 控制消息
