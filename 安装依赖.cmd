@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "F:\ProgramData\anaconda3\python.exe" (
  "F:\ProgramData\anaconda3\python.exe" -m venv "%~dp0.venv"
) else (
  python -m venv "%~dp0.venv"
)
if errorlevel 1 (
  echo 创建 Python 环境失败，请确认已安装 Python 3.10 或以上版本。
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo 安装依赖失败，请检查网络连接后重试。
  pause
  exit /b 1
)
echo 安装完成，可以双击“启动挂机.cmd”。
pause
