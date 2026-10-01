@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py"
  exit /b
)
if exist "F:\ProgramData\anaconda3\pythonw.exe" (
  start "" "F:\ProgramData\anaconda3\pythonw.exe" "%~dp0app.py"
  exit /b
)
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
  start "" pythonw.exe "%~dp0app.py"
  exit /b
)
echo 未找到 Python。请安装 Python 3.10 或以上版本，并运行“安装依赖.cmd”。
pause
