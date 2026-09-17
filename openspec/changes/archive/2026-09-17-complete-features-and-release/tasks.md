# complete-features-and-release — 实现任务

> 前置：change `fix-core-pipeline` 已完成并归档。

## 1. Electron 安全与协议地基

- [x] 1.1 `main.ts`：`webPreferences` 改为 `contextIsolation:true, nodeIntegration:false, preload: dist/preload.js`
- [x] 1.2 `preload.ts`：扩建 electronAPI（窗口控制、设置读写、事件订阅），字幕窗与设置窗共用
- [x] 1.3 `overlay.html`：全部 `ipcRenderer` 直用迁移到 `window.electronAPI`；保留原生 WebSocket 客户端
- [x] 1.4 `websocket_server.py`：新增请求/响应模式（`{type:'request', id, method}` → `send_to` 回 `{type:'response', id, ...}`），实现 `get_audio_sources`、`get_config`

## 2. 窗口交互与快捷键

- [x] 2.1 `main.ts`+`overlay.html`：锁定/解锁双模式（`setIgnoreMouseEvents` + `-webkit-app-region: drag` 条件启用），托盘勾选项，状态持久化
- [x] 2.2 `main.ts`：注册全套快捷键（暂停/语言/模型/锁定），单个失败记日志不影响其余；store defaults 补齐 `switchModel`、`toggleLock`
- [x] 2.3 新增图标资源 `frontend/src/icon.png` 与 `icon.ico`；`createTray` 加 try/catch 降级
- [x] 2.4 托盘菜单补齐：显示/隐藏、暂停/恢复、锁定位置、设置、退出

## 3. 设置面板接线

- [x] 3.1 `main.ts`：设置窗口单例创建/聚焦；IPC 通道 `settings-load`/`settings-save`
- [x] 3.2 `settings.html`：IPC 加载/保存 electron-store；音频源下拉经 WS `get_audio_sources` 拉取，失败显示重试态
- [x] 3.3 保存后实时下发：样式→overlay IPC；target_languages/active_language/模型/音频源→WS control 消息，带回执处理
- [x] 3.4 前端 WS 连接建立后发送 `config_sync`；后端收到后覆盖对应运行配置

## 4. 字幕显示与激活语言

- [x] 4.1 后端：维护 `active_target_language`；新增 `control/set_language` action 与 `invalid_language` 回执；subtitle 消息携带 `active_language`，translations 只含激活语言
- [x] 4.2 `overlay.html`：按 `active_language` 渲染（消灭 zh 硬编码）；实现 `switch-language` 轮换逻辑
- [x] 4.3 `overlay.html`：display_mode 两种模式生效；样式改为启动时从 electron-store 读取 + IPC 实时更新（删除 CSS 硬编码）

## 5. 打包分发

- [x] 5.1 删除 `package.json` 的 `build` 键；`electron-builder.yml` 移除 mac/linux 目标；移除空 models/ 的 extraResources 项
- [x] 5.2 `build.spec`：`collect_all` ctranslate2/tokenizers/sentencepiece，`collect_data_files('faster_whisper')`，hiddenimports 增 soxr 删 langdetect，datas 移除 models/，console 改为构建变量
- [x] 5.3 `main.ts`：后端进程托管（packaged 拉起 resourcesPath 下 exe / dev 拉起 python；WS 指数退避健康检查 90s；will-quit 杀进程树；异常退出托盘提示+可重启；超时错误对话框含日志路径）
- [x] 5.4 后端：`RotatingFileHandler` 写 userData/logs/backend.log；前端接入 electron-log 写 frontend.log；启动时 config.yaml 首次复制到 userData
- [x] 5.5 `build.bat`：增加构建前检查（图标存在、config.yaml 存在）与 console 开关参数

## 6. 验证与文档

- [x] 6.1 手测清单全过：三快捷键、锁定拖拽、设置保存实时生效、后端崩溃重启
- [x] 6.2 干净环境冒烟：安装包首装→自动拉起后端→模型下载进度可见→60s 内出字幕；logs/ 有日志
- [x] 6.3 README 修正：删除"按应用捕获"，延迟宣传改为"约 1~2.5 秒"，补充分发安装与设置面板说明
