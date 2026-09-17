# settings-management 增量规格

## REMOVED Requirements

### Requirement: 设置窗口生命周期

**Reason**: 独立设置窗口被主窗口内的应用内设置页取代（ChatGPT 式 in-app settings，见 main-window 规格"应用内设置页"与本文件新增"设置页生命周期"）。

**Migration**: 托盘菜单"设置"改为打开主窗口并深链到设置页；原设置窗全部控件按分段迁移至设置页对应分段（通用/字幕外观/音频/模型/快捷键/高级）。

## ADDED Requirements

### Requirement: 设置页生命周期

设置 SHALL 为主窗口内的分段页面（通用、字幕外观、音频、模型、快捷键、高级），各控件 SHALL 实时展示当前持久化配置值。改动即落盘（无"保存/取消"两段式）：每次控件变更 SHALL 立即写入 electron-store 并实时生效。外部入口（托盘菜单、其它页面链接）SHALL 支持深链到指定分段。设置页 MUST NOT 存在多实例（已处于设置页时切换分段即可）。

#### Scenario: 打开即当前值

- **WHEN** 用户打开设置页任一分段
- **THEN** 全部控件展示当前已持久化的配置值，与字幕窗/后端实际运行态一致

#### Scenario: 改动即持久

- **WHEN** 用户修改任一设置项后立即退出并重启应用
- **THEN** 该项以修改后的值生效，无需任何"保存"操作

#### Scenario: 深链定位

- **WHEN** 托盘菜单"设置"在主窗口已打开设置页时被点击
- **THEN** 视图定位到通用分段，不产生第二个设置界面

### Requirement: 开机自启

设置"通用"分段 SHALL 提供开机自启开关：状态持久化于 electron-store，并经系统登录项（`setLoginItemSettings`）实际生效或取消。打开设置页时开关 SHALL 回读系统登录项真实状态校准（两者不一致时以系统为准并修正 store）。启用/禁用失败 SHALL 显示错误提示且开关回退，MUST NOT 出现开关状态与实际登录项不一致而无提示的情形。

#### Scenario: 开启后系统自启

- **WHEN** 用户开启开机自启并重启操作系统后登录
- **THEN** 应用随登录自动启动并进入托盘常驻形态

#### Scenario: 关闭后登录项移除

- **WHEN** 用户关闭开机自启
- **THEN** 系统登录项中该应用的条目被移除，store 状态同步为关闭

### Requirement: 高级页运维入口

设置"高级"分段 SHALL 提供"打开配置目录"与"打开日志目录"两个系统入口按钮：配置目录含用户 config.yaml 副本与 store 文件，日志目录含前端与后端日志文件。点击 SHALL 以系统文件管理器打开对应目录；目录缺失时 SHALL 先行创建。

#### Scenario: 打开日志目录

- **WHEN** 用户点击"打开日志目录"
- **THEN** 系统文件管理器打开 userData/logs，其中可见 backend.log 与 frontend.log

## MODIFIED Requirements

### Requirement: 配置双源边界与同步

electron-store SHALL 是用户偏好（字幕样式与外观预设、窗口位置与外观、目标与激活语言、快捷键、锁定状态、模型选择、音频源、主题、开机自启）的唯一来源。config.yaml SHALL 仅承载后端管线参数（audio/pipeline/vad/asr/translation/websocket 段）：首装时复制模板到用户数据目录，此后以副本为准；副本允许用户手工编辑，前端 MUST NOT 覆盖已存在的副本。分发模板与用户副本 MUST NOT 包含 subtitle/system/shortcuts 前端专用段（存量副本中遗留的此类段 SHALL 在升级首启时做一次性迁移导入 store，此后忽略，原文件不重写）。Gateway 连接建立（含重连）后 SHALL 将后端运行态对齐到 store 偏好：发送 `config_sync`（target_languages、active_language）；store 的 Whisper 模型与后端当前模型不一致时发送 `change_model`；音频源不一致时发送 `set_audio_source`。

#### Scenario: 启动同步

- **WHEN** 前端启动且 Gateway 连上后端 WebSocket
- **THEN** 后端收到 config_sync，后续翻译使用 store 同步的目标语言与激活语言

#### Scenario: 差异对齐

- **WHEN** 连接建立时 store 中模型为 small 而后端以 base 启动
- **THEN** Gateway 下发 change_model small，回执成功后 AppState.model 为 small，无需用户操作

#### Scenario: 首装副本仅含后端段

- **WHEN** 全新安装后首次启动
- **THEN** userData 生成的 config.yaml 副本仅含后端消费段，字幕样式与快捷键全部由 store 管理

#### Scenario: 用户手改管线参数被尊重

- **WHEN** 用户手工编辑副本中 vad.threshold 后重启应用
- **THEN** 后端按文件值运行，前端不重写该文件

#### Scenario: 旧配置迁移

- **WHEN** 从旧版本升级且用户副本中存在 subtitle/system/shortcuts 段的自定义值
- **THEN** 升级首启将这些值导入 store（如开机自启、字体字号），原文件保留，日志记录迁移结果

### Requirement: 设置实时生效

设置变更后（改动即落盘，无保存/取消两段式）：字幕样式与窗口外观 SHALL 经状态广播实时应用到悬浮窗；目标语言、激活语言、Whisper 模型、音频源 SHALL 经主进程 Gateway 推送后端并在下一次处理周期生效，无需重启应用或窗口。模型与音频源变更 SHALL 有成功/失败回执：失败时 UI 状态 SHALL 回退为原值并显示可消失的错误提示。

#### Scenario: 切换音频源

- **WHEN** 用户在设置页音频分段选择另一回环设备
- **THEN** 后端切换到该设备继续捕获；失败时设置页该控件回退原值并显示错误提示

#### Scenario: 改样式即时生效

- **WHEN** 用户在字幕外观分段调整字号
- **THEN** 悬浮窗文字随每次调整实时变化，无需提交动作

### Requirement: 音频源枚举

设置页音频分段的设备列表 SHALL 通过主进程 Gateway 的请求桥（`get_audio_sources` 请求/响应）从后端实时拉取，拉取中 SHALL 显示加载态，拉取失败 SHALL 显示可重试的错误态而非假数据。

#### Scenario: 拉取设备列表

- **WHEN** 用户打开设置页音频分段
- **THEN** 设备下拉展示经 Gateway 返回的全部回环设备名称，当前音频源被标记

#### Scenario: 后端未连接

- **WHEN** 打开音频分段时后端 WebSocket 未连接
- **THEN** 设备列表显示"后端未连接"并提供重试入口，点击重试在连接恢复后可成功拉取
