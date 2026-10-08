@echo off
setlocal

set "SCRIPT=%~dp0picasso_price_paster.ahk"
set "OUT=%~dp0picasso_price_paster.exe"

if exist "%~dp0AutoHotkey\Compiler\Ahk2Exe.exe" (
  set "AHK2EXE=%~dp0AutoHotkey\Compiler\Ahk2Exe.exe"
) else if exist "%ProgramFiles%\AutoHotkey\Compiler\Ahk2Exe.exe" (
  set "AHK2EXE=%ProgramFiles%\AutoHotkey\Compiler\Ahk2Exe.exe"
) else if exist "%ProgramFiles(x86)%\AutoHotkey\Compiler\Ahk2Exe.exe" (
  set "AHK2EXE=%ProgramFiles(x86)%\AutoHotkey\Compiler\Ahk2Exe.exe"
) else (
  echo Could not find Ahk2Exe.exe.
  echo.
  echo Option A:
  echo   Install AutoHotkey v1.1, then run this file again.
  echo.
  echo Option B:
  echo   Put a portable AutoHotkey folder here:
  echo   %~dp0AutoHotkey\Compiler\Ahk2Exe.exe
  echo.
  pause
  exit /b 1
)

echo Building:
echo   %OUT%
echo.
"%AHK2EXE%" /in "%SCRIPT%" /out "%OUT%"

if errorlevel 1 (
  echo.
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Done.
echo You can now run:
echo   %OUT%
pause
