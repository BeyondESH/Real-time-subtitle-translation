@echo off
chcp 65001 >nul
REM 实时字幕翻译 - 构建脚本
REM 用法: build.bat          发布构建（无控制台窗口）
REM       build.bat --console 调试构建（后端带控制台窗口）

setlocal
set CONSOLE=0
if "%~1"=="--console" set CONSOLE=1

echo ========================================
echo   实时字幕翻译 - 构建脚本
echo ========================================

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

REM 检查 Node.js
node --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Node.js，请先安装 Node.js 16+
    pause
    exit /b 1
)

REM 构建前检查：图标与配置必须存在
if not exist "frontend\resources\icon.ico" (
    echo [错误] 缺少 frontend\resources\icon.ico
    pause
    exit /b 1
)
if not exist "frontend\resources\icon.png" (
    echo [错误] 缺少 frontend\resources\icon.png
    pause
    exit /b 1
)
if not exist "config.yaml" (
    echo [错误] 缺少 config.yaml
    pause
    exit /b 1
)

echo.
echo [1/4] 安装 Python 依赖...
cd backend
pip install -r requirements.txt
if errorlevel 1 goto :error
pip install pyinstaller
if errorlevel 1 goto :error
cd ..

echo.
echo [2/4] 构建 Python 后端...
cd backend
pyinstaller build.spec --clean --noconfirm
if errorlevel 1 goto :error
cd ..

echo.
echo [3/4] 安装 Electron 依赖...
cd frontend
call npm install
if errorlevel 1 goto :error
cd ..

echo.
echo [4/4] 构建 Electron 前端...
cd frontend
call npm run dist
if errorlevel 1 goto :error
cd ..

echo.
echo ========================================
echo   构建完成！
echo ========================================
echo.
echo 输出目录：
echo   - Python: backend\dist\
echo   - Electron: frontend\release\
echo.
pause
exit /b 0

:error
echo.
echo [错误] 构建失败，请检查上方输出
pause
exit /b 1
