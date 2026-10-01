@echo off
setlocal
cd /d "%~dp0"
if exist "F:\ProgramData\anaconda3\python.exe" (
  "F:\ProgramData\anaconda3\python.exe" -m venv --system-site-packages "%~dp0.venv"
) else (
  python -m venv --system-site-packages "%~dp0.venv"
)
if errorlevel 1 (
  echo Failed to create the project environment. Install Python 3.10 or newer.
  if /I not "%~1"=="--no-pause" pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo Dependency installation failed. Check the output above and retry.
  if /I not "%~1"=="--no-pause" pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -c "import cv2, numpy, PIL, tkinter"
if errorlevel 1 (
  echo The environment check failed. The application was not started.
  if /I not "%~1"=="--no-pause" pause
  exit /b 1
)
echo Installation completed. You can now run the launcher.
if /I not "%~1"=="--no-pause" pause
exit /b 0
