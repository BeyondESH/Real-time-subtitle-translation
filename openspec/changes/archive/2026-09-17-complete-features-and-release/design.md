# complete-features-and-release — 技术设计

## Context

前端四个文件里，`settings.html` 完整但从未被打开；`preload.ts` 写好了却没挂载；`main.ts` 的托盘"设置"菜单是 TODO；字幕窗全局鼠标穿透导致拖拽无从下手。构建侧，`package.json` 的 `build` 键与 `electron-builder.yml` 并存（实际生效的是 yml，但双份配置必然腐化）；`build.spec` 缺少 ctranslate2 等关键收集项且引用了不存在的图标；`frontend/src/` 下没有任何图标文件，`new Tray()` 启动即抛异常；Electron 与后端是完全脱节的双进程，安装包用户拿到手根本跑不起来。

约束：

- 依赖 change `fix-core-pipeline` 已落地的控制协议与懒加载
- 目标平台只有 Windows（WASAPI 限定），mac/linux 目标属于无效配置
- 模型运行时下载到 `~/.cache/subtitle-translator`，安装包不内嵌模型（控制体积）

## Goals / Non-Goals

**Goals:**

- README 宣传的每一项功能都可真实操作
- 渲染进程符合 Electron 官方安全基线
- `build.bat` 一键产出可安装、可开箱运行的 NSIS 安装包（首次运行自动下载模型，进度可见）
- 出问题时用户数据目录里有日志可查

**Non-Goals:**

- 不做自动更新（electron-updater 属后续增强）
- 不做代码签名（需要证书，用户自行补 `win.certificateFile`）
- 不内嵌模型文件到安装包（5GB 体积不可接受；离线部署留作后续可选方案）

## Decisions

### D1: 渲染进程安全边界

`webPreferences` 改为 `contextIsolation: true, nodeIntegration: false, preload: preload.js`。`overlay.html` 全部 `ipcRenderer` 调用迁移到 `window.electronAPI`（preload.ts 扩建）。WebSocket 客户端留在渲染进程（浏览器原生 API，无需 node）。settings.html 同样走 preload 桥。

### D2: 锁定/解锁窗口交互

放弃"穿透模式下做拖拽"的死结，采用双模式：

- **锁定（默认）**：`setIgnoreMouseEvents(true, {forward: true})`，字幕窗纯展示、点击穿透
- **解锁**：托盘菜单"锁定字幕位置"勾选项 / 快捷键 `Ctrl+Shift+D` 切换；解锁时 `setIgnoreMouseEvents(false)`，容器启用 `-webkit-app-region: drag`（系统级拖拽，无需自实现 mousemove IPC），窗口边缘可缩放

状态持久化到 electron-store。备选方案（已否决）：mouseenter 自动解锁——透明窗口的 enter/leave 事件在穿透态不可靠。

### D3: 配置单一职责边界

- **electron-store**：一切用户偏好（字幕样式、窗口位置、激活语言、快捷键、锁定状态）——唯一 UI 配置源
- **config.yaml**：后端管线默认值（采样率、VAD、队列、模型名）——安装时复制到 userData，用户可改
- **启动握手**：前端 WS 连接成功后发送 `{type:'config_sync', target_languages, active_language}`；后端以此为准运行，消除双轨漂移

### D4: 激活目标语言

后端维护 `active_target_language`（默认 target_languages 首项），**只翻译激活语言**——多目标全翻是白白放大 NLLB 推理延迟。`Ctrl+Shift+L` → 前端发 `control/set_language`（循环取值）→ 后端更新并在后续 subtitle 消息的 `translations` 中只含激活语言；overlay 渲染 `translations[data.active_language]`，消息体自带 `active_language` 字段，前端不做任何语言硬编码。

### D5: WebSocket 请求/响应扩展

现有协议只有广播与被动接收。为设置面板的"音频源下拉"增加请求/响应：客户端发 `{type:'request', id, method:'get_audio_sources'}`，服务端用现有 `send_to` 回 `{type:'response', id, result|error}`。id 用 UUID。仅新增两个 method（`get_audio_sources`、`get_config`），不泛化成 RPC 框架。

### D6: 后端进程托管

- packaged：`process.resourcesPath/backend/SubtitleTranslator.exe`；dev：`python backend/main.py`
- 启动后轮询 WS（指数退避，最长 90s，覆盖模型加载）；超时弹错误对话框（附日志路径）并允许重试
- `app.on('will-quit')` 杀进程树；后端异常退出时托盘气泡提示并可一键重启
- 端口冲突：8765 被占时后端启动失败，健康检查捕获后提示用户改 `websocket.port`

### D7: PyInstaller 修复

- `collect_all('ctranslate2')`、`collect_all('tokenizers')`、`collect_data_files('faster_whisper')`（关键：`silero_vad.onnx`）、`collect_all('sentencepiece')`、hiddenimport 补 `soxr`、删 `langdetect`
- `datas` 移除空 `../models`；`config.yaml` 照常打包为只读默认模板，运行时优先读 userData 副本
- `console` 改为构建变量控制：发布 False，调试传 `-- --console`（build.bat 加开关）
- 图标：新增 `frontend/src/icon.png`（托盘）与 `icon.ico`（安装包），纳入构建前检查清单

### D8: 文件日志

后端：`RotatingFileHandler`（userData/logs/backend.log，3×2MB），级别读 config；前端：`electron-log` 同目录 frontend.log。打包后控制台不可见，文件日志是唯一的排障通道。

## Risks / Trade-offs

- [contextIsolation 切换导致 overlay.html 现有 IPC 调用全断] → 一次性迁移并由"字幕正常显示+拖拽可用"两个手测项验收；preload API 面保持最小
- [后端进程托管的启动时序（模型加载可能数分钟）] → 健康检查窗口 90s + 模型下载进度消息已复用 fix-core-pipeline 的机制，用户有明确反馈
- [只翻译激活语言改变了"多语言同屏"的潜在用法] → README 从未承诺多语同屏；需要时在设置里加回多选（协议已兼容）
- [PyInstaller 打包 torch/transformers 体积（解压后 ~2GB）] → 接受现状，文档注明；upx 保持开启但排除 torch DLL（upx 压缩大 DLL 易损坏）
- [图标为程序生成的基础样式] → 任务中生成可识别的占位图标，后续可替换

## Migration Plan

配置迁移：检测到旧 electron-store 缺 `activeLanguage`/`locked` 键时写入默认值。无用户数据破坏性变更。灰度策略：本地 build.bat 出包 → 干净 Windows 虚拟机/沙盒首装验证（首次下载模型流程）→ 发布。

## Open Questions

- 首次运行模型下载总量 ~2GB（ASR base + 日中主模型），是否需要在安装向导中预置"下载最小模型集"说明页——随设置面板一起做，验收时定稿文案
