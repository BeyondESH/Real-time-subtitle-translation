# Vendored llama.cpp Binaries

本目录存放随包分发的 llama.cpp `llama-server` 二进制（翻译引擎运行时）。
二进制不入 git（见根 `.gitignore`），由 `backend/scripts/vendor_llama.ps1` 可复现地下载解包。

## Pin 记录

| 项 | 值 |
|---|---|
| 版本 | **b11029**（llama-server 自报 `0.4.1-dev`, commit `5c53396b8`） |
| 发布日期 | 2026-09-18 |
| 下载日期 | 2026-09-18 |
| 来源 | `https://github.com/ggml-org/llama.cpp/releases/tag/b11029` |

## 组件与校验（zip SHA256）

| 文件 | 用途 | zip 大小 | SHA256 |
|---|---|---|---|
| `llama-b11029-bin-win-cpu-x64.zip` | CPU 构建（兜底路径） | 18.4 MB | `F617254581251F257EBCA1513A90399E837BDD564AC8897ABE67FC3FE9D0E673` |
| `llama-b11029-bin-win-cuda-12.4-x64.zip` | CUDA 12.4 构建（GPU 路径） | 254.2 MB | `B9740A623323ACED02C8E4E4D9A98C16F348810C2BEE6B1FC2036092BCDB8112` |
| `cudart-llama-bin-win-cuda-12.4-x64.zip` | CUDA 运行库（cudart/cublas/cublasLt），仅驱动机运行所需 | 391.4 MB | `8C79A9B226DE4B3CACFD1F83D24F962D0773BE79F1E7B75C6AF4DED7E32AE1D6` |

## 目录布局

```
backend/vendor/llama/
├── win-x64-cpu/     # 解包自 CPU zip（~45 MB）
├── win-x64-cuda/    # 解包自 CUDA zip + cudart zip 的 3 个 DLL（~1.1 GB）
└── README.md        # 本文件
```

- `win-x64-cpu/llama-server.exe`：纯 CPU 构建。
- `win-x64-cuda/llama-server.exe`：CUDA 12.4 构建；`cublas64_12.dll` / `cublasLt64_12.dll` / `cudart64_12.dll` 来自 cudart zip，保证"仅装 NVIDIA 驱动、无 CUDA Toolkit"的用户机可运行。
- 两个 zip 均含 `LICENSE-LLVM-OpenMP`（libomp 的 LLVM 许可），已随解包保留在各目录中。

## 许可证

| 组件 | 许可 |
|---|---|
| llama.cpp（llama-server 及 ggml 系 DLL） | MIT — https://github.com/ggml-org/llama.cpp/blob/master/LICENSE |
| CUDA 运行库（cudart/cublas/cublasLt） | NVIDIA CUDA Toolkit EULA（允许随应用再分发，见 NVIDIA 官方条款） |
| libomp（LLVM OpenMP） | Apache-2.0 with LLVM Exceptions（`LICENSE-LLVM-OpenMP`） |

## 复现

```powershell
pwsh -File backend/scripts/vendor_llama.ps1            # 下载并解包到本目录
pwsh -File backend/scripts/vendor_llama.ps1 -CudaOnly  # 仅 CUDA 部件
```

脚本内嵌上述 pin 与校验值；如需升级 llama.cpp 版本，更新脚本中的版本号与哈希并同步本 README。
