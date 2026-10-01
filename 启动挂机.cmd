@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -c "import sys; assert (3, 10) <= sys.version_info < (3, 13); import cv2, numpy, PIL, tkinter, rapidocr_onnxruntime" >nul 2>nul
  if not errorlevel 1 goto use_venv
)
if exist "F:\ProgramData\anaconda3\python.exe" (
  "F:\ProgramData\anaconda3\python.exe" -c "import sys; assert (3, 10) <= sys.version_info < (3, 13); import cv2, numpy, PIL, tkinter, rapidocr_onnxruntime" >nul 2>nul
  if not errorlevel 1 goto use_anaconda
)
where python.exe >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; assert (3, 10) <= sys.version_info < (3, 13); import cv2, numpy, PIL, tkinter, rapidocr_onnxruntime" >nul 2>nul
  if not errorlevel 1 goto use_path
)
echo No usable Python environment was found.
echo Run the dependency installer, then try again.
if /I not "%~1"=="--check" pause
exit /b 1

:use_venv
if /I "%~1"=="--check" (
  echo Launcher check passed: project .venv
  exit /b 0
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py"
exit /b 0

:use_anaconda
if /I "%~1"=="--check" (
  echo Launcher check passed: Anaconda
  exit /b 0
)
start "" "F:\ProgramData\anaconda3\pythonw.exe" "%~dp0app.py"
exit /b 0

:use_path
if /I "%~1"=="--check" (
  echo Launcher check passed: Python on PATH
  exit /b 0
)
where pythonw.exe >nul 2>nul
if errorlevel 1 (
  echo pythonw.exe was not found. Reinstall Python with its desktop components.
  pause
  exit /b 1
)
start "" pythonw.exe "%~dp0app.py"
exit /b 0
