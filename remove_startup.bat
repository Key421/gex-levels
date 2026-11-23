@echo off
REM Removes the GEX page from Windows startup.
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GEX Levels.lnk"
if exist "%LNK%" (
    del "%LNK%"
    echo Removed. The GEX page will no longer start when you sign in.
) else (
    echo The GEX page wasn't set to start automatically - nothing to remove.
)
pause
