# release-packaging Specification

## Purpose
TBD - created by archiving change complete-features-and-release. Update Purpose after archive.
## Requirements
### Requirement: 构建配置单一来源

electron-builder 配置 SHALL 只存在于 `electron-builder.yml`；`package.json`  MUST NOT 含 `build` 键。构建目标 SHALL 仅保留 Windows NSIS（x64），移除无效的 mac/linux 配置。

#### Scenario: 配置无歧义

- **WHEN** 执行 `npm run dist`
- **THEN** electron-builder 仅读取 electron-builder.yml，产出 Windows 安装包，无配置冲突警告

### Requirement: 后端进程托管

打包后的应用启动时 SHALL 自动拉起随包分发的后端可执行文件，并以指数退避轮询 WebSocket 完成健康检查（上限 90 秒）。应用退出时 SHALL 结束后端进程树。后端异常退出 SHALL 托盘提示并支持一键重启。健康检查超时 SHALL 弹出含日志路径的错误对话框并允许重试。

#### Scenario: 开箱即用

- **WHEN** 用户安装后首次启动应用
- **THEN** 无需手动开任何终端，后端被自动拉起，模型下载进度在字幕窗可见，就绪后字幕正常工作

#### Scenario: 后端崩溃恢复

- **WHEN** 运行中后端进程被杀
- **THEN** 托盘出现提示，用户点击重启后服务恢复

### Requirement: PyInstaller 完整性

`build.spec` SHALL 完整收集 ctranslate2、tokenizers、sentencepiece、soxr 及 faster-whisper 数据资产（含 `silero_vad.onnx`）；SHALL NOT 打包空 models/ 目录；console 窗口 SHALL 由构建开关控制，发布构建默认关闭。

#### Scenario: 打包后端可独立运行

- **WHEN** 在干净 Windows 环境（无 Python）运行打包出的 SubtitleTranslator.exe
- **THEN** 进程正常启动、加载配置并监听 WebSocket 端口，无缺失模块/DLL 报错

### Requirement: 文件日志

后端 SHALL 向用户数据目录 `logs/backend.log` 写滚动日志（3 个文件 × 2MB），前端 SHALL 经 electron-log 写 `logs/frontend.log`；级别由配置控制。打包运行（无控制台）时排障信息 MUST 可从日志文件获得。

#### Scenario: 打包环境排障

- **WHEN** 打包应用运行出现异常
- **THEN** 用户数据目录 logs/ 下存在包含错误堆栈的当日日志

### Requirement: 安装包内容与首装体验

安装包 SHALL 包含后端可执行文件、默认 config.yaml 模板与图标资源；SHALL NOT 内嵌模型文件（首次运行下载到用户缓存目录）。首次启动 SHALL 在用户数据目录生成可编辑的 config.yaml 副本，此后以副本为准。

#### Scenario: 首装流程

- **WHEN** 干净机器完成安装并启动
- **THEN** userData 生成 config.yaml 副本，模型开始下载且进度可见，安装包体积不含模型（安装包 <500MB）

### Requirement: 构建冒烟验证

`build.bat` SHALL 一键完成后端与前端构建；每次发布前 SHALL 执行冒烟验证：安装包安装启动、后端进程被拉起、播放测试音源 60 秒内出现正确字幕。README SHALL 同步修正不实宣传（按应用捕获、<1s 延迟）并补充分发说明。

#### Scenario: 一键构建

- **WHEN** 开发机执行 build.bat
- **THEN** 依次完成依赖安装、PyInstaller 打包、electron-builder 出包，产物路径打印清晰

