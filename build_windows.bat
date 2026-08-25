@echo off
REM Build volkit.exe on Windows -- double-click, or run from a terminal.
REM
REM This is a thin wrapper.  All of the work, and every check, lives in
REM build_exe.py so the local build, the GitHub Actions build and any manual
REM run do exactly the same thing.  Pass any of its flags through, e.g.
REM
REM     build_windows.bat --onefile --zip
REM
REM Requires Python 3.10 or later on PATH.  PyInstaller cannot cross-compile,
REM which is why this must run on Windows at all.

setlocal
python --version >nul 2>&1 || (echo Python was not found on PATH & pause & exit /b 1)

python "%~dp0build_exe.py" %*
set RC=%ERRORLEVEL%

REM Double-clicked, the window closes the instant the build ends and takes the
REM error with it.  Hold it open.
if "%CMDCMDLINE:~0,4%"=="cmd " pause
exit /b %RC%
