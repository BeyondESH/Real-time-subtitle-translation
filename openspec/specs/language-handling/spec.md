# language-handling Specification

## Purpose
TBD - created by archiving change fix-core-pipeline. Update Purpose after archive.
## Requirements
### Requirement: 源语言唯一认定

管线中的源语言 SHALL 以 faster-whisper 识别结果中的 `info.language` 为唯一依据。系统 MUST NOT 在管线内对识别文本做二次语言检测（langdetect 依赖随之移除）。

#### Scenario: 日语语音识别后直接翻译

- **WHEN** Whisper 判定语句语言为 `ja`
- **THEN** 翻译模块直接以 `ja` 为源语言走日中主模型，不再经过任何文本语言检测

### Requirement: 语言代码规范化

系统 SHALL 提供统一的语言代码规范化函数：`zh-cn`、`zh-tw`、`zh-hans`、`zh-hant` 等变体 SHALL 归一为 `zh`；大小写、连字符差异 SHALL 被消化。规范化 SHALL 是纯函数，可独立单元测试。

#### Scenario: 中文变体归一

- **WHEN** 输入 `zh-cn`、`zh-TW`、`ZH` 任一形式
- **THEN** 规范化结果均为 `zh`

### Requirement: 翻译语言对映射

NLLB 语言代码 SHALL 仅从显式映射表查取。目标语言或源语言不在映射表中时，系统 SHALL 返回明确的"未支持的语言对"占位结果并记录日志，MUST NOT 动态拼接语言代码（如 `zh-cn_Latn`），MUST NOT 抛出未处理异常。

#### Scenario: 未映射语言安全降级

- **WHEN** 请求一个映射表不存在的语言对翻译
- **THEN** 该目标语言的结果为占位提示串，其余目标语言的翻译不受影响，进程不崩溃

### Requirement: 同语言跳过

当源语言与某目标语言相同时，系统 SHALL 跳过该语言对的翻译调用，不占用翻译模型推理资源。

#### Scenario: 中文语音目标含中文

- **WHEN** 源语言为 `zh`，目标语言列表含 `zh`、`en`
- **THEN** 不产生 zh→zh 的推理调用，仅返回 `en` 的翻译结果

