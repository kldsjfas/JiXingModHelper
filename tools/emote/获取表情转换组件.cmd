@echo off
setlocal
echo This downloads pinned FFmpeg and FFprobe from the BtbN GitHub release.
echo The archive and every runtime file are checked against the bundled SHA256 manifest.
echo No system PATH, driver or service is changed.
echo.
set "componentScript=%~dp0tools\emote\fetch_ffmpeg.ps1"
if exist "%~dp0fetch_ffmpeg.ps1" set "componentScript=%~dp0fetch_ffmpeg.ps1"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%componentScript%"
set "setupExit=%ERRORLEVEL%"
echo.
if not "%setupExit%"=="0" (
  echo Component setup failed. Exit code: %setupExit%
  echo Check the error above and your network connection, then run this file again.
) else (
  echo Component setup completed. Return to the app and retry preview or import.
)
echo Press any key to close this window.
pause >nul
exit /b %setupExit%
