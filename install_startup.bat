@echo off
REM Adds the GEX page to Windows startup so it launches (minimized) every time you sign in.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'GEX Levels.lnk';" ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk);" ^
  "$s.TargetPath = '%~dp0start_gex.bat'; $s.WorkingDirectory = '%~dp0';" ^
  "$s.WindowStyle = 7; $s.Description = 'Start the GEX levels page'; $s.Save();" ^
  "Write-Host ('Created: ' + $lnk)"
if errorlevel 1 (
    echo.
    echo Couldn't create the startup shortcut.
) else (
    echo.
    echo Done. The GEX page will start automatically when you sign in to Windows.
    echo To undo, double-click remove_startup.bat.
)
pause
