# complete-features-and-release — 补齐宣传功能并打通打包分发

## Why

README 承诺的功能有半数是空壳：设置面板存在但未接线（`TODO`）、切换语言快捷键是空实现、切换模型快捷键未注册、窗口拖拽被全局鼠标穿透堵死、多目标语言被硬编码为只显示中文。同时 Electron 端存在安全反模式（`nodeIntegration: true` + `contextIsolation: false` + preload 未挂载），打包配置双份冲突（package.json 与 electron-builder.yml），Electron 从不拉起后端进程，托盘图标文件不存在导致启动即抛异常——安装包即便打出来也无法开箱即用。依赖前置 change `fix-core-pipeline`（管线先修活）。

## What Changes

- **Electron 安全加固**：挂载 preload、`contextIsolation: true`、`nodeIntegration: false`，渲染进程只经 `electronAPI` 桥访问 IPC
- **窗口交互重做**：锁定（鼠标穿透）/ 解锁（可拖拽移动、可交互）双模式，托盘菜单与快捷键切换，状态持久化
- **设置面板落地**：托盘打开设置窗口；读写 electron-store；样式实时推送到字幕窗；音频源/模型/语言经 WebSocket 同步后端
- **目标语言激活机制**：后端只翻译当前激活语言；`Ctrl+Shift+L` 轮换激活语言；字幕窗按激活语言渲染，消灭 `zh` 硬编码；显示模式（原文+译文/仅译文）真正生效
- **补齐第三个快捷键**：`Ctrl+Shift+M` 注册并联通 change_model（依赖 fix-core-pipeline 的后端实现）
- **WebSocket 协议扩展**：增加带 `id` 关联的请求/响应模式（get_audio_sources、get_config），复用现有 `send_to`
- **打包分发修复**：electron-builder.yml 为唯一构建配置源（删除 package.json `build` 键）；Electron 自动拉起/守护/结束后端进程并做 WS 健康检查；PyInstaller spec 补齐 ctranslate2/tokenizers/faster-whisper 资产（silero_vad.onnx）/sentencepiece/soxr 收集项；不再打包空 models/ 目录；移除 mac/linux 无效目标；补齐图标资源
- **可观测性**：后端滚动文件日志（用户数据目录），前端日志落盘，级别可配
- **文档同步**：README 修正"按应用捕获"、"<1s 延迟"等不实宣传，补充设置面板与分发说明

## Capabilities

### New Capabilities

- `subtitle-display`: 激活目标语言渲染、显示模式、字幕样式实时应用
- `settings-management`: 设置窗口生命周期、electron-store 持久化、前后端配置同步与音频源枚举
- `overlay-window`: 锁定/解锁交互模型、完整快捷键注册、渲染进程安全边界、托盘健壮性
- `release-packaging`: 构建配置单一来源、后端进程托管、PyInstaller 完整性、文件日志、安装包内容与冒烟验证

### Modified Capabilities

- `pipeline-control`: 新增 `set_language`（切换激活目标语言）action，扩展原协议（fix-core-pipeline 归档后此能力已存在）

## Impact

- **前端**：`main.ts`（设置窗口、锁定模式、后端进程托管、托盘）、`preload.ts`（扩展 API 面）、`overlay.html`（electronAPI 化、激活语言渲染）、`settings.html`（IPC 接线）
- **后端**：`websocket_server.py`（请求/响应模式）、`main.py`（新 action、config_sync、文件日志）
- **构建**：`electron-builder.yml`、`package.json`、`build.spec`、`build.bat`、新增图标资源
- **依赖**：新增 `electron-log`
- **顺序**：必须在 `fix-core-pipeline` 完成并归档后实施
