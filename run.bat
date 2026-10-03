@echo off
setlocal
cd /d "%~dp0"

REM MiSTer ROM Sync — launch from source (latest script).
REM Taskbar icon: AppUserModelID + WM_SETICON in mister_rom_sync.py.
REM Native icon: build/run MiSTerRomSync.exe via build_exe.bat.

where pyw >nul 2>&1
if not errorlevel 1 (
    start "" pyw -3 mister_rom_sync.py
    exit /b 0
)

where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw mister_rom_sync.py
    exit /b 0
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 mister_rom_sync.py
    goto :done
)

where python >nul 2>&1
if not errorlevel 1 (
    python mister_rom_sync.py
    goto :done
)

echo Python not found. Install Python 3 and ensure py/python is on PATH.
pause
exit /b 1

:done
if errorlevel 1 (
    echo.
    echo Exit code: %ERRORLEVEL%
    pause
)
exit /b %ERRORLEVEL%
