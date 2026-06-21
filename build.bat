@echo off
REM ===========================================================================
REM  Build TokeerDRM.exe (single-file, no console, DPI-aware) via the spec.
REM  Run from this folder after:  pip install -r requirements.txt pyinstaller
REM  The spec carries the DPI-aware manifest + Qt excludes + bundled assets.
REM ===========================================================================

python -m PyInstaller --noconfirm --clean TokeerDRM.spec

echo.
echo Done. Your app is at:  dist\TokeerDRM.exe
pause
