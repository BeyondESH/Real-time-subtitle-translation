# model-lifecycle Specification

## Purpose
TBD - created by archiving change fix-core-pipeline. Update Purpose after archive.
## Requirements
### Requirement: 线程安全的进度上报

模型下载/加载进度回调在任意线程被触发时，系统 SHALL 通过 `loop.call_soon_threadsafe` 切换到主事件循环后再发送 WebSocket 消息。进度上报 MUST NOT 导致模型初始化失败或抛出 `RuntimeError: no running event loop`。

#### Scenario: 首次下载翻译模型

- **WHEN** 全新环境首次运行，翻译模型需要从网络下载
- **THEN** 前端持续收到 `model_progress` 消息，初始化正常完成，不出现"加载模型失败"误报

#### Scenario: 无事件循环线程中触发回调

- **WHEN** worker 线程内的加载函数报告进度
- **THEN** 进度消息被调度到主事件循环发送，回调本身不抛异常

### Requirement: 模型分级加载

系统 SHALL 按以下时机加载模型：Whisper ASR 模型在服务启动时加载；日中主翻译模型在 ASR 就绪后以后台任务预载（`translation.preload_primary` 可关）；NLLB 全语种模型在首次需要非日中语言对时懒加载（`translation.lazy_load` 可关）。模型加载 SHALL 由锁串行化，同一模型不重复加载。

#### Scenario: 启动只加载 ASR

- **WHEN** 服务启动完成且尚未产生任何翻译请求
- **THEN** ASR 模型已就绪，NLLB 未加载，内存中不存在 NLLB 权重

#### Scenario: 首次触发懒加载

- **WHEN** 首次出现英→中翻译请求且 NLLB 尚未加载
- **THEN** 该请求等待加载完成（期间前端持续收到进度消息），随后返回翻译结果，后续请求不再等待

### Requirement: 模型切换原子性

切换 ASR 模型 SHALL 在锁保护下完成：切换期间新语句按背压规则丢弃；切换完成后由新模型服务。切换到不受支持的模型名 SHALL 返回错误回执且不破坏当前模型状态。

#### Scenario: 热切换模型

- **WHEN** 运行中收到 `change_model`（如 base→small）
- **THEN** 旧模型释放、新模型加载完成前到达的语句被安全丢弃，加载完成后识别继续

#### Scenario: 非法模型名

- **WHEN** 收到 `change_model` 且模型名不在支持列表
- **THEN** 客户端收到 `type=error, code=invalid_model` 回执，当前模型继续工作

### Requirement: 设备检测与回退

`device: auto` 时系统 SHALL 优先使用 CUDA；CUDA 不可用 SHALL 回退 CPU 并将 ASR 计算类型降为 int8，回退过程 MUST 记录日志。

#### Scenario: 无 GPU 环境

- **WHEN** 在无 NVIDIA 显卡的机器上启动
- **THEN** ASR 以 cpu+int8 加载，日志中可见回退说明，服务正常可用

