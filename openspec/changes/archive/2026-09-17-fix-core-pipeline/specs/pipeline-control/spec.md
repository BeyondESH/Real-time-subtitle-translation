# pipeline-control — 前后端控制消息链路

## ADDED Requirements

### Requirement: 控制消息协议

前后端控制消息 SHALL 使用 WebSocket JSON 格式 `{type: "control", action: <string>, ...}`。后端 SHALL 支持 `pause`、`resume`、`change_model` 三种 action。未知 action SHALL 记录日志并忽略，MUST NOT 导致异常或连接断开。

#### Scenario: 未知控制消息容错

- **WHEN** 客户端发送 `{type: "control", action: "explode"}`
- **THEN** 后端记录 WARNING 日志，连接保持，后续消息正常处理

### Requirement: 暂停与恢复端到端贯通

前端触发暂停/恢复时 SHALL 通过 WebSocket 向后端发送对应 control 消息。后端收到 `pause` 后 SHALL 进入暂停语义（见 audio-streaming 的暂停要求）；收到 `resume` 后恢复。前端自身 ALSO SHALL 暂停字幕渲染，两层状态保持一致。

#### Scenario: 快捷键暂停全链路生效

- **WHEN** 用户按下暂停快捷键
- **THEN** 后端在下一个音频周期内停止向 ASR 输送音频，前端不再渲染新字幕，GPU 推理负载下降

#### Scenario: 恢复后继续

- **WHEN** 用户在暂停后按下恢复快捷键
- **THEN** 后端恢复音频处理，下一条语句的字幕正常出现在前端

### Requirement: 切换模型消息

前端注册切换模型快捷键后，触发时 SHALL 发送 `{type: "control", action: "change_model", model_size: <name>}`。后端处理规则见 model-lifecycle 的模型切换原子性要求。

#### Scenario: 快捷键换模型

- **WHEN** 用户按下切换模型快捷键
- **THEN** 后端收到 change_model 消息并切换到循环列表中的下一个模型，完成后前端可通过模型信息查询确认
