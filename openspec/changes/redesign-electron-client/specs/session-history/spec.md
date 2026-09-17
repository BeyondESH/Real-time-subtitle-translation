# session-history 增量规格（新能力）

## ADDED Requirements

### Requirement: 语句持久化

Gateway 每收到一条 `subtitle` 广播 SHALL 立即将其写入主进程 SQLite（表 utterances：session_id、received_at、ts_start/ts_end 可空、original、source_lang、translation、target_lang、model），translation 取激活目标语言的译文。主窗口未打开或未显示时 SHALL 照常记录。单条写入失败 SHALL 记录日志并继续分发该条字幕到窗口，MUST NOT 中断直播或抛出到事件循环。数据库文件 SHALL 位于 userData 目录（history.db）。

#### Scenario: 主窗口关闭时字幕仍入库

- **WHEN** 用户仅使用悬浮字幕窗（主窗口隐藏）观看 10 分钟
- **THEN** 期间全部语句均已写入 SQLite，之后打开主窗口可完整回放

#### Scenario: 写入故障不中断直播

- **WHEN** 数据库写入因磁盘异常失败
- **THEN** 该条字幕仍正常分发到窗口显示，日志记录写入错误，应用不崩溃

### Requirement: 会话生命周期与切分

应用 SHALL 维护会话（sessions：id、title、started_at、ended_at、audio_source、stats）。切分规则：应用启动时若上一会话已含语句 SHALL 自动开启新会话（空会话直接复用，不产生空壳堆积）；用户 SHALL 可经侧栏"＋新会话"手动切分；提供"静音 N 分钟自动切分"选项（默认关闭，默认阈值 30 分钟，可配置）。会话标题默认取首句译文前 12 个字符，SHALL 可重命名。会话结束（应用退出、手动切分、自动切分）SHALL 回填 ended_at。删除会话 SHALL 级联删除其全部语句，且需二次确认；删除活跃会话后 SHALL 自动开启新会话。

#### Scenario: 二启自动新会话

- **WHEN** 用户退出应用后再次启动，且上一会话已有语句
- **THEN** 侧栏出现新的活跃会话，上一会话以结束态保留在列表中

#### Scenario: 空会话不堆积

- **WHEN** 用户启动应用后未收到任何字幕即退出，再次启动
- **THEN** 复用原空会话，会话列表不增加条目

#### Scenario: 静音自动切分

- **WHEN** 自动切分开启（阈值 30 分钟）且连续 30 分钟无新语句后一条字幕到达
- **THEN** 该字幕落入新会话，旧会话 ended_at 为切分时刻

### Requirement: 历史回放与搜索

用户 SHALL 可在主窗口打开任一会话进行回放：复用直播流卡片样式按时间顺序完整呈现，显示会话起止时间与语句统计。应用 SHALL 提供全文搜索：按原文与译文匹配（不区分大小写），结果按会话分组展示命中语句及上下文摘要，点击结果 SHALL 跳转到对应会话回放页并定位到该语句。空搜索结果 SHALL 有明确空态。

#### Scenario: 回放完整性

- **WHEN** 打开一个含 200 条语句的会话
- **THEN** 全部语句按时间序渲染，滚动流畅，头部显示起止时间与条数

#### Scenario: 搜索跳转定位

- **WHEN** 用户搜索关键词且命中两个会话中的 3 条语句
- **THEN** 结果按会话分组列出；点击其中一条后主区打开该会话并滚动定位到高亮语句

### Requirement: 多格式导出

会话与搜索结果 SHALL 支持导出为四种格式：SRT（标准字幕时间轴）、TXT（原文/译文双语逐行）、Markdown（按会话组织的卡片式文档）、JSON（结构化全字段）。SRT 时间轴 SHALL 优先使用后端透传的 ts_start/ts_end；字段缺失时 SHALL 以 received_at 近似推导（句尾对齐），并在导出内容或 accompanying 说明中注明为近似时间。导出 SHALL 经系统保存对话框选择路径；导出成功/失败 SHALL 有明确反馈。导出器 SHALL 实现为不依赖 Electron 的纯函数模块并有单元测试覆盖。

#### Scenario: 精确 SRT

- **WHEN** 会话语句均含后端时间戳且用户导出 SRT
- **THEN** 生成的 SRT 时间轴与语音实际起止一致，可被播放器正常加载

#### Scenario: 旧后端降级导出

- **WHEN** 语句缺失 ts_start/ts_end（旧版后端产生）且用户导出 SRT
- **THEN** 导出仍成功，时间轴以 received_at 近似生成，条目顺序与内容完整

#### Scenario: JSON 往返

- **WHEN** 用户导出 JSON 后检视内容
- **THEN** 每条语句包含 original、translation、语言对、时间戳字段，与库中记录一致

### Requirement: 历史容量治理

设置"高级"分段 SHALL 显示历史数据库当前占用大小，并提供"清空全部历史"入口：SHALL 要求二次确认，确认后删除全部会话与语句并收缩数据库文件，清空后直播功能 SHALL 立即恢复正常记录。默认不对历史总量设上限。

#### Scenario: 清空历史

- **WHEN** 用户确认清空全部历史
- **THEN** 会话侧栏清空，数据库文件体积收缩，随后到达的字幕正常写入新会话
