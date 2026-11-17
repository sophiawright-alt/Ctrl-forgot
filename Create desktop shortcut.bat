@echo off
rem Double-click once to put a "Second Brain" icon on your desktop.
setlocal
set "HERE=%~dp0"
set "TARGET="
for /f "delims=" %%i in ('where pyw 2^>nul') do if not defined TARGET set "TARGET=%%i"
if not defined TARGET for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined TARGET set "TARGET=%%i"
set "ARGS="%HERE%Second Brain.pyw""
if not defined TARGET set "ARGS="
if not defined TARGET set "TARGET=%HERE%Second Brain.pyw"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Second Brain.lnk'); $s.TargetPath = $env:TARGET; $s.Arguments = $env:ARGS; $s.WorkingDirectory = $env:HERE; $s.IconLocation = $env:HERE + 'web\icon.ico'; $s.Description = 'Second Brain: your notes, kept on this computer'; $s.Save()"
if errorlevel 1 goto failed
echo.
echo Done. Double-click "Second Brain" on your desktop to start it.
echo.
pause
exit /b 0
:failed
echo.
echo Could not create the shortcut. You can still double-click "Second Brain.pyw" in this folder.
echo.
pause
exit /b 1
