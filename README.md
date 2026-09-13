# 🎬 Real-time Subtitle Translator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Electron](https://img.shields.io/badge/Electron-28-green.svg)](https://www.electronjs.org/)
[![Whisper](https://img.shields.io/badge/Whisper-faster--whisper-orange.svg)](https://github.com/SYSTRAN/faster-whisper)

基于 OpenAI Whisper 的实时字幕翻译软件，支持多语种语音识别和翻译，提供沉浸式透明悬浮字幕体验。

[English](#english) | [中文](#中文)

---

## 中文

### ✨ 功能特性

- 🎙️ **实时音频捕获** - 使用 WASAPI 回环技术，可选择捕获特定应用的音频
- 🗣️ **语音识别** - 基于 faster-whisper，支持 GPU 加速，延迟 <1 秒
- 🌐 **多语种翻译** - 日中专用模型 + NLLB-200 全语种支持
- 🎨 **透明悬浮窗** - 可拖拽、可缩放、始终置顶、鼠标穿透
- ⌨️ **快捷键控制** - 全局快捷键暂停/恢复、切换语言
- 📦 **离线可用** - 所有模型本地运行，无需网络
- 🔄 **模型自动下载** - 首次使用自动下载模型，后续离线使用

### 📸 界面预览

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                                                     │   │
│  │         今天天气真好啊                               │   │
│  │         今日は天気がいいですね                         │   │
│  │                                                     │   │
│  └─────────────────────────────────────────────────────┘   │
│                      ↑ 透明背景，仅显示文字                   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 🚀 快速开始

#### 系统要求

- **操作系统**: Windows 10/11
- **GPU**: NVIDIA 显卡（推荐，用于加速）
- **内存**: 8GB+
- **硬盘**: 5GB+（模型文件）

#### 安装

```bash
# 克隆仓库
git clone https://github.com/your-username/Real-time-subtitle-translation.git
cd Real-time-subtitle-translation

# 安装后端依赖
cd backend
pip install -r requirements.txt

# 安装前端依赖
cd ../frontend
npm install
```

#### 启动

```bash
# 终端 1: 启动后端服务
cd backend
python main.py

# 终端 2: 启动前端
cd frontend
npm start
```

### 🎯 使用方法

#### 基本操作

1. 启动后，字幕窗口会显示在屏幕底部
2. 播放任意音频（视频、音乐、游戏、直播）
3. 字幕会自动显示识别和翻译结果

#### 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+Shift+Space` | 暂停/恢复字幕 |
| `Ctrl+Shift+L` | 切换目标语言 |
| `Ctrl+Shift+M` | 切换 Whisper 模型 |

#### 系统托盘

右键点击系统托盘图标可以：
- 显示/隐藏字幕窗口
- 暂停/恢复字幕
- 打开设置面板
- 退出程序

### ⚙️ 配置说明

配置文件: `config.yaml`

```yaml
# 音频设置
audio:
  sample_rate: 16000      # 采样率
  channels: 1             # 声道数
  chunk_size: 1024        # 音频块大小

# ASR 设置
asr:
  model_size: base        # tiny/base/small/medium/large-v3
  device: auto            # auto/cpu/cuda
  compute_type: float16   # float16/int8_float16/int8

# 翻译设置
translation:
  primary_model: Helsinki-NLP/opus-mt-ja-zh
  fallback_model: facebook/nllb-200-distilled-600M
  target_languages:
    - zh                  # 中文
    - en                  # 英文

# 字幕显示
subtitle:
  font:
    family: "Microsoft YaHei"
    size: 24
    color: "#FFFFFF"
    stroke_color: "#000000"
    stroke_width: 2
```

### 📊 模型说明

#### Whisper 模型

| 模型 | 大小 | 速度 | 准确度 | 推荐场景 |
|------|------|------|--------|----------|
| tiny | 39MB | ⚡⚡⚡⚡⚡ | ⭐⭐ | 实时性要求高 |
| base | 74MB | ⚡⚡⚡⚡ | ⭐⭐⭐ | **推荐** |
| small | 244MB | ⚡⚡⚡ | ⭐⭐⭐⭐ | 平衡选择 |
| medium | 769MB | ⚡⚡ | ⭐⭐⭐⭐⭐ | 准确度优先 |
| large-v3 | 1.5GB | ⭐ | ⭐⭐⭐⭐⭐ | 最高准确度 |

#### 翻译模型

| 模型 | 用途 | 大小 | 语言支持 |
|------|------|------|----------|
| Helsinki-NLP/opus-mt-ja-zh | 日语→中文专用 | ~300MB | 日→中 |
| facebook/nllb-200-distilled-600M | 通用翻译 | ~1.3GB | 200+ 语种 |

### 🏗️ 项目结构

```
Real-time-subtitle-translation/
├── backend/                    # Python 后端
│   ├── main.py                 # 主入口
│   ├── audio_capture.py        # WASAPI 音频捕获
│   ├── asr_engine.py           # faster-whisper ASR
│   ├── translator.py           # 翻译引擎
│   ├── websocket_server.py     # WebSocket 服务
│   ├── requirements.txt        # Python 依赖
│   └── tests/                  # 测试文件
├── frontend/                   # Electron 前端
│   ├── src/
│   │   ├── main.ts             # Electron 主进程
│   │   ├── overlay.html        # 字幕悬浮窗
│   │   ├── preload.ts          # 预加载脚本
│   │   └── settings.html       # 设置面板
│   ├── package.json
│   └── tsconfig.json
├── config.yaml                 # 配置文件
├── build.bat                   # Windows 构建脚本
└── README.md                   # 项目文档
```

### 🔧 构建安装包

```bash
# Windows
build.bat
```

输出目录:
- `backend/dist/` - Python 后端可执行文件
- `frontend/release/` - Electron 安装包

### ❓ 常见问题

#### Q: 没有声音捕获？

A: 确保：
1. 应用正在播放音频
2. 音频源选择正确
3. 系统音频未静音

#### Q: 翻译延迟高？

A: 尝试：
1. 使用更小的模型（如 base）
2. 确保 GPU 已启用
3. 检查系统资源占用

#### Q: 如何添加新语言？

A: 在 `config.yaml` 的 `target_languages` 中添加语言代码：
```yaml
target_languages:
  - zh  # 中文
  - en  # 英文
  - ja  # 日文
  - ko  # 韩文
```

#### Q: 模型下载失败？

A: 检查：
1. 网络连接正常
2. 防火墙未阻止
3. 磁盘空间充足

### 📄 许可证

MIT License - 详见 [LICENSE](LICENSE)

### 🤝 贡献

欢迎提交 Issue 和 Pull Request！

### 🙏 致谢

- [OpenAI Whisper](https://github.com/openai/whisper) - 语音识别模型
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) - Whisper 加速版本
- [Helsinki-NLP](https://huggingface.co/Helsinki-NLP) - 翻译模型
- [Meta NLLB](https://github.com/facebookresearch/fairseq/tree/nllb) - 多语种翻译模型
- [Electron](https://www.electronjs.org/) - 桌面应用框架

---

## English

### ✨ Features

- 🎙️ **Real-time Audio Capture** - WASAPI loopback, capture specific app audio
- 🗣️ **Speech Recognition** - Based on faster-whisper, GPU accelerated, <1s latency
- 🌐 **Multi-language Translation** - Japanese-Chinese specialized + NLLB-200 for 200+ languages
- 🎨 **Transparent Overlay** - Draggable, resizable, always-on-top, click-through
- ⌨️ **Global Shortcuts** - Pause/resume, switch languages
- 📦 **Offline Ready** - All models run locally, no internet required
- 🔄 **Auto Model Download** - First-run auto-download, cached for offline use

### 🚀 Quick Start

#### Requirements

- **OS**: Windows 10/11
- **GPU**: NVIDIA (recommended)
- **RAM**: 8GB+
- **Storage**: 5GB+

#### Install

```bash
git clone https://github.com/your-username/Real-time-subtitle-translation.git
cd Real-time-subtitle-translation

# Backend
cd backend && pip install -r requirements.txt

# Frontend
cd ../frontend && npm install
```

#### Run

```bash
# Terminal 1: Backend
cd backend && python main.py

# Terminal 2: Frontend
cd frontend && npm start
```

### ⌨️ Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+Shift+Space` | Pause/Resume |
| `Ctrl+Shift+L` | Switch Language |
| `Ctrl+Shift+M` | Switch Model |

### 📄 License

MIT License

---

**⭐ Star this repo if you find it useful!**
