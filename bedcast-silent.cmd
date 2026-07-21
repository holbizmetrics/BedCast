@echo off
rem BedCast silent-PC mode — double-click and go to bed.
rem Routes Windows default output to the VB-Audio Cable (room = silent),
rem serves the audio on :48100, restores your speakers when you close this
rem window or press Ctrl+C.
rem If this window is killed hard and audio seems "broken": Windows Sound
rem settings -> Output -> pick your speakers again. Nothing is damaged.

set EXE=%~dp0src\BedCast.Server\bin\Release\net10.0-windows\bedcast-server.exe
if not exist "%EXE%" set EXE=%~dp0bedcast-server.exe
if not exist "%EXE%" (
  echo bedcast-server.exe not found - build with:
  echo   dotnet build src/BedCast.Server -c Release
  pause
  exit /b 1
)
"%EXE%" --silent
pause
