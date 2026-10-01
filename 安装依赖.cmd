@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -c "import sys; assert (3, 10) <= sys.version_info < (3, 13)" >nul 2>nul
  if not errorlevel 1 goto install_dependencies
)
set "BOOT_PYTHON=python"
if exist "F:\ProgramData\anaconda3\python.exe" (
  set "BOOT_PYTHON=F:\ProgramData\anaconda3\python.exe"
)
"%BOOT_PYTHON%" -c "import sys; assert (3, 10) <= sys.version_info < (3, 13)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.10, 3.11 or 3.12 with Tkinter is required.
  if /I not "%~1"=="--no-pause" pause
  exit /b 1
)
"%BOOT_PYTHON%" -m venv --system-site-packages "%~dp0.venv"
if errorlevel 1 (
  echo Failed to create the project environment. Install Python 3.10 to 3.12.
  if /I not "%~1"=="--no-pause" pause
  exit /b 1
)

:install_dependencies
"%~dp0.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo Dependency installation failed. Check the output above and retry.
  if /I not "%~1"=="--no-pause" pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -c "import cv2, numpy, PIL, tkinter, rapidocr_onnxruntime"
if errorlevel 1 (
  echo The environment check failed. The application was not started.
  if /I not "%~1"=="--no-pause" pause
  exit /b 1
)
echo Installation completed. You can now run the launcher.
if /I not "%~1"=="--no-pause" pause
exit /b 0
