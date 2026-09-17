# overlay-window 增量规格

## MODIFIED Requirements

### Requirement: 托盘健壮性

托盘图标 SHALL 从随应用分发、保证存在的资源加载；图标加载失败 SHALL 记日志降级（无托盘继续运行），MUST NOT 导致启动崩溃。托盘菜单 SHALL 包含：显示主窗口、显示/隐藏字幕、暂停/恢复、锁定字幕位置、重启后端服务、设置、退出。"显示主窗口"在主窗口已显示时 SHALL 聚焦还原；"设置"SHALL 打开主窗口并深链到设置页（主窗口未显示时先显示）。

#### Scenario: 正常启动

- **WHEN** 应用启动
- **THEN** 托盘图标正常显示，七项菜单可用

#### Scenario: 图标资源缺失

- **WHEN** 图标文件意外缺失
- **THEN** 应用继续运行且字幕窗可用，日志中出现托盘降级记录

#### Scenario: 托盘打开设置

- **WHEN** 主窗口处于隐藏状态时用户点击托盘菜单"设置"
- **THEN** 主窗口显示并打开设置页，字幕悬浮窗状态不受影响

## ADDED Requirements

### Requirement: 字幕窗数据经主进程转发

字幕悬浮窗 MUST NOT 直连后端 WebSocket。字幕、状态（暂停/锁定/连接/下载进度）、告警 SHALL 全部经 preload 桥订阅主进程 Gateway/AppState 的广播获得；悬浮窗发起的任何控制（如解锁状态下的交互）SHALL 走 dispatch 动作通道。悬浮窗的隐藏、关闭或销毁 MUST NOT 影响后端连接与其它窗口的数据接收。

#### Scenario: 悬浮窗重建后状态完整

- **WHEN** 悬浮窗因外观预设切换被销毁重建
- **THEN** 重建后的窗口立即获得全量状态快照并继续接收字幕流，期间到达的字幕不丢失展示（新句正常显示）

#### Scenario: 悬浮窗隐藏不影响通道

- **WHEN** 用户隐藏字幕悬浮窗
- **THEN** Gateway 连接保持，主窗口直播流与历史记录功能完全不受影响

### Requirement: 外观预设

字幕悬浮窗 SHALL 支持两种外观预设：纯文字（默认，透明窗口，现状语义）与毛玻璃胶囊（Windows 11 `backgroundMaterial: 'acrylic'`，圆角内容区）。预设 SHALL 在设置"字幕外观"分段选择并持久化；切换预设 MAY 经窗口重建实现，重建 SHALL 恢复位置、锁定态与可见性，用户无内容丢失感知。不支持亚克力材质的系统（如 Windows 10）SHALL 禁用该选项并展示原因说明，MUST NOT 提供会静默失败的开关。锁定穿透语义（setIgnoreMouseEvents）在纯文字预设下 SHALL 完整保持；毛玻璃预设下若与 forward 穿透不兼容，锁定态 SHALL 至少保证不可拖拽且不响应交互，并在设置项旁如实说明。

#### Scenario: Win11 切换毛玻璃

- **WHEN** 用户在 Windows 11 上将预设切换为毛玻璃胶囊
- **THEN** 悬浮窗以亚克力材质重建，位置与锁定态保持，字幕渲染与动效正常

#### Scenario: Win10 选项禁用

- **WHEN** 应用运行在 Windows 10 上且用户打开字幕外观设置
- **THEN** 毛玻璃选项呈禁用态并显示"需要 Windows 11"说明，纯文字预设正常工作
