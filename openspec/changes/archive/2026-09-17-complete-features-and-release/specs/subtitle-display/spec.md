# subtitle-display — 字幕渲染

## ADDED Requirements

### Requirement: 激活目标语言渲染

字幕窗 SHALL 渲染 subtitle 消息中 `active_language` 字段所指语言的译文。前端 MUST NOT 硬编码任何语言代码。当消息中缺少激活语言译文时，SHALL 回退显示原文。

#### Scenario: 激活语言为中文

- **WHEN** 收到 `{type:'subtitle', active_language:'zh', translations:{zh:'今天天气真好'}}`
- **THEN** 字幕窗主行显示"今天天气真好"

#### Scenario: 激活语言切换后生效

- **WHEN** 激活语言从 zh 切换为 en 后收到新 subtitle 消息
- **THEN** 主行显示英文译文，不显示中文

### Requirement: 显示模式

字幕窗 SHALL 支持 `original_and_translation` 与 `translation_only` 两种显示模式，模式变更 SHALL 实时生效且无需重启。源语言与激活语言相同时 MUST NOT 重复显示原文行。

#### Scenario: 仅译文模式

- **WHEN** 显示模式为 `translation_only`
- **THEN** 每条字幕只渲染译文行，不渲染原文行

#### Scenario: 同源语言不重复

- **WHEN** 显示模式为 `original_and_translation` 且源语言与激活语言相同
- **THEN** 只渲染一行，不出现内容相同的原文行

### Requirement: 字幕样式实时应用

字体、字号、文字颜色、描边颜色、描边宽度 SHALL 来自 electron-store 配置（MUST NOT 硬编码于 CSS），设置保存后 SHALL 通过 IPC 实时推送到字幕窗生效并持久化。

#### Scenario: 改字号即时生效

- **WHEN** 用户在设置面板将字号从 24 改为 36 并保存
- **THEN** 字幕窗文字立即以 36px 渲染，重启应用后保持 36px

### Requirement: 字幕历史

字幕窗 SHALL 保留最近 N 条（默认 5）字幕滚动显示，超出后最旧条目移除。暂停期间收到的字幕消息 SHALL 丢弃不渲染。

#### Scenario: 历史滚动

- **WHEN** 连续收到 7 条字幕
- **THEN** 字幕窗仅显示最近 5 条，最早 2 条已移除
