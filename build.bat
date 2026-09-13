@echo off
REM 实时字幕翻译 - 构建脚本

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

echo.
echo [1/4] 安装 Python 依赖...
cd backend
pip install -r requirements.txt
pip install pyinstaller
cd ..

echo.
echo [2/4] 构建 Python 后端...
cd backend
pyinstaller build.spec --clean
cd ..

echo.
echo [3/4] 安装 Electron 依赖...
cd frontend
call npm install
cd ..

echo.
echo [4/4] 构建 Electron 前端...
cd frontend
call npm run dist
cd ..

echo.
echo ========================================
echo   构建完成！
echo ========================================
echo.
echo 输出目录：
echo   - Python: backend/dist/
echo   - Electron: frontend/release/
echo.
pause
