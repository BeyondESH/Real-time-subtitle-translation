# subtitle-display 增量规格

## ADDED Requirements

### Requirement: 字幕入场动效

悬浮字幕窗中每条新字幕 SHALL 以 250ms ease-out 淡入加 8px 上浮入场；多条字幕连续到达时动效 SHALL 并行播放，MUST NOT 排队阻塞或跳帧。系统开启"减少动态效果"（prefers-reduced-motion）时 SHALL 取消位移与淡入，字幕直接呈现。动效 MUST NOT 改变既有的最近 N 条滚动与移除规则。

#### Scenario: 正常入场

- **WHEN** 音频播放中一条字幕到达
- **THEN** 该字幕以淡入上浮动效出现在字幕窗，最旧条目按既有规则移除

#### Scenario: 减弱动效环境

- **WHEN** 系统开启减少动态效果且字幕到达
- **THEN** 字幕立即呈现，无动画，内容与布局不受影响

### Requirement: 暂停状态徽标

捕获处于暂停状态时，字幕悬浮窗 SHALL 显示一枚低存在感暂停徽标（尺寸不超过 16px、透明度不高于 50%、不响应鼠标），恢复后 SHALL 立即消失。徽标 MUST NOT 遮挡字幕文字区域。

#### Scenario: 暂停可见性

- **WHEN** 用户按 Ctrl+Shift+Space 暂停
- **THEN** 悬浮窗角落出现暂停徽标，用户无需打开主窗口即可确认暂停已生效

#### Scenario: 恢复即消失

- **WHEN** 用户恢复捕获
- **THEN** 徽标消失，后续字幕正常渲染

### Requirement: 过载告警可视化

悬浮字幕窗收到 pipeline_warning 分发时 SHALL 在窗口顶部显示琥珀色告警细条（含累计丢弃数），约 2 秒后自动消失。告警条 MUST NOT 遮挡字幕文字，连续告警 SHALL 刷新同一条而非堆叠。

#### Scenario: 丢句告警

- **WHEN** 后端队列过载丢弃语句并广播 pipeline_warning
- **THEN** 悬浮窗顶部出现琥珀细条显示累计丢弃数，2 秒后自动消失，字幕区不受遮挡

#### Scenario: 连续告警不堆叠

- **WHEN** 2 秒内连续收到多次 pipeline_warning
- **THEN** 告警条仅刷新数字与消失计时，不出现多条重叠
