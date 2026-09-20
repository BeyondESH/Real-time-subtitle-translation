# Real-time Subtitle Translator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Electron](https://img.shields.io/badge/Electron-28-green.svg)](https://www.electronjs.org/)
[![ASR](https://img.shields.io/badge/ASR-Fun--ASR--Nano%20(sherpa--onnx)-orange.svg)](https://github.com/k2-fsa/sherpa-onnx)

本地离线的实时字幕翻译软件（Fun-ASR-Nano 语音识别 + llama.cpp 本地翻译）：捕获系统音频 → 语音识别 → 多语种翻译 → 透明悬浮字幕 + ChatGPT 式主窗口（直播流 / 会话历史 / 搜索 / 导出）。完全离线可用。

[中文](#中文) | [English](#english)

---

## 中文

### 功能特性（2.0）

- **主窗口** — ChatGPT 式布局：直播字幕流、会话侧栏（今天/昨天/更早）、状态胶囊条（音频源·模型·语言·连接状态就地切换）、应用内设置页（六分段，改动即生效）
- **实时音频捕获** — WASAPI 回环捕获系统音频，设备可选（胶囊条 / 设置 / 引导页三处下拉：默认回环设备或指定设备）。多显示器：悬浮窗可指定显示器、按显示器记忆位置、越界自动回收
- **语音识别** — Fun-ASR-Nano（sherpa-onnx 进程内常驻）+ 项目内嵌 Silero VAD 切句，语句结束约 1~2 秒出字幕（每条字幕脚注显示端到端耗时，悬浮可看分解）；源语言可选（日语（默认）/中文/英文，热切换）；"聆听中"实时脉冲指示
- **推理设备** — 自动/CPU/GPU 三态切换：启动自动检测 CUDA（识别经 onnxruntime CUDA provider 探针、翻译经 llama.cpp 设备枚举探针），检测到即用 GPU（翻译引擎的 CUDA 运行库随包内置，有 NVIDIA 显卡即 GPU 翻译）；不可用或加载失败静默降级 CPU 继续运行，设置页实时显示实际设备与降级原因
- **多语种翻译** — llama.cpp 本地推理（`llama-server` sidecar）：默认 Hy-MT2-1.8B 翻译专用模型，内置注册表可运行时切换（7B / Qwen3-1.7B），首次使用自动下载（多源镜像、断点续传）；译文随生成**逐字上屏（流式）**，进行中弱化、定稿增强，首字延迟显著低于整段等待；可用 `translation.stream: false` 关掉回退整段模式
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
│          │  │ 默认设备 · base · 中 · ● 运行│ │
│ 设置 ●  │  └──────────────────────────┘  │
└──────────┴────────────────────────────────┘
```

### 快速开始

#### 系统要求

- **操作系统**: Windows 10 1809+ / Windows 11（毛玻璃预设需 Win11）
- **GPU**: NVIDIA 显卡（推荐加速；启动自动检测启用，不可用或加载失败静默降级 CPU + int8）
- **内存**: 8GB+；**硬盘**: 5GB+（模型文件）

#### 安装（普通用户）

从 [Releases](https://github.com/BeyondESH/Real-time-subtitle-translation/releases) 下载
`实时字幕翻译-<version>-setup-x64.exe` 安装即可。首启进入引导流程，模型
（识别模型 Fun-ASR-Nano 约 948MB + 翻译模型 Hy-MT2-1.8B 约 1.1GB）自动下载并显示进度，之后完全离线。

> **SmartScreen 提示**：安装包未购买代码签名证书，Windows SmartScreen 可能提示
> "未知发布者"。选择"更多信息 → 仍要运行"即可；或校验 Release 页公布的哈希。

#### 开发运行

```bash
git clone https://github.com/BeyondESH/Real-time-subtitle-translation.git
cd Real-time-subtitle-translation

# 后端依赖（Python 3.10+ 推荐；requirements 默认装 CPU 版 sherpa-onnx）
cd backend && pip install -r requirements.txt && cd ..

# GPU 可选：识别引擎换 CUDA 变体 wheel + 准备随包 CUDA 运行库（cuDNN9/cufft 等）
pip install "sherpa-onnx==1.13.8+cuda12.cudnn9" -f https://k2-fsa.github.io/sherpa/onnx/cuda.html
pwsh -File backend/scripts/vendor_llama.ps1 -CudaOnly   # 解包 llama 二进制 + 复制 nvidia 运行库 DLL

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
| `Ctrl+Shift+D` | 锁定/解锁字幕窗口（解锁后可拖拽） |

### 系统托盘

显示主窗口 / 显示·隐藏字幕 / 暂停·恢复 / 锁定字幕位置 / 重启后端服务 / 设置 / 退出。

### 配置职责（2.0 起双文件各司其职）

| 配置 | 位置 | 内容 |
|------|------|------|
| **用户偏好** | `%APPDATA%\real-time-subtitle-translator\config.json`（electron-store） | 字幕样式/外观预设、主题、窗口位置与显示器、快捷键、目标/激活语言、模型档位、推理设备、音频源、开机自启、会话设置 |
| **后端管线** | `%APPDATA%\real-time-subtitle-translator\config.yaml`（首装自动复制，**可手工编辑**） | audio / pipeline / vad / asr / translation / websocket |

```yaml
# config.yaml（仅后端管线参数）
pipeline:
  buffer_seconds: 30      # 环形缓冲容量
  tick_ms: 125            # 切句分析周期
  max_utterance_s: 15     # 单句最长秒数，超过强制切分
  queue_size: 8           # 语句队列深度，满时丢最旧并告警
vad:
  threshold: 0.5          # 语音判定阈值，嘈杂环境可调高至 0.6~0.7
  min_silence_duration_ms: 400
  speech_pad_ms: 200
asr:
  model: funasr-nano      # 识别引擎模型标识（单引擎，Fun-ASR-Nano INT8，约 948MB 首装下载）
  device: auto            # auto/cpu/cuda；auto=检测到即优先 GPU，失败静默降级 CPU（UI 设置优先于此文件）
  language: ja            # 源语言（识别提示 + 翻译源语言）：ja/zh/en，非法值回退 ja
  itn: true               # 逆文本规范化（数字/日期口语形式 → 书面形式）
  hotwords: ''            # 热词（逗号分隔，提升专有名词识别；空=不启用）
  num_threads: 2          # 解码线程数
translation:
  default_model: hy-mt2-1.8b-q4km   # 随包注册表 id；首次使用自动下载
  download:
    source: auto                    # auto/huggingface/hf-mirror/modelscope
  device: auto                      # auto/cpu/cuda
  stream: true                      # 译文流式输出（逐字上屏）；false=整段输出
```

> 从 1.x 升级：旧 config.yaml 中 subtitle/system/shortcuts 段的自定义值会在升级
> 首启时**一次性迁移**进用户偏好存储，原文件保留不改写。

> 推理设备日常在 设置→模型 切换（存于用户偏好）：显式选择 CPU/GPU 时经 `SUBTITLE_DEVICE`
> 注入后端首载，优先于本文件；本文件的 `device` 仅在偏好为"自动"时作为后端默认值生效。

> 升级提示：用户目录 `config.yaml` 首装复制后**不会随版本升级覆盖**；想获得新默认值
> （如 `tick_ms: 125`、`min_silence_duration_ms: 400`），请对照本模板手动更新对应键。
> 端点判定的低延迟修正无需改配置即生效。

### 模型说明

| 识别模型（首次启动自动下载） | 大小 | 说明 |
|------|------|------|
| Fun-ASR-Nano（INT8，单引擎） | ~948MB | 中文/英文/日语识别（SenseVoice 编码器 + Qwen3-0.6B 解码器；无自动语种检测，源语言由设置指定） |

| 翻译模型（GGUF，按需下载） | 大小 | 用途 |
|------|------|------|
| Hy-MT2-1.8B（默认） | ~1.1GB | 翻译专用，质量与速度平衡（Apache-2.0） |
| Hy-MT2-7B | ~4.6GB | 翻译专用高质量档（Apache-2.0） |
| Qwen3-1.7B | ~1.3GB | 通用/多语兜底（Apache-2.0） |

> 翻译由随包的 llama.cpp（`llama-server`）本地推理；模型不入安装包，首次使用自动下载
> （HuggingFace 官方 / HF 镜像 / ModelScope 多源切换、断点续传），可在 设置→模型 切换。

### 项目结构

```
Real-time-subtitle-translation/
├── backend/                     # Python 后端（管线）
│   ├── main.py                  # 装配 + 控制消息 + vad_state 广播
│   ├── audio_capture.py         # WASAPI 回环捕获（soxr 重采样）
│   ├── audio_buffer.py          # 环形缓冲 + VAD 切句（UtteranceSegment 含时间戳）
│   ├── vad_events.py            # VAD 状态翻转（speech 即时 / silence 去抖）
│   ├── pipeline_worker.py       # 有界队列：ASR→翻译→广播（背压丢最旧）
│   ├── asr_engine.py            # Fun-ASR-Nano（sherpa-onnx 进程内常驻）
│   ├── asr_models.py            # ASR 模型注册表与下载（多源/续传/校验）
│   ├── translator.py            # llama-server sidecar 翻译引擎（注册表/净化/降级）
│   ├── llama_server_manager.py  # llama-server 进程管理（端口/健康/重启/清理）
│   ├── translation_models.py    # 翻译模型注册表（pin + sha256）
│   ├── model_downloader.py      # 模型下载（多源/续传/校验/进度）
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

**独占模式 / DRM 内容捕获不到？** 独占模式与受 DRM 保护的内容不经过系统回环，属回环捕获的固有限制，无法捕获。

**翻译延迟高？** 正常语句延迟（说完→上屏）约 1~2 秒；每条字幕脚注显示该耗时，悬浮可看分解（静音等待/队列/识别/翻译），先看哪段大再对症下药。持续偏高时：
确认 GPU 已启用（设置→模型"推理设备"状态行）、翻译模型保持默认 Hy-MT2-1.8B（7B 档更慢，
见 设置→模型→翻译模型）、出现"处理过载"告警细条说明跟不上，请减少同时处理的负担（如暂停字幕）。流式开启时正文逐字出现，首字更快；若遇异常可通过 `translation.stream: false` 回退。

**翻译模型下载失败？** 设置→模型→翻译模型 显示当前模型与下载状态；失败时重新选择该模型
即重试（自动多源切换、断点续传）。也可手工编辑用户目录 `config.yaml` 的
`translation.download.source`（auto/huggingface/hf-mirror/modelscope）后重启。

**GPU 未启用？** 设置→模型 的"推理设备"状态行显示实际设备与原因："CPU（未检测到兼容的
CUDA 环境）"=环境无可用 CUDA；"CPU（GPU 加载失败，已自动降级）"=检测到但加载失败（排查见
`logs/backend.log`）。翻译引擎（llama.cpp）与识别引擎（Fun-ASR-Nano/sherpa-onnx）的 CUDA
运行库均随安装包内置（有 NVIDIA 显卡即可 GPU 运行），无需系统安装 CUDA。

**GPU 被自动降级到 CPU？** 识别引擎优先加载安装包内置的 CUDA 运行库（`vendor/llama`，无需系统
额外安装）；仅当内置运行库缺失或加载失败时才会自动改用 CPU 继续工作，确切原因见
`logs/backend.log`。小模型（Fun-ASR-Nano）在 CPU 上同样可实时（RTF 约 0.13，部分机器
CPU 甚至快于 GPU），降级不影响可用性。

**快捷键无效？** 设置→快捷键查看注册状态；显示"注册失败"表示被其他应用占用，
可点"修改"重录组合键。

**更新失败？** 自动更新依赖 GitHub Releases 网络可达；失败为静默降级，
可到 Releases 页手动下载覆盖安装（配置与历史保留）。

---

## English

### Features (2.0)

- **Main window** — ChatGPT-style: live caption stream, session sidebar, status pill bar (audio source / model / language / connection, switch in place), in-app settings (6 sections, changes apply instantly)
- **Audio capture** — WASAPI loopback captures system audio with selectable devices (dropdown in pill bar / settings / onboarding: default loopback device or a specific device). Multi-display: overlay display pick, per-display position memory, off-screen auto-recall
- **Multi-language translation** — local llama.cpp inference (`llama-server` sidecar): default Hy-MT2-1.8B translation model, runtime-switchable registry (Hy-MT2-7B / Qwen3-1.7B), downloaded on first use (multi-source mirrors, resumable); the translation streams in as it is generated, dimmed while in progress and emphasized once final, with a much lower first-token delay than waiting for the whole segment; set `translation.stream: false` to disable streaming and fall back to the one-shot mode
- **Session history** — local SQLite: searchable (original + translation), replay with jump-to-hit, export **SRT / bilingual TXT / Markdown / JSON** (accurate SRT timeline via backend timestamps)
- **Overlay** — lock (click-through) / unlock (draggable), always-on-top; entry animation; pause badge; overload strip; plain-text or **acrylic pill** preset (Windows 11)
- **Inference device** — Auto/CPU/GPU toggle; CUDA auto-detected at startup (onnxruntime CUDA-provider probe for ASR, llama.cpp device-enumeration probe for translation), used when available (the translation engine's CUDA runtime ships with the installer); silent CPU fallback on failure; settings shows the actual device and fallback reason
- **Multi-display** — pick a display for the overlay, per-display position memory, off-screen auto-recall
- **Global shortcuts** — pause / cycle language / toggle lock, **re-recordable** in settings with visible conflict warnings
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

`Ctrl+Shift+Space` pause/resume · `Ctrl+Shift+L` cycle language · `Ctrl+Shift+D` lock/unlock

> **SmartScreen**: the installer is unsigned; choose "More info → Run anyway" or verify the published hash.

### FAQ

**No audio is being captured?** Confirm audio is playing, the loopback device selected in Settings → Audio is correct, and the system is not muted.

**Exclusive-mode / DRM content not captured?** Exclusive-mode and DRM-protected audio bypass system loopback — an inherent limitation of loopback capture.

**See "GPU runtime unavailable, automatically fell back to CPU"?** The ASR engine loads the CUDA
runtime bundled with the installer (`vendor/llama`) automatically — no system CUDA installation is
required. Only when the bundled runtime is missing or fails does it fall back to CPU + int8 (exact
reason in `logs/backend.log`).

**Translation model download failed?** Settings → Model → Translation model shows the current
model and download state; re-selecting the model retries (automatic multi-source switching,
resumable). You can also edit `translation.download.source`
(auto/huggingface/hf-mirror/modelscope) in the user `config.yaml` and restart.

**High translation latency?** With streaming enabled the translation appears word by word and the
first token shows sooner; if it misbehaves, set `translation.stream: false` to fall back to the
one-shot mode.

### License

MIT License

---

**Star this repo if you find it useful!**
