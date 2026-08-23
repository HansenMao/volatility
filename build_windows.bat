@echo off
REM Build volkit.exe on Windows.
REM
REM PyInstaller cannot cross-compile, so this must run on a Windows machine
REM (or use the GitHub Actions workflow, which does it on a hosted runner).
REM
REM Requires Python 3.10 or later on PATH.

setlocal
echo === volkit Windows build ===

python --version || (echo Python not found on PATH & exit /b 1)

echo.
echo [1/4] installing build and runtime dependencies
python -m pip install --upgrade pip                                    || exit /b 1
python -m pip install -r requirements.txt                              || exit /b 1
REM tzdata is REQUIRED on Windows: there is no system IANA database, and cut
REM times, the weekly market close and the economic calendar all need one.
python -m pip install tzdata pyinstaller                               || exit /b 1

echo.
echo [2/4] running the test suite
python -m unittest discover -s tests                                   || exit /b 1

echo.
echo [3/4] building the executable
python -m PyInstaller volkit.spec --noconfirm --clean                  || exit /b 1

echo.
echo [4/4] staging the user's data files beside the exe
if not exist dist\volkit\files mkdir dist\volkit\files
copy /Y files\vol_marks.xlsx        dist\volkit\        >nul 2>&1
copy /Y files\market_feed.csv       dist\volkit\        >nul 2>&1
copy /Y files\bands.csv             dist\volkit\        >nul 2>&1
copy /Y files\holiday_overrides.csv dist\volkit\        >nul 2>&1
copy /Y USER_MANUAL.md              dist\volkit\        >nul 2>&1

echo.
echo Done.  dist\volkit\volkit.exe
echo Double-click it, or run:  volkit.exe --help
endlocal
