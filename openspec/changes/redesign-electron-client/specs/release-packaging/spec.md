# release-packaging 增量规格

## MODIFIED Requirements

### Requirement: 构建配置单一来源

electron-builder 配置 SHALL 只存在于 `electron-builder.yml`；`package.json` MUST NOT 含 `build` 键。构建目标 SHALL 仅保留 Windows NSIS（x64）。前端构建体系换为 electron-vite 后：yml 的 `files` 与入口路径 SHALL 指向其标准产物目录（`out/**`），SHALL 新增 `publish` 段（GitHub provider，供自动更新使用），并与 electron-vite 配置、better-sqlite3 原生模块打包 MUST 无冲突（产物可独立运行，含原生模块重编译）。

#### Scenario: 配置无歧义

- **WHEN** 执行 `npm run dist`
- **THEN** electron-builder 仅读取 electron-builder.yml，electron-vite 产物（main/preload/renderer）完整入包，无配置冲突警告

#### Scenario: 打包产物含原生模块

- **WHEN** 在干净 Windows 环境安装打包产物并启动
- **THEN** better-sqlite3 历史库功能可用（会话可读写），无 ABI/绑定错误

### Requirement: 后端进程托管

打包后的应用启动时 SHALL 自动拉起随包分发的后端可执行文件，并以指数退避轮询 WebSocket 完成健康检查（上限 90 秒）。应用退出时 SHALL 结束后端进程树。后端异常退出 SHALL 托盘提示并支持一键重启。健康检查超时 SHALL 弹出含日志路径的错误对话框并允许重试。模型下载进度 SHALL 在主窗口可见（首启引导下载页，或引导跳过/非首启时经直播流占位状态与状态胶囊条呈现）。

#### Scenario: 开箱即用

- **WHEN** 用户安装后首次启动应用
- **THEN** 无需手动开任何终端，后端被自动拉起，模型下载进度在主窗口引导页可见，就绪后字幕正常工作

#### Scenario: 后端崩溃恢复

- **WHEN** 运行中后端进程被杀
- **THEN** 托盘出现提示，用户点击重启后服务恢复，主窗口连接状态经历 down→reconnecting→open 并恢复正常

### Requirement: 安装包内容与首装体验

安装包 SHALL 包含后端可执行文件、默认 config.yaml 模板与图标资源；SHALL NOT 内嵌模型文件（首次运行下载到用户缓存目录）。config.yaml 模板 SHALL 仅含后端消费段（audio/pipeline/vad/asr/translation/websocket），前端专属偏好段（subtitle/system/shortcuts）不再分发。首次启动 SHALL 在用户数据目录生成可编辑的 config.yaml 副本，此后以副本为准，前端 MUST NOT 覆盖已存在副本。

#### Scenario: 首装流程

- **WHEN** 干净机器完成安装并启动
- **THEN** userData 生成仅含后端段的 config.yaml 副本，模型开始下载且进度在主窗口可见，安装包体积不含模型（安装包 <500MB）

## ADDED Requirements

### Requirement: 自动更新

应用 SHALL 集成 electron-updater（GitHub Releases 源，配置于 electron-builder.yml publish 段与 NSIS 目标）：启动后延迟静默检查更新；设置"通用"分段 SHALL 提供手动"检查更新"并反馈结果（已是最新 / 新版本号）；发现新版本 SHALL 后台下载并在主窗口状态区显示下载进度；下载完成 SHALL 以托盘通知提示"重启安装"，用户可推迟至下次退出时自动应用。更新检查或下载失败（断网、无发布源）MUST NOT 反复弹窗打扰，SHALL 静默记日志并保持手动检查入口可用。

#### Scenario: 发现并安装新版本

- **WHEN** GitHub Releases 存在高于当前版本的新版且用户网络正常
- **THEN** 应用后台下载并显示进度，完成后托盘通知提示重启安装；重启后版本更新且用户配置与历史数据保留

#### Scenario: 已是最新

- **WHEN** 用户在设置页点击"检查更新"且无新版本
- **THEN** 界面反馈"已是最新版本"，无任何下载行为

#### Scenario: 断网静默降级

- **WHEN** 自动检查更新时无网络连接
- **THEN** 无错误弹窗，日志记录失败原因，应用全部本地功能不受影响
