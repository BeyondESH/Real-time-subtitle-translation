# overlay-window Specification

## Purpose
TBD - created by archiving change complete-features-and-release. Update Purpose after archive.
## Requirements
### Requirement: 锁定/解锁双模式

字幕窗 SHALL 有锁定与解锁两种模式。锁定（默认）：鼠标穿透，窗口不可交互。解锁：`setIgnoreMouseEvents(false)`，字幕区域支持系统级拖拽移动（`-webkit-app-region: drag`），窗口可缩放。模式 SHALL 可通过托盘菜单勾选与快捷键切换，并持久化到 electron-store。

#### Scenario: 解锁后拖拽

- **WHEN** 用户切换到解锁模式并拖拽字幕区域
- **THEN** 窗口跟随移动，松手后新位置写入 electron-store

#### Scenario: 锁定后穿透

- **WHEN** 用户切回锁定模式并点击字幕区域
- **THEN** 点击事件穿透到下层窗口，字幕窗不响应

#### Scenario: 重启保持

- **WHEN** 用户在解锁模式下退出并重启应用
- **THEN** 窗口位置与锁定状态保持退出前的值

### Requirement: 快捷键完整注册

应用 SHALL 从 electron-store 读取并注册全部全局快捷键：暂停/恢复、切换激活语言、切换 Whisper 模型、锁定/解锁。注册失败（按键被占用）SHALL 记日志并在设置面板可见提示，MUST NOT 静默失败。

#### Scenario: 三个宣传快捷键可用

- **WHEN** 用户依次按下 Ctrl+Shift+Space / Ctrl+Shift+L / Ctrl+Shift+M
- **THEN** 分别触发暂停恢复、激活语言轮换、Whisper 模型轮换，且后端行为同步发生

#### Scenario: 快捷键冲突

- **WHEN** 某快捷键被其他应用占用
- **THEN** 日志记录失败项，其余快捷键正常注册

### Requirement: 渲染进程安全边界

字幕窗与设置窗 SHALL 以 `contextIsolation: true`、`nodeIntegration: false` 运行，一切主进程交互 SHALL 经 preload 暴露的最小 API 面完成。渲染进程 MUST NOT 直接使用 `ipcRenderer`/`require`。

#### Scenario: 渲染进程无 node 权限

- **WHEN** 字幕窗或设置窗页面执行 `typeof require`
- **THEN** 结果为 `'undefined'`，页面功能仍完整可用

### Requirement: 托盘健壮性

托盘图标 SHALL 从随应用分发、保证存在的资源加载；图标加载失败 SHALL 记日志降级（无托盘继续运行），MUST NOT 导致启动崩溃。托盘菜单 SHALL 包含：显示/隐藏字幕、暂停/恢复、锁定位置、设置、退出。

#### Scenario: 正常启动

- **WHEN** 应用启动
- **THEN** 托盘图标正常显示，五项菜单可用

#### Scenario: 图标资源缺失

- **WHEN** 图标文件意外缺失
- **THEN** 应用继续运行且字幕窗可用，日志中出现托盘降级记录

