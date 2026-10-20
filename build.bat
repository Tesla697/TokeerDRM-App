@echo off
REM ===========================================================================
REM  Build TokeerDRM.exe (single-file, no console, DPI-aware) via the spec.
REM  Run from this folder after:  pip install -r requirements.txt pyinstaller
REM  The spec carries the DPI-aware manifest + version resource + Qt excludes.
REM
REM  Code signing (the real fix for AV false positives) kicks in automatically
REM  once you set these before running, otherwise the build just skips it:
REM     set TOKEERDRM_CERT=C:\path\to\cert.pfx
REM     set TOKEERDRM_CERT_PASS=yourpassword
REM  Optional (defaults to DigiCert's RFC-3161 timestamp server):
REM     set TOKEERDRM_TS=http://timestamp.digicert.com
REM ===========================================================================

python -m PyInstaller --noconfirm --clean TokeerDRM.spec
if errorlevel 1 goto end

if defined TOKEERDRM_CERT (
    if "%TOKEERDRM_TS%"=="" set "TOKEERDRM_TS=http://timestamp.digicert.com"
    echo.
    echo Signing dist\TokeerDRM.exe ...
    signtool sign /fd SHA256 /tr "%TOKEERDRM_TS%" /td SHA256 /f "%TOKEERDRM_CERT%" /p "%TOKEERDRM_CERT_PASS%" "dist\TokeerDRM.exe"
    if errorlevel 1 (
        echo.
        echo [!] Signing FAILED. The unsigned exe is still at dist\TokeerDRM.exe.
        echo     Check the cert path/password and that signtool is on PATH
        echo     ^(Windows SDK, e.g. C:\Program Files ^(x86^)\Windows Kits\10\bin\...\x64^).
    ) else (
        echo [+] Signed. Verifying...
        signtool verify /pa "dist\TokeerDRM.exe"
    )
) else (
    echo.
    echo [i] No TOKEERDRM_CERT set, skipping signing. Unsigned builds get more
    echo     AV false positives; set TOKEERDRM_CERT + TOKEERDRM_CERT_PASS to sign.
)

:end
echo.
echo Done. Your app is at:  dist\TokeerDRM.exe
pause
