@echo off
rem Starts the AAC Map Overlay. The first run sets up Python packages (about a minute).
rem The Python environment lives in a short path because .NET cannot load DLLs from very long ones.
set "VENV=%LOCALAPPDATA%\AACMapOverlay\venv"
if not exist "%VENV%\Scripts\pythonw.exe" (
  echo Setting up the overlay for the first time...
  python -m venv "%VENV%" || (echo Python 3 is required: https://www.python.org/downloads/ & pause & exit /b 1)
  "%VENV%\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r "%~dp0requirements.txt" || (pause & exit /b 1)
)
rem ad blocking (Brave's engine); added later, so install it for older setups too
"%VENV%\Scripts\python.exe" -c "import adblock" 2>nul || "%VENV%\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check adblock
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0overlay.py"
