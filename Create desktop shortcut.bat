@echo off
rem Double-click once to put a "Ctrl+Forgot" icon on your desktop.
setlocal
set "HERE=%~dp0"
set "TARGET="
for /f "delims=" %%i in ('where pyw 2^>nul') do if not defined TARGET set "TARGET=%%i"
if not defined TARGET for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined TARGET set "TARGET=%%i"
set "ARGS="%HERE%Ctrl+Forgot.pyw""
if not defined TARGET set "ARGS="
if not defined TARGET set "TARGET=%HERE%Ctrl+Forgot.pyw"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Ctrl+Forgot.lnk'); $s.TargetPath = $env:TARGET; $s.Arguments = $env:ARGS; $s.WorkingDirectory = $env:HERE; $s.IconLocation = $env:HERE + 'web\icon.ico'; $s.Description = 'Ctrl+Forgot: for everything you forgot you wrote down'; $s.Save()"
if errorlevel 1 goto failed
echo.
echo Done. Double-click "Ctrl+Forgot" on your desktop to start it.
echo.
pause
exit /b 0
:failed
echo.
echo Could not create the shortcut. You can still double-click "Ctrl+Forgot.pyw" in this folder.
echo.
pause
exit /b 1
