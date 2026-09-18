# Third-party license archive — translation models

本目录归档翻译模型与运行时的许可证文本及 pin 记录（打包随附与合规审计用）。

## Hy-MT2 系（默认翻译模型）

| 项 | 值 |
|---|---|
| 仓库 | `tencent/Hy-MT2-1.8B-GGUF` |
| Pin revision | `a0c709d9fac510f2c807aa3af52872340dc37a4a`（2026-09-18 记录） |
| 当前许可 | **Apache-2.0** |
| 归档文件 | `Hy-MT2-1.8B-LICENSE.txt`（取自 pin 的 revision） |

**许可证历史（重要）**：Hy-MT2 原始发布版（2026-05-21，commit `cf56b463…`）的
`LICENSE.txt` 为**腾讯 HY 社区许可**（含地域限制：排除欧盟等条款与 1 亿 MAU 条款）；
其后经 "Update license metadata" 提交（`9a341cd…`）重新授权为 **Apache-2.0**。
发布分发前须复核所 pin revision 的 LICENSE 文件与本文档一致。

同族 `tencent/Hy-MT2-7B-GGUF`（pin `ab847266…`）许可历史与文本同 1.8B。

## Qwen3-1.7B（可选档）

| 项 | 值 |
|---|---|
| 仓库 | `bartowski/Qwen_Qwen3-1.7B-GGUF`（Qwen3 系 Apache-2.0，社区 GGUF 转换） |
| Pin revision | `dcb19155b962dbb6389f4691a982043a8e651022` |

## llama.cpp 运行时（翻译引擎）

| 项 | 值 |
|---|---|
| 组件 | `llama-server` 与 ggml 系 DLL（随包 `backend/vendor/llama/`，经 `SUBTITLE_LLAMA_DIR` 定位） |
| 版本 pin | **b11029**（llama-server 自报 `0.4.1-dev`，commit `5c53396b8`，2026-09-18 下载） |
| 构建 | `win-x64-cpu`（纯 CPU 兜底）与 `win-x64-cuda`（CUDA 12.4，含 cudart/cublas/cublasLt 运行库） |
| 许可 | MIT — 归档文件 `llama.cpp-LICENSE.txt`（Copyright (c) 2023-2026 The ggml authors） |
| zip 校验 | 见 `backend/vendor/llama/README.md`（SHA256 清单与复现脚本 `backend/scripts/vendor_llama.ps1`） |

随构建分发的其他组件：
- **libomp（LLVM OpenMP）**：Apache-2.0 with LLVM Exceptions，`LICENSE-LLVM-OpenMP` 随各解包目录保留；
- **CUDA 运行库（cudart/cublas/cublasLt）**：NVIDIA CUDA Toolkit EULA（允许随应用再分发）。
