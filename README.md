# Real-time Subtitle Translator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Electron](https://img.shields.io/badge/Electron-28-green.svg)](https://www.electronjs.org/)
[![Whisper](https://img.shields.io/badge/Whisper-faster--whisper-orange.svg)](https://github.com/SYSTRAN/faster-whisper)

基于 OpenAI Whisper 的实时字幕翻译软件：捕获系统音频 → 语音识别 → 多语种翻译 → 透明悬浮字幕 + ChatGPT 式主窗口（直播流 / 会话历史 / 搜索 / 导出）。完全离线可用。

[中文](#中文) | [English](#english)

---

## 中文

### 功能特性（2.0）

- **主窗口** — ChatGPT 式布局：直播字幕流、会话侧栏（今天/昨天/更早）、状态胶囊条（音频源·模型·语言·连接状态就地切换）、应用内设置页（六分段，改动即生效）
- **实时音频捕获** — WASAPI 回环捕获系统音频，设备可选；多显示器：悬浮窗可指定显示器、按显示器记忆位置、越界自动回收
- **语音识别** — faster-whisper + 内置 Silero VAD 切句，语句结束约 1~2.5 秒出字幕；"聆听中"实时脉冲指示
- **多语种翻译** — 日中专用模型 + NLLB-200 全语种（懒加载）
- **悬浮字幕窗** — 锁定（鼠标穿透）/解锁（拖拽）双模式、始终置顶；入场动效；暂停徽标；过载告警细条；纯文字 / 毛玻璃胶囊（Windows 11 亚克力）双外观预设
- **会话历史** — SQLite 本地持久化：每场直播/会议一个会话，自动以首句命名；全文搜索（原文+译文）；回放定位；导出 **SRT / TXT 双语 / Markdown / JSON**（后端时间戳透传，SRT 时间轴精确）
- **全局快捷键** — 暂停/恢复、切换语言、切换模型、锁定窗口；**支持自定义重录**，注册失败（被占用）在设置页可见警示
- **首次运行引导** — 欢迎 → 音频源选择 → 模型下载进度 → 完成（可跳过，下载转后台）
- **自动更新** — electron-updater + GitHub Releases：静默检查、后台下载、托盘通知重启安装（可推迟到退出时自动安装）
- **开机自启** — 系统登录项实装，状态与系统真实状态自动校准
- **暗/亮双主题** — 暗色优先，可跟随系统，含 WCO 原生标题栏按钮配色同步
- **离线可用** — 模型本地运行，首次自动下载，之后无需网络
- **架构**（2.0 重构）— 主进程唯一 WebSocket Gateway（指数退避重连）+ AppState 单一状态机（托盘/快捷键/UI 三源一致）+ 配置单一真相（electron-store），窗口只是显示器

### 界面预览

```
主窗口                                          悬浮字幕窗（透明置顶）
┌───────────────────────────────────────────┐   ┌─────────────────────────────┐
│ ⌂ 实时字幕翻译                      ─ □ ✕ │   │                             │
├──────────┬────────────────────────────────┤   │      今天天气真好啊          │
│ ＋新会话  │  直播字幕           暂停 锁   │   │      今日は天気がいいですね   │
│ 搜索      │                                │   └─────────────────────────────┘
│ 今天      │      今日は天気がいいですね     │    ↑ 纯文字预设；Win11 可切
│ ▸生肉直播 │      今天天气真好啊             │      毛玻璃胶囊预设
│ ▸晨会录音 │      14:32:05 · 日→中          │
│ 昨天      │           ● ● ● 聆听中…        │
│ ▸发布会   │  ┌──────────────────────────┐  │
│          │  │ 扬声器 · base · 中 · ● 运行  │ │
│ 设置 ●  │  └──────────────────────────┘  │
└──────────┴────────────────────────────────┘
```

### 快速开始

#### 系统要求

- **操作系统**: Windows 10 1809+ / Windows 11（毛玻璃预设需 Win11）
- **GPU**: NVIDIA 显卡（推荐加速；无 GPU 自动 CPU + int8）
- **内存**: 8GB+；**硬盘**: 5GB+（模型文件）

#### 安装（普通用户）

从 [Releases](https://github.com/BeyondESH/Real-time-subtitle-translation/releases) 下载
`实时字幕翻译-<version>-setup-x64.exe` 安装即可。首启进入引导流程，模型
（Whisper + 日中翻译，约 400MB）自动下载并显示进度，之后完全离线。

> **SmartScreen 提示**：安装包未购买代码签名证书，Windows SmartScreen 可能提示
> "未知发布者"。选择"更多信息 → 仍要运行"即可；或校验 Release 页公布的哈希。

#### 开发运行

```bash
git clone https://github.com/BeyondESH/Real-time-subtitle-translation.git
cd Real-time-subtitle-translation

# 后端依赖（Python 3.10+ 推荐）
cd backend && pip install -r requirements.txt && cd ..

# 前端依赖 + 启动（开发模式自动拉起后端，无需第二个终端）
cd frontend
npm install
npx electron-rebuild -f -w better-sqlite3   # 原生模块对齐 Electron ABI（首次/升级后）
npm run dev
```

常用命令（frontend/）：`npm run dev`（HMR）、`npm run build`、`npm run typecheck`、
`npm test`（vitest）、`npm run pack`（免安装目录）、`npm run dist`（NSIS 安装包）。

### 快捷键（可在 设置→快捷键 重录）

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+Shift+Space` | 暂停/恢复字幕 |
| `Ctrl+Shift+L` | 轮换目标语言 |
| `Ctrl+Shift+M` | 轮换 Whisper 模型 |
| `Ctrl+Shift+D` | 锁定/解锁字幕窗口（解锁后可拖拽） |

### 系统托盘

显示主窗口 / 显示·隐藏字幕 / 暂停·恢复 / 锁定字幕位置 / 重启后端服务 / 设置 / 退出。

### 配置职责（2.0 起双文件各司其职）

| 配置 | 位置 | 内容 |
|------|------|------|
| **用户偏好** | `%APPDATA%\real-time-subtitle-translator\config.json`（electron-store） | 字幕样式/外观预设、主题、窗口位置与显示器、快捷键、目标/激活语言、模型档位、音频源、开机自启、会话设置 |
| **后端管线** | `%APPDATA%\real-time-subtitle-translator\config.yaml`（首装自动复制，**可手工编辑**） | audio / pipeline / vad / asr / translation / websocket |

```yaml
# config.yaml（仅后端管线参数）
pipeline:
  buffer_seconds: 30      # 环形缓冲容量
  tick_ms: 250            # 切句分析周期
  max_utterance_s: 15     # 单句最长秒数，超过强制切分
  queue_size: 8           # 语句队列深度，满时丢最旧并告警
vad:
  threshold: 0.5          # 语音判定阈值，嘈杂环境可调高至 0.6~0.7
  min_silence_duration_ms: 600
  speech_pad_ms: 200
asr:
  model_size: base        # tiny/base/small/medium/large-v3
  device: auto            # auto/cpu/cuda（无 CUDA 自动降级 cpu+int8）
translation:
  primary_model: Helsinki-NLP/opus-mt-ja-zh
  fallback_model: facebook/nllb-200-distilled-600M
```

> 从 1.x 升级：旧 config.yaml 中 subtitle/system/shortcuts 段的自定义值会在升级
> 首启时**一次性迁移**进用户偏好存储，原文件保留不改写。

### 模型说明

| Whisper 模型 | 大小 | 速度 | 准确度 |
|------|------|------|--------|
| tiny | 39MB | 5/5 | 2/5 |
| base | 74MB | 4/5 | 3/5 **推荐** |
| small | 244MB | 3/5 | 4/5 |
| medium | 769MB | 2/5 | 5/5 |
| large-v3 | 1.5GB | 1/5 | 5/5 |

| 翻译模型 | 用途 | 语言支持 |
|------|------|------|
| Helsinki-NLP/opus-mt-ja-zh | 日→中专用（启动后台预载） | 日→中 |
| facebook/nllb-200-distilled-600M | 通用（首次用到才加载） | 200+ 语种 |

### 项目结构

```
Real-time-subtitle-translation/
├── backend/                     # Python 后端（管线）
│   ├── main.py                  # 装配 + 控制消息 + vad_state 广播
│   ├── audio_capture.py         # WASAPI 回环捕获（soxr 重采样）
│   ├── audio_buffer.py          # 环形缓冲 + VAD 切句（UtteranceSegment 含时间戳）
│   ├── vad_events.py            # VAD 状态翻转（speech 即时 / silence 去抖）
│   ├── pipeline_worker.py       # 有界队列：ASR→翻译→广播（背压丢最旧）
│   ├── asr_engine.py            # faster-whisper
│   ├── translator.py            # opus-mt + NLLB（懒加载/预载）
│   ├── websocket_server.py      # WS 服务（广播 + 请求/响应）
│   └── tests/                   # pytest（单元 + 集成 + 协议）
├── frontend/                    # Electron 前端（electron-vite + React 18 + Tailwind）
│   ├── src/
│   │   ├── main/                # 主进程：Gateway / AppState / Controller /
│   │   │   │                    #   ConfigStore / BackendManager / HistoryStore /
│   │   │   │                    #   exporters / UpdaterService / windows
│   │   │   └── __tests__/       # vitest（状态机/协议/迁移/导出/历史真库）
│   │   ├── preload/             # typed 桥（appAPI，contextIsolation）
│   │   ├── shared/              # 跨进程类型 + Intent
│   │   └── renderer/src/
│   │       ├── app/             # 主窗口页面：Live/会话回放/搜索/设置/引导
│   │       ├── components/ui/   # token 驱动薄组件层
│   │       ├── overlay/         # 悬浮字幕窗（React）
│   │       ├── state/hooks.ts   # appAPI 订阅 hooks
│   │       └── styles/tokens.css# 设计 token 唯一来源（暗/亮双主题）
│   ├── electron.vite.config.ts  # main/preload/renderer(双 MPA 入口)
│   ├── electron-builder.yml     # 打包配置唯一来源（含 publish/asarUnpack）
│   └── resources/               # 图标
├── config.yaml                  # 后端管线参数模板（首装复制到用户目录）
├── build.bat                    # Windows 一键构建
└── openspec/                    # 规格与变更（specs / changes）
```

### 构建安装包

```bash
build.bat            # 发布构建（PyInstaller 后端 + NSIS 安装包）
build.bat --console  # 调试构建（后端带控制台窗口）
```

输出：`backend/dist/`（后端）与 `frontend/release/`：
`real-time-subtitle-translator-<version>-setup-x64.exe`（安装包，ASCII 命名以保证
自动更新元数据一致）+ `latest.yml` + `.blockmap`（差分更新）。
发布：将这三个文件上传至 GitHub Release 即触发存量客户端自动更新。

### 常见问题

**没有声音捕获？** 确认正在播放音频、设置→音频里回环设备选择正确、系统未静音。

**翻译延迟高？** 正常语句延迟约 1~2.5 秒。持续偏高时：换更小模型（Ctrl+Shift+M）、
确认 GPU 已启用、出现"处理过载"告警细条说明跟不上，请降模型档位。

**快捷键无效？** 设置→快捷键查看注册状态；显示"注册失败"表示被其他应用占用，
可点"修改"重录组合键。

**更新失败？** 自动更新依赖 GitHub Releases 网络可达；失败为静默降级，
可到 Releases 页手动下载覆盖安装（配置与历史保留）。

---

## English

### Features (2.0)

- **Main window** — ChatGPT-style: live caption stream, session sidebar, status pill bar (audio source / model / language / connection, switch in place), in-app settings (6 sections, changes apply instantly)
- **Session history** — local SQLite: searchable (original + translation), replay with jump-to-hit, export **SRT / bilingual TXT / Markdown / JSON** (accurate SRT timeline via backend timestamps)
- **Overlay** — lock (click-through) / unlock (draggable), always-on-top; entry animation; pause badge; overload strip; plain-text or **acrylic pill** preset (Windows 11)
- **Multi-display** — pick a display for the overlay, per-display position memory, off-screen auto-recall
- **Global shortcuts** — pause / cycle language / cycle model / toggle lock, **re-recordable** in settings with visible conflict warnings
- **First-run onboarding** — welcome → audio source → model download progress → done (skippable)
- **Auto-update** — electron-updater + GitHub Releases, silent check, background download, restart-to-install (or install on quit)
- **Dark/light themes** — dark-first, follow-system option, WCO native titlebar sync
- **Offline** — local models, auto-downloaded on first run
- **Architecture** — single WS Gateway in main process (exponential-backoff reconnect), single AppState machine (tray/shortcuts/UI consistent), single config source of truth

### Requirements

Windows 10 1809+ / 11, 8GB+ RAM, 5GB+ disk, NVIDIA GPU recommended (auto-fallback CPU+int8).

### Dev

```bash
cd backend && pip install -r requirements.txt
cd ../frontend && npm install
npx electron-rebuild -f -w better-sqlite3   # native module vs Electron ABI
npm run dev                                  # backend auto-spawned
```

### Shortcuts (re-recordable in Settings → Shortcuts)

`Ctrl+Shift+Space` pause/resume · `Ctrl+Shift+L` cycle language · `Ctrl+Shift+M` cycle model · `Ctrl+Shift+D` lock/unlock

> **SmartScreen**: the installer is unsigned; choose "More info → Run anyway" or verify the published hash.

### License

MIT License

---

**Star this repo if you find it useful!**
