# Design: redesign-electron-client

## Context

现有客户端为三文件结构：`main.ts`（主进程：窗口/托盘/快捷键/后端托管/electron-store）、`overlay.html`（字幕悬浮窗，**直连** `ws://localhost:8765`）、`settings.html`（独立设置窗，经 preload IPC 读写配置）。渲染层无构建体系（tsc + 内联 JS）。后端协议：广播 `subtitle`/`model_progress`/`pipeline_warning`/`error`，控制 `pause`/`resume`/`set_language`/`set_audio_source`/`change_model`/`config_sync`，请求/响应 `get_audio_sources`/`get_config`。字幕消息现无时间戳。

既有 8 份 capability spec（openspec/specs/），其中 overlay-window、settings-management、subtitle-display、pipeline-control、release-packaging 五份受本次变更影响。Windows-only（WASAPI 限定），Electron 28，NSIS 打包，后端 PyInstaller 随包分发。

## Goals / Non-Goals

**Goals:**

- WS 连接、状态、配置、历史全部收敛到主进程，窗口退化为纯显示/交互端
- ChatGPT/Ollama 桌面端观感的完整主窗口（直播流/会话侧栏/应用内设置/首启引导）
- 字幕悬浮窗视觉翻新且行为语义（穿透/锁定/置顶）不回退
- 会话历史持久化与 SRT/TXT/MD/JSON 导出
- 开机自启实装、自动更新接入
- 既有 5 份 spec 的行为要求在新架构下全部继续成立（改写处出 delta spec）

**Non-Goals:**

- 麦克风输入模式（后端 capture 扩展，另行提案）
- 流式部分字幕 / partial ASR 结果（另行提案）
- Web / 移动端客户端
- 客户端 UI 多语言（本期仅 zh-CN，文案集中管理为将来 i18n 留位）
- 后端管线算法改动（切句/VAD/ASR/翻译逻辑不动）

## Decisions

### D1. 渲染栈：React 18 + electron-vite + Tailwind，自建薄 UI 组件层

- **选择**：electron-vite 统一构建（main / preload / renderer 多入口，renderer 内 `index.html` 主窗口与 `overlay.html` 悬浮窗双 MPA 入口）；React 18 + TypeScript strict；Tailwind CSS 承载设计 token；组件自建（Button/Pill/Toggle/Slider/Modal/SegmentedNav 等约 12 个，shadcn 风格代码入仓），图标用 lucide-react。**不引入**重型组件库（MUI/AntD）与前端路由库之外的状态库——状态一律来自主进程 AppState，渲染层无本地权威状态。
- **备选**：Svelte（更轻但生态/组件参考少）；继续 vanilla（做到目标精致度的动画与组件复用成本过高）；Next/Nuxt（桌面端无意义）。
- **理由**：ChatGPT 式界面 = 大量小而一致的交互组件 + 列表虚拟化 + 主题切换，React 生态成熟度最高；electron-vite 对 Electron 28 的 HMR/打包支持完善。

### D2. 连接与状态：主进程 Gateway + AppState 单一状态机

```
                 后端 Python (:8765)
                        ▲ 唯一 WS 连接
┌─ Electron 主进程 ──────┴───────────────────────────────┐
│  Gateway                                                │
│   · 连接生命周期 + 指数退避重连(250ms→30s, 抖动脉冲)      │
│   · request(method,params)→Promise  (pending map + 超时) │
│   · 收到广播 → 路由: subtitle→HistoryStore写入+流广播      │
│     model_progress/pipeline_warning/error/vad_state      │
│       →AppState 更新                                     │
│  AppState (单一真相)                                     │
│   connection/capture/vad/model/modelDownload/language/    │
│   audioSource/locked/overlayVisible/lastWarning/dropped   │
│   · dispatch(action) 是唯一变更入口                       │
│   · 变更→计算 patch→webContents.send('state:patch')全窗口  │
│  ConfigStore(electron-store) / HistoryStore(SQLite)       │
│  WindowManager: main / overlay / tray / globalShortcut    │
└──────────────┬─────────────────────────┬─────────────────┘
     IPC       ▼                IPC      ▼
   主窗口(React)              悬浮字幕窗(React, 极薄)
```

- **动作源统一**：托盘菜单、全局快捷键、主窗口按钮全部调用同一 `dispatch(action)`；action 集合（typed union）：`togglePause`、`cycleLanguage`/`setLanguage(code)`、`cycleModel`/`setModel(size)`、`toggleLock`、`setAudioSource(id)`、`setOverlayVisible(bool)`、`newSession`、`setConfig(patch)` 等。dispatch 内部完成三件事：状态迁移 → 需要时经 Gateway 发 WS control/request → 广播 patch。
- **preload API 面（唯一双向通道，替代现 electronAPI）**：`getState()`、`onStatePatch(cb)`、`onSubtitle(cb)`、`dispatch(action)`、`wsRequest(method, params)`、会话/历史/导出/窗口控制等 invoke 方法。渲染进程零 node 权限要求不变（overlay-window spec）。
- **备选**：连接留在渲染层（现状，控制通道随字幕窗存亡——正是要修的病根）；独立 utilityProcess 承载 WS（多一层进程与序列化，收益不抵复杂度）。
- **重连语义**：Gateway 与后端进程托管解耦——端口探活失败由 BackendManager 负责重启进程，Gateway 只管 WS 层重连；两者状态都进 AppState。

### D3. 配置：electron-store 偏好唯一真相 + config.yaml 首装生成不覆盖 + 连接后差异对齐

- 职责切分：electron-store 只管**用户偏好**（字幕样式/外观预设、窗口、语言、快捷键、锁定、模型选择、音频源、主题、开机自启）；config.yaml 只管**后端管线参数**（audio/pipeline/vad/asr/translation/websocket）。
- config.yaml 语义：**首装生成、用户可编辑、前端永不覆盖**——新模板仅含后端消费段（subtitle/system/shortcuts 僵尸段移除）；userData 已有副本则原样尊重（用户手调 vad.threshold 等管线参数不被前端重写），后端加载代码零改动。
- **连接后差异对齐（替代每次启动重写文件）**：Gateway 在 WS 连接建立（含重连）后将后端运行态对齐到 store：发 `config_sync`（target/active language）；store 模型 ≠ 后端当前模型时发 `change_model`；音频源不一致时发 `set_audio_source`。对齐幂等，重连自动重同步——这也顺手修掉"后端重启后前端偏好失效"的现状缺陷。
- **一次性迁移**：升级首启检测旧副本中 `subtitle`/`system`/`shortcuts` 段的用户改动值，导入 store（autoStart、字体字号等），原文件保留不重写，日志记录迁移结果；store schema 加 `version` 字段，用 electron-store migrations 演进。
- **备选**：每次启动前从 store 物化重写 config.yaml（会覆盖用户手调管线参数，弃）；双向同步 config.yaml ↔ store（冲突解决复杂，且后端从不回写，弃）；后端改读 JSON（无必要）。

### D4. 历史存储：better-sqlite3（主进程同步 API），导出器为纯函数模块

```sql
sessions   (id TEXT PK, title TEXT, started_at INT, ended_at INT NULL,
            audio_source TEXT, stats_json TEXT)
utterances (id INTEGER PK AUTOINCREMENT, session_id TEXT FK, received_at INT,
            ts_start REAL NULL, ts_end REAL NULL,
            original TEXT, source_lang TEXT, translation TEXT, target_lang TEXT,
            model TEXT)
-- 索引: utterances(session_id, received_at); sessions(started_at DESC)
```

- 写入路径：Gateway 收到 `subtitle` 广播 → 组装 utterance（当前会话 id、后端时间戳或接收时刻）→ 同步 insert（better-sqlite3 单条写 <1ms，不阻塞事件循环）→ 再 IPC 广播给窗口。**主窗口未开时历史照常记录**——这是 Gateway 上移的直接红利。
- 会话切分：应用启动时若上一会话有语句则开新自动会话（空会话复用不堆垃圾）；侧栏"＋新会话"手动切分；可选"静音 N 分钟自动切分"（默认关，阈值默认 30min，写入 store `sessions.autoSplitSilenceMin`）。会话标题默认取首句译文前 12 字，可重命名。
- 导出器 `exporters/`（srt/txt/md/json）为无 Electron 依赖的纯函数（输入 utterance[]，输出字符串），vitest 单测。SRT 时间轴优先 `ts_start/ts_end`，缺失时以 `received_at` 反推近似并在导出说明里注明（旧后端兼容路径）。
- 搜索：`LIKE '%q%'` 起步（万级语句量足够快）；FTS5 留到量级出现再做，不在本期。
- **备选**：JSONL 追加文件（搜索/删除/分页全要手写）；electron-store 存历史（JSON 全量读写，规模上去必然卡）；低内存占用考虑过 `node:sqlite`（Electron 28 未内置，不可用）。

### D5. 主窗口壳：无边框 + WCO（titleBarOverlay）

- `BrowserWindow({ frame: false, titleBarOverlay: { color, symbolColor, height: 36 }, roundedCorners: true })`，原生最小化/最大化/关闭按钮由 Windows 绘制（Electron 28 在 Win10/11 均支持）；标题栏自定义区域 CSS `-webkit-app-region: drag`，交互控件 `no-drag`。主题切换时用 `setTitleBarOverlay()` 同步按钮配色。
- 布局：左侧 240px 可折叠侧栏（会话列表/搜索/设置入口），主区路由 `/live`（直播流，默认）、`/session/:id`（历史回放）、`/settings/*`（分段导航设置页）；路由用轻量自管 state（不引 react-router，窗口内三路由手写足够）——**修正**：为可维护性仍引入 react-router-dom（HashRouter），避免自造轮子。
- **备选**：系统原生边框（省事但观感立即掉档，与 ChatGPT 桌面端目标背离）；全自绘按钮（跨 Windows 版本行为细节多，WCO 是官方答案）。

### D6. 悬浮窗：双外观预设，切换需重建窗口

- 预设 A **纯文字**（默认）：`transparent: true` 现状延续 + 新动效；预设 B **毛玻璃胶囊**：`transparent: false, backgroundMaterial: 'acrylic'`（Win11），CSS 透明背景 + 圆角内容区。`transparent` 运行时不可切换是 Electron/Windows 限制 → 预设变更由 WindowManager 销毁重建悬浮窗（位置/锁定态从 store 恢复，用户无感知丢失）。
- Win10 检测不支持 `backgroundMaterial` → 设置项禁用并显示"需要 Windows 11"。锁定穿透（`setIgnoreMouseEvents(true,{forward})`）在两种预设下均需验证，acrylic 若与 forward 冲突则毛玻璃模式下穿透退化为整窗命中测试（记录为 P1 验证点）。
- 动效与徽标（subtitle-display delta）：新句入场 `opacity 0→1 + translateY(8px→0)`，250ms ease-out；保留最近 N 条；暂停时角落 12px ⏸ 徽标（50% 透明度）；pipeline_warning → 琥珀色 2px 顶部细条 2s。
- **备选**：CSS backdrop-filter 自绘毛玻璃（transparent 窗口下 backdrop-filter 抓不到桌面背景，Windows 上只有系统 material 能做到真亚克力）。

### D7. 后端协议增量（向后兼容，唯一后端改动面）

- `pipeline_worker` 广播的 `subtitle` 消息增加 `ts_start`/`ts_end`（float 秒，来自 UtteranceSegmenter 已有切句时间，随 utterance 对象透传进队列）——SRT 精确导出的前提。
- `main.py` 切句循环中检测 VAD 状态迁移（segmenter 内部已有 speech 判定），**仅在状态翻转时**广播 `{type:'vad_state', state:'speech'|'silence'}`，speech 起始立即发、silence 起始去抖 300ms，避免高频消息。
- 协议测试（backend/tests/）补两个消息形状断言。旧前端收到新字段无感；新前端对旧后端缺字段走 D4 降级路径。

### D8. 设计 token（design-system 的落地约定）

| Token | 暗色（默认） | 亮色 | 用途 |
|---|---|---|---|
| bg-base | `#0d0d0d` | `#ffffff` | 主区背景 |
| bg-sidebar | `#171717` | `#f9f9f9` | 侧栏/次级面板 |
| bg-elevated | `#1e1e1e` | `#f2f2f2` | 卡片/输入面 |
| border | `rgba(255,255,255,.08)` | `rgba(0,0,0,.08)` | 1px 静默边框 |
| text-1 / text-2 | `#ececec` / `#9a9a9a` | `#1a1a1a` / `#6b6b6b` | 主/次文字 |
| accent | `#10a37f`（待终选） | 同 | 品牌色，克制使用 |
| ok/warn/danger | `#4ade80`/`#fbbf24`/`#f87171` | 同 | 仅状态语义 |
| radius | 卡片 12px / 控件 8px / 胶囊 999px | | |
| motion | 200ms ease-out；字幕入场 250ms fade+8px | | |
| font | `"Segoe UI Variable", system-ui, "Microsoft YaHei"`；UI 14px / 二级 12px | | |

- Tailwind config 全部引用 CSS 变量（`:root` + `[data-theme="light"]`），主题切换 = 改 `data-theme` 属性；亮暗双主题本期都交付（工作量在 token 表而非组件）。字体/字号/颜色等字幕样式仍由 store 配置驱动（subtitle-display spec 不变式：MUST NOT 硬编码）。

### D9. 自动更新与开机自启

- 自动更新：electron-updater + GitHub Releases provider（仓库公开）；`electron-builder.yml` 增 `publish`。启动后静默检查 + 设置页手动"检查更新"；下载进度进状态胶囊条提示位；下载完成托盘通知"重启安装"。不引入代码签名（SmartScreen 提示为已知代价，README 说明）。
- 开机自启：`app.setLoginItemSettings({ openAtLogin })`，开关在设置"通用"页，持久化于 store `system.autoStart`（替换 config.yaml 的死 `system` 段语义）。

### D10. 目录结构与测试

```
frontend/src/
├─ main/            index.ts · gateway.ts · state.ts · actions.ts
│                   backend-manager.ts · config.ts · history-db.ts
│                   exporters/{srt,txt,md,json}.ts · windows/{main,overlay,tray,shortcuts}.ts
├─ preload/         index.ts（typed bridge，唯一 IPC 面）
└─ renderer/
   ├─ index.html + app/（routes: live · session · settings · onboarding）
   ├─ features/{live-stream,session-list,session-detail,settings,onboarding}/
   ├─ components/ui/（token 驱动的薄组件层）
   ├─ overlay/（index.html + 极薄组件，无路由）
   └─ styles/tokens.css
```

- 前端 vitest：state reducer（action→patch 纯函数化）、exporters、gateway 协议编解码（mock WS）；组件层不强制单测，靠手动验收清单。后端既有 pytest 全量保持绿色 + 协议增量断言。
- root `build.bat`/`electron-builder.yml` 适配：`files` 改为 electron-vite 产物 `out/**`，better-sqlite3 native 模块验证（`--dir` 打包冒烟先行）。

## Risks / Trade-offs

- [better-sqlite3 原生模块与 Electron ABI / 打包失败] → P2 第一件事就是 `electron-builder --dir` 冒烟；失败则回退 `node:sqlite` 升级 Electron ≥33（评估后二选一，不留无方案风险）
- [acrylic 悬浮窗与鼠标穿透 forward 模式兼容性未验证] → P1 前置 spike（半天）；不兼容则毛玻璃预设下锁定=仅不可拖（点击被吞），文档如实描述
- [WCO 在部分 Win10 版本表现异常（旧 LTSC）] → 保底方案：探测失败时退回系统边框启动，记录日志
- [React 化增加悬浮窗内存（约 +30~50MB）] → 悬浮窗入口极简（无路由/无 store 订阅之外的库），且用户本就在运行 GB 级模型的桌面场景，可接受
- [自动更新无签名，SmartScreen 拦截] → README 说明 + 保留手动下载渠道；更新失败静默降级不影响使用
- [配置一次性迁移丢失用户手改 config.yaml] → 迁移前备份 `config.yaml.bak`，设置"高级"页提供"打开配置目录"入口
- [AppState 广播频率（vad_state 翻转 + subtitle 流）造成渲染抖动] → patch 合帧（16ms 批量）+ 渲染层 `useSyncExternalStore` 选择性订阅
- [旧 spec 行为回归（5 份 delta 之外的 overlay 锁定/托盘/快捷键语义）] → tasks 内列既有行为回归清单，P1 验收逐项打勾

## Migration Plan

分四阶段合入，每阶段独立可发布、可 revert：

- **P0 骨架迁移**（用户不可感知）：electron-vite + React 工程落地；Gateway/AppState/ConfigStore 上移主进程；overlay 以新栈等价重写（功能=现状）；删除 settings.html 前先以"主进程转发版"过渡。回滚 = revert 前端目录，后端无改动。
- **P1 主窗口 MVP**（观感焕新）：主窗口 shell/Live 视图/状态胶囊/设置页/托盘整合/onboarding 静态流程；设计 token 全面上线；WCO + acrylic spike。回滚 = 隐藏主窗口入口退回托盘-only（feature flag `ui.mainWindow`）。
- **P2 历史与导出**：SQLite + 会话侧栏/回放/搜索/导出 + 后端时间戳与 vad_state（后端改动仅此阶段合入）。
- **P3 产品完善**：自动更新、开机自启、毛玻璃预设转正、快捷键自定义 UI、多显示器选择、README 重写。

版本语义：P0 起 package.json 进入 2.0.0-alpha 序列，P3 收口发 2.0.0。

## Open Questions

- accent 终选色值（`#10a37f` 青绿 vs 蓝紫系）——P1 视觉走查时定，token 单点改动
- 会话静音自动切分的默认阈值（30min 提案）与是否默认开启（当前设计：默认关）
- onboarding 中模型下载页是否允许"跳过并后台下载"（倾向允许，字幕先出原文后补翻译体验复杂——默认不允许，下载完成前 Live 视图显示占位状态）
- GitHub Releases 的 publish token / CI 工作流归属（影响 P3 自动更新的联调时机）
