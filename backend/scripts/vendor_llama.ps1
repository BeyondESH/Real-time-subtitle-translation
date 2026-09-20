# 下载并解包 pin 版 llama.cpp Windows 二进制（见 backend/vendor/llama/README.md）
# 用法: pwsh -File backend/scripts/vendor_llama.ps1 [-CudaOnly] [-Force]
param(
    [switch]$CudaOnly,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Version = 'b11029'
$BaseUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$Version"

$Components = @(
    @{ Name = 'cpu';    File = "llama-$Version-bin-win-cpu-x64.zip";        Sha256 = 'F617254581251F257EBCA1513A90399E837BDD564AC8897ABE67FC3FE9D0E673'; Dir = 'win-x64-cpu' },
    @{ Name = 'cuda';   File = "llama-$Version-bin-win-cuda-12.4-x64.zip";  Sha256 = 'B9740A623323ACED02C8E4E4D9A98C16F348810C2BEE6B1FC2036092BCDB8112'; Dir = 'win-x64-cuda' },
    @{ Name = 'cudart'; File = "cudart-llama-bin-win-cuda-12.4-x64.zip";    Sha256 = '8C79A9B226DE4B3CACFD1F83D24F962D0773BE79F1E7B75C6AF4DED7E32AE1D6'; Dir = 'win-x64-cuda' }
)
if ($CudaOnly) { $Components = $Components | Where-Object { $_.Name -ne 'cpu' } }

$VendorRoot = Join-Path $PSScriptRoot '..\vendor\llama'
$CacheDir = Join-Path $env:TEMP 'llama_dl'
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-File([string]$Url, [string]$Out) {
    if ((Test-Path $Out) -and -not $Force) { Write-Host "[skip] cached: $Out"; return }
    Write-Host "[dl] $Url"
    # 企业网络环境常因吊销检查失败：curl 用 --ssl-no-revoke；无 curl 时回退 Invoke-WebRequest
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & curl.exe -L --ssl-no-revoke -s -o $Out $Url
        if ($LASTEXITCODE -ne 0) { throw "curl 下载失败: $Url" }
    } else {
        Invoke-WebRequest -Uri $Url -OutFile $Out -UseBasicParsing
    }
}

foreach ($c in $Components) {
    $zip = Join-Path $CacheDir $c.File
    Get-File "$BaseUrl/$($c.File)" $zip
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash
    if ($hash -ne $c.Sha256) { throw "sha256 不匹配: $($c.File) 期望 $($c.Sha256) 实际 $hash" }
    $dest = Join-Path $VendorRoot $c.Dir
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Write-Host "[unzip] $($c.File) -> $dest"
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $dest, $true)
}

# ---- ORT (sherpa-onnx) CUDA EP 运行库：cudnn9 / cufft / nvrtc / nvjitlink ----
# 识别引擎（Fun-ASR-Nano/sherpa-onnx CUDA 变体）经独立 onnxruntime CUDA EP 推理，
# 需 cuDNN9 全套 + cufft64_11 + nvrtc + nvjitlink（缺失时报 126 / Failed to load shared library）。
# 来源：NVIDIA pip 包（nvidia-*）；复制进 win-x64-cuda 后由引擎启动时的 PATH 注入解析。
Write-Host '[pip] nvidia cudnn/cufft/nvrtc/nvjitlink (cu12)'
python -m pip install --quiet nvidia-cudnn-cu12 nvidia-cufft-cu12 nvidia-cuda-nvrtc-cu12 nvidia-nvjitlink-cu12
if ($LASTEXITCODE -ne 0) { throw 'nvidia 运行库 pip 安装失败' }
$SitePackages = (python -c "import site; print(site.getsitepackages()[0])").Trim()
$CudaDest = Join-Path $VendorRoot 'win-x64-cuda'
New-Item -ItemType Directory -Force -Path $CudaDest | Out-Null
foreach ($sub in @('cudnn\bin', 'cufft\bin', 'cuda_nvrtc\bin', 'nvjitlink\bin')) {
    $src = Join-Path $SitePackages "nvidia\$sub"
    if (Test-Path $src) {
        Copy-Item (Join-Path $src '*.dll') $CudaDest -Force
        Write-Host "[copy] $src\*.dll -> $CudaDest"
    } else {
        Write-Host "[warn] 未找到 $src（跳过）"
    }
}

Write-Host 'vendor 完成。冒烟: backend/vendor/llama/win-x64-cuda/llama-server.exe --list-devices；'
Write-Host '识别 GPU 冒烟: python -X utf8 backend/scripts/try_cuda_load.py（需已下载 ASR 模型）'
