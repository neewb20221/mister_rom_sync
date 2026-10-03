@echo off
setlocal
cd /d "%~dp0"

REM Create a GitHub Release from the current tree.
REM Usage: release.bat [version]
REM Example: release.bat 0.1.0
REM
REM Before running:
REM   1) Set APP_VERSION in mister_rom_sync.py to the same version
REM   2) Update CHANGELOG.md
REM   3) Commit and push to origin

set "VER=%~1"
if "%VER%"=="" set "VER=0.1.0"

echo Version: %VER%
echo.

call build_exe.bat
if errorlevel 1 exit /b 1

if not exist "MiSTerRomSync.exe" (
  echo ERROR: MiSTerRomSync.exe not found after build.
  exit /b 1
)

echo.
echo Creating GitHub release v%VER% ...
gh release create "v%VER%" "MiSTerRomSync.exe" ^
  --title "MiSTer ROM Sync %VER%" ^
  --notes-file CHANGELOG.md ^
  --latest

if errorlevel 1 (
  echo.
  echo Release create failed. If the tag already exists, delete it or bump the version.
  exit /b 1
)

echo.
echo OK: https://github.com/neewb20221/mister_rom_sync/releases/tag/v%VER%
exit /b 0
