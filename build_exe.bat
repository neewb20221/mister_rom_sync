@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
    echo Python not found.
    exit /b 1
)

echo [1/2] Installing PyInstaller...
py -m pip install -q pyinstaller

echo [2/2] Building standalone one-file EXE (stdlib + tkinter only)...
py -m PyInstaller ^
  --noconfirm --clean ^
  --windowed --onefile ^
  --noupx ^
  --name "MiSTerRomSync" ^
  --icon "assets\mister.ico" ^
  --add-data "assets\mister.ico;assets" ^
  --add-data "assets\mister_32.png;assets" ^
  --add-data "assets\mister_48.png;assets" ^
  --add-data "assets\mister_favicon.png;assets" ^
  --paths "." ^
  --hidden-import dat_engine ^
  --hidden-import dat_download ^
  --hidden-import dat_prefs ^
  --hidden-import dat_manager_ui ^
  --hidden-import mister_platform_map ^
  --hidden-import rom_heuristics ^
  --hidden-import app_paths ^
  --hidden-import crc_cache ^
  mister_rom_sync.py

if errorlevel 1 (
    echo BUILD FAILED
    exit /b 1
)

copy /Y "dist\MiSTerRomSync.exe" "MiSTerRomSync.exe" >nul
echo.
echo OK: MiSTerRomSync.exe
echo Fully standalone — no Python/vendor/PyYAML needed at runtime.
echo Next to the exe it will create: dats\  logs\  config_mister_rom_sync.json
exit /b 0
