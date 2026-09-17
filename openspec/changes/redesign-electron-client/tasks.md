# Tasks: redesign-electron-client

阶段语义（design.md Migration Plan）：P0 骨架迁移（用户不可感知）→ P1 主窗口 MVP（观感焕新）→ P2 历史与导出（含后端协议扩展）→ P3 产品完善与发布。每阶段收口时全量回归既有 spec 行为。

## 1. P0 构建体系与设计基座

- [x] 1.1 引入 electron-vite：新建 `electron.vite.config.ts`（main / preload / renderer 三目标，renderer 含 `index.html` 与 `overlay.html` 双 MPA 入口），`frontend/src` 重组为 `main/`、`preload/`、`renderer/` 目录结构，npm scripts 改为 dev/build（HMR），移除 tsc + copy-assets 旧链路；`npm run dev` 可起现状功能的过渡壳
- [x] 1.2 安装 P0 依赖：react、react-dom、react-router-dom、tailwindcss、lucide-react、vitest（含 @testing-library 基础件）；确认 Electron 28 + TS strict 编译干净
- [x] 1.3 落地设计 token：`renderer/styles/tokens.css`（暗/亮双主题 CSS 变量，色值/文字双色阶/圆角阶梯/动效时长按 design.md D8 表）+ Tailwind theme 全部映射 CSS 变量 + `data-theme` 属性切换机制（含跟随系统探测）
- [x] 1.4 实现薄 UI 组件层 `components/ui/`：Button、Toggle、Slider、Select、Modal、Pill、SegmentedNav、ListItem、StatusDot、ProgressBar、EmptyState、FocusRing 约定（focus-visible），全部仅引用 token，无硬编码色值/圆角
- [x] 1.5 vitest 测试骨架 + 源码色值扫描脚本（断言渲染层除 token 文件外无 #hex/rgb 字面量，对应 design-system"源码无散落硬编码"场景）

## 2. P0 主进程架构（Gateway / AppState / ConfigStore）

- [x] 2.1 `main/gateway.ts`：主进程唯一 WS 客户端——连接生命周期 + 指数退避重连（250ms→30s）+ `request(method,params)→Promise`（唯一 id、pending map、10s 超时、ok:false 类型化错误）；渲染层零直连（删除 overlay 内 WS 逻辑的前置件）
- [x] 2.2 `main/state.ts` + `main/actions.ts`：AppState 字段全集（connection/capture/vad/model/modelDownload/activeLanguage/targetLanguages/audioSource/locked/overlayVisible/lastWarning/droppedCount）；typed action union + 纯函数 reducer；`dispatch(action)` 唯一变更入口；patch 合帧广播（16ms 批量）到全部存活窗口；新窗口先全量快照后增量
- [x] 2.3 Gateway 广播路由：subtitle → IPC `stream:subtitle` 分发；model_progress → AppState.modelDownload；pipeline_warning → lastWarning/droppedCount + 告警事件；error → 日志 + 类型化错误事件；vad_state → AppState.vad（字段缺失容错：旧后端不报错不显示）
- [x] 2.4 `main/config.ts`：store schema 扩展（theme、autoStart、外观预设、sessions.autoSplit、快捷键注册状态缓存等）+ `version` 字段与 electron-store migrations；一次性迁移：读旧用户 config.yaml 中 subtitle/system/shortcuts 段值导入 store（文件不重写，日志记录迁移结果）
- [x] 2.5 配置模板瘦身：根目录 `config.yaml` 移除 subtitle/system/shortcuts 段仅留后端消费段；打包 extraResources 模板同步；验证后端以瘦模板正常启动
- [x] 2.6 Gateway 偏好对齐：连接建立（含重连）后发 `config_sync`；store 模型/音频源与后端运行态差异时分别发 `change_model`/`set_audio_source`，幂等实现 + 单测
- [x] 2.7 `preload/index.ts` 重写：typed bridge——`getState`/`onStatePatch`/`onSubtitle`/`dispatch`/`wsRequest` + 窗口控制 invoke；移除旧 `electronAPI` 面；渲染进程 `typeof require === 'undefined'` 断言保持
- [x] 2.8 `main/backend-manager.ts`：自旧 main.ts 迁移进程托管（spawn/端口探活指数退避/taskkill 进程树/异常退出托盘提示/超时对话框重试）；健康与存活状态写入 AppState 而非局部变量
- [x] 2.9 托盘与快捷键改走 dispatch：托盘菜单七项（显示主窗口[P1 前指向占位或禁用]、显示/隐藏字幕、暂停/恢复、锁定字幕位置、重启后端服务、设置、退出）；四个全局快捷键动作统一 dispatch，注册失败状态入 store（供 P1 设置页警示）
- [x] 2.10 `renderer/overlay/` 新栈重写（功能等价现状）：订阅 onSubtitle/state-patch；最近 N 条与移除规则、双行/仅译文模式、激活语言回退原文、字体样式来自配置、暂停丢弃语义、锁定穿透 + 解锁拖拽 + 位置持久化；毛玻璃/动效留 P1
- [x] 2.11 单测齐备：state reducer（三源动作一致性）、gateway 编解码与 pending map/超时（mock WS）、重连退避序列、偏好对齐幂等
- [x] 2.12 P0 回归与打包冒烟：既有 8 份 spec 行为逐项清单核验（重点：overlay-window 锁定/快捷键/安全边界/托盘、settings-management 同步与实时生效、subtitle-display 全部渲染规则）；`npm run dist`（--dir 先行）安装冒烟：拉起后端、60 秒内出字幕、退出回收进程树

## 3. P1 主窗口与新设计语言上线

- [x] 3.1 `main/windows/main.ts`：WCO 无边框主窗口——`titleBarOverlay`（配色随主题 `setTitleBarOverlay` 同步）、drag/no-drag 区域、单例聚焦还原、位置尺寸持久化、关闭=隐藏到托盘（仅托盘"退出"真退出）、WCO 不可用降级系统边框并记日志
- [x] 3.2 应用壳：HashRouter 路由（/live 默认、/session/:id、/settings/*、/onboarding）+ 240px 可折叠侧栏（折叠态持久化）+ 主题切换全窗口联动（对应 design-system 暗亮双主题场景）
- [x] 3.3 Live 直播流视图：字幕卡片（译文主行/原文次行/脚注时间戳+语言对+延迟）、自动滚动 + 用户上滚暂停跟随 + "回到最新"、空态引导卡、暂停态提示、断连与模型下载占位状态、"聆听中"脉冲（vad 缺失时不显示）
- [x] 3.4 状态胶囊条：音频源/模型/激活语言/连接状态四胶囊 + 就地切换面板（复用 dispatch，与快捷键/托盘同源）+ 后端错误回执时胶囊回退原值与可消失错误提示
- [x] 3.5 设置页六分段（改动即落盘即生效，无保存/取消）：通用（主题三态、开机自启开关——setLoginItemSettings + 打开页回读系统状态校准 + 失败回退提示）；字幕外观（字体样式 + 外观预设选择）；音频（经 wsRequest 枚举、加载态、未连接态与重试）；模型（档位选择 + 下载进度呈现）；快捷键（全量展示 + 注册失败警示项）；高级（打开配置目录/日志目录）
- [x] 3.6 悬浮窗视觉刷新：入场动效（250ms fade+8px 上浮、并行不排队、prefers-reduced-motion 降级）、暂停徽标（≤16px、≤50% 透明、不遮挡文字）、告警细条（琥珀、累计丢弃数、2s 自动消失、连续告警刷新不堆叠）
- [x] 3.7 毛玻璃胶囊预设：先做半天 spike（Win11 acrylic + setIgnoreMouseEvents forward 兼容性，记录结论）；实现预设切换=窗口重建（恢复位置/锁定/可见性）；Win10 检测不支持则设置项禁用并注明"需要 Windows 11"
- [x] 3.8 首次运行引导：四步流程（欢迎 → 音频源选择[wsRequest 拉取 + 默认预选] → 模型下载[model_progress 驱动进度] → 完成进入 /live）+ 跳过（后台继续下载，进度转直播流占位态）+ store 首启标记（二启不再触发）
- [x] 3.9 删除 `settings.html` 与独立设置窗口代码路径；托盘"设置"深链主窗口设置页通用分段；P1 验收：main-window、settings-management、subtitle-display、overlay-window、design-system 各 delta 场景逐条走查打勾

## 4. P2 会话历史与后端协议扩展

- [x] 4.1 后端时间戳透传：UtteranceSegmenter 切句时间随 utterance 对象 → 队列 → `pipeline_worker` 的 subtitle 广播新增 `ts_start`/`ts_end`（既有字段语义不变）；`backend/tests/` 增消息形状断言；旧前端（当前 release 版）对新消息无解析错误的兼容验证
- [x] 4.2 后端 vad_state 广播：切句循环检测语音状态翻转，speech 立即发、silence 去抖 300ms、仅翻转时发、无客户端静默跳过；tests 增补（翻转事件、200ms 短间隙不抖动、无客户端安全）
- [x] 4.3 P2 打包冒烟前置：better-sqlite3 随 `--dir` 打包的原生模块 ABI 验证（失败则触发 design.md 风险预案：升 Electron ≥33 用 node:sqlite，并更新 D4）
- [x] 4.4 `main/history-db.ts`：schema（sessions/utterances + 索引）建库迁移；subtitle 到达即同步写入（含激活语言译文、时间戳或 received_at；写入失败记日志不中断分发）；userData/history.db
- [x] 4.5 会话生命周期：启动自动新会话（空会话复用）、手动新会话（封口 + ended_at + 直播流清空）、静音 N 分钟自动切分（默认关、阈值可配）、默认标题=首句译文前 12 字、重命名、删除级联 + 二次确认（删活跃会话自动开新）
- [x] 4.6 会话侧栏：今天/昨天/更早分组列表、活跃高亮、右键菜单（重命名/删除/导出）、＋新会话、折叠联动；点击历史会话打开 /session/:id 回放页（卡片样式复用、起止时间与统计、滚动流畅 200+ 条）
- [x] 4.7 全文搜索：LIKE 匹配原文与译文（不区分大小写）、按会话分组结果 + 命中上下文摘要、点击跳转会话并定位高亮语句、空态
- [x] 4.8 `main/exporters/`：srt/txt/md/json 四格式纯函数导出器（SRT 优先 ts_start/ts_end，缺失以 received_at 近似并在输出注明）+ vitest 全覆盖（含精确/降级/JSON 往返三场景）
- [x] 4.9 导出 UI：回放页与侧栏右键导出入口、系统保存对话框、成功/失败明确反馈
- [x] 4.10 容量治理：高级分段显示 history.db 占用大小、"清空全部历史"（二次确认 + 级联删除 + VACUUM 收缩 + 清空后新字幕正常入新会话）
- [x] 4.11 P2 验收：session-history 与 pipeline-control delta 全部场景逐条走查；主窗口隐藏期间字幕照常入库的专项验证

## 5. P3 产品完善与发布

- [x] 5.1 自动更新：electron-builder.yml 增 publish（GitHub provider）；electron-updater 集成——启动延迟静默检查、设置通用页"检查更新"及结果反馈、新版本后台下载 + 主窗口状态区进度、完成后托盘通知"重启安装"（可推迟至下次退出应用）、断网/无发布源静默记日志
- [x] 5.2 多显示器：设置内悬浮窗显示器选择（枚举 displays）、窗口位置按显示器记忆、拔插显示器后位置越界自动回收进可视区
- [x] 5.3 快捷键自定义 UI：设置快捷键分段支持重录组合键、冲突检测与注册失败即时警示（联动 store 注册状态）、恢复默认
- [x] 5.4 发布工程：`build.bat` 适配新构建链（electron-vite build → electron-builder）、版本号进入 2.0.0 序列、NSIS 产物命名与安装/升级路径验证（1.x 覆盖安装保历史数据）
- [x] 5.5 README 全面更新：功能清单、新界面预览（主窗口/悬浮窗两形态 ASCII 或截图位）、项目结构（新目录树）、配置职责表（store 偏好 vs config.yaml 管线）、快捷键表、自动更新与 SmartScreen 未签名说明
- [ ] 5.6 全量发布回归：9 份 delta spec 全部场景作为验收清单逐项打勾；backend pytest 全绿；frontend vitest 全绿；`build.bat` 一键出包 + 干净环境安装冒烟（首启引导 → 模型下载 → 60 秒出字幕 → 历史入库 → 导出 SRT）
- [x] 5.7 收尾：`openspec validate --change redesign-electron-client` 通过；归档准备（5 份 delta 同步回 openspec/specs，4 份新能力入库）
