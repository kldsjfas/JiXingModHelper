@echo off
setlocal
echo This downloads the optional audio decoder from the official vgmstream release.
echo The archive and extracted files are checked against the bundled SHA256 manifest.
echo.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\audio\fetch_vgmstream.ps1" -Destination "%~dp0tools\audio\vgmstream"
set "setupExit=%ERRORLEVEL%"
echo.
if not "%setupExit%"=="0" (
  echo Audio decoder setup failed. Exit code: %setupExit%
  echo Check the error above and your network connection, then run this file again.
) else (
  echo Audio decoder setup completed. Return to the app and retry the audio preview.
)
echo Press any key to close this window.
pause >nul
exit /b %setupExit%
