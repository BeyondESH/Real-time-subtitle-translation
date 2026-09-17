# Proposal: redesign-electron-client

## Why

现有 Electron 客户端"能跑但形态薄"：WebSocket 连接长在字幕悬浮窗的渲染进程里（隐藏/关闭字幕窗即失去控制通道与告警）、暂停/语言/模型状态分裂在主进程、overlay、托盘三处、配置存在 electron-store 与 config.yaml 两个真相源且 `subtitle`/`system`/`shortcuts` 段是后端不消费的死配置、渲染层为无构建的内联 HTML/JS。产品面缺主窗口（用户锚点只有托盘图标）、字幕看过即失（无历史/导出）、开机自启配置了但未实现。本变更在保留 Electron 与现有后端协议的基础上，重构客户端架构并以 ChatGPT/Ollama 桌面端的现代简约风格重建全部界面。

## What Changes

- **架构**：WebSocket 连接上移至 Electron 主进程（唯一 Gateway，含断线指数退避重连与请求/响应 pending map）；主进程持有唯一 AppState 状态机，状态变更统一向所有窗口广播；托盘/快捷键/任意 UI 操作全部收敛为对 AppState 的 action，消灭三处状态分裂。
- **界面全面重设计**（现代简约、暗色优先、单色基调 + 一个低饱和品牌色，对齐 ChatGPT 桌面端观感）：
  - 新增**主窗口**：无边框 + Windows 原生按钮覆盖（WCO `titleBarOverlay`）；左侧会话侧栏（新建/历史/搜索），主区为 ChatGPT 式"直播字幕流"（原文+译文卡片、时间戳/语言对/延迟脚注、VAD"聆听中"脉冲指示），底部为状态胶囊条（音频源·模型·语言·连接状态），设置改为应用内分段页面（通用/字幕外观/音频/模型/快捷键/高级），含首次运行引导流程（欢迎→音频源选择→模型下载进度→完成）。
  - **字幕悬浮窗翻新**：保留透明穿透/锁定语义，新增入场动效（fade + 上浮 250ms）、暂停徽标、丢句琥珀告警条（2s 自动消失）、"纯文字/毛玻璃胶囊"两种外观预设（毛玻璃用 Win11 `backgroundMaterial`，不支持时自动降级纯文字）。
- **会话历史与导出**：主进程 SQLite 持久化会话与语句；默认"每次启动一个自动会话 + 手动新建会话 + 静音超阈值自动切分（可关）"；支持会话重命名/删除/全文搜索/回放；导出 SRT、TXT（双语）、Markdown、JSON。
- **配置收敛**（**BREAKING**）：electron-store 成为用户偏好唯一真相源；主进程启动后端前物化仅含后端消费段（audio/pipeline/vad/asr/translation/websocket）的 config.yaml，`subtitle`/`system`/`shortcuts` 僵尸段从配置模板移除；独立设置窗口移除（托盘"设置"改开主窗口设置页）。
- **后端协议小幅扩展**（向后兼容）：`subtitle` 消息透传切句时间戳 `ts_start`/`ts_end`（SRT 精确导出的前提）；新增 `vad_state`（speech/silence）轻量广播（驱动"聆听中"指示）。
- **产品完善**：开机自启实装（`setLoginItemSettings`，设置页开关）；自动更新（electron-updater + NSIS 差分升级）；前端构建体系换 electron-vite，渲染层 React 18 + Tailwind（自建薄组件层，shadcn 风格）。
- **明确不在本期范围**：麦克风输入模式、流式部分字幕（faster-whisper partial）、Web/移动端客户端——另行提案。

## Capabilities

### New Capabilities

- `main-window`: 主窗口壳（WCO 无边框、侧栏、路由）、直播字幕流视图、状态胶囊条、会话侧栏、应用内设置页（含开机自启）、首次运行引导流程。
- `client-gateway-state`: 主进程唯一 WS Gateway（重连/pending map/事件分发）、AppState 单一状态机、跨窗口状态广播、动作统一派发（托盘/快捷键/UI 同源）。
- `session-history`: 会话与语句数据模型、SQLite 持久化、会话切分规则（自动/手动/静音超时）、搜索、历史回放、多格式导出（SRT/TXT/MD/JSON）、会话管理。
- `design-system`: 设计 token（色彩/字体/圆角/动效）、暗色优先与亮色双主题、组件一致性约束，覆盖主窗口与字幕悬浮窗。

### Modified Capabilities

- `overlay-window`: 字幕窗 MUST NOT 直连 WebSocket（改为订阅主进程转发）；新增外观预设（纯文字/毛玻璃胶囊）要求；锁定/解锁、快捷键、安全边界、托盘要求保持。
- `subtitle-display`: 新增动效与状态可见性要求（入场动效、暂停徽标、丢句告警条）；既有渲染规则（激活语言、显示模式、样式实时应用、最近 N 条）不变。
- `settings-management`: 设置窗生命周期要求改为"主窗口内设置页"（托盘深链直达）；`config_sync` 的发送主体与时机改为 Gateway（连接建立时与配置变更时）；配置双源边界改为"electron-store 唯一真相 + 启动前物化后端专用 config.yaml"；新增开机自启持久化与生效要求。
- `pipeline-control`: 新增 `vad_state` 广播要求；`subtitle` 消息新增 `ts_start`/`ts_end` 字段要求；既有 control 协议与回执行为不变。
- `release-packaging`: 前端构建产物结构与构建配置随 electron-vite 更新（`files`/入口路径）；新增自动更新要求（更新检查、下载进度提示、重启安装）。

## Impact

- **前端（重建）**：`frontend/src/` 重组为 `main/`（gateway、state、backend-manager、history-db、config、windows）、`preload/`、`renderer/`（React：app 路由、features、components/ui、overlay 独立入口、styles/tokens）；删除 `settings.html`，`overlay.html` 重写。依赖新增 React 18、electron-vite、Tailwind CSS、better-sqlite3、electron-updater、lucide-react。
- **后端（小改）**：`pipeline_worker.py`（subtitle 消息补时间戳）、`audio_buffer.py`/`main.py`（utterance 时间透传、`vad_state` 广播）；`backend/tests/` 协议测试相应扩展；PyInstaller 产物不变。
- **配置与构建**：根目录 `config.yaml` 模板瘦身；`electron-builder.yml` 的 `files` 段与 publish/更新配置更新；`build.bat` 适配新构建命令。
- **文档**：README 界面预览、项目结构、配置说明、快捷键表更新。
- **兼容性**：WS 协议为纯增量扩展，旧后端 + 新客户端可运行（时间戳缺失时 SRT 导出降级为接收时刻近似）；新后端 + 旧前端不受影响。
- **风险**：React 化后主窗口冷启动体积/内存增加（Electron 28 下可接受）；毛玻璃在 Win10 无 `backgroundMaterial` 支持（自动降级已覆盖）；better-sqlite3 需随 Electron ABI 重编译（electron-builder 自动处理，需验证打包）。
