@echo off
rem deploy/windows/tunnel.cmd
rem
rem Double-click to start the SSH tunnel, then open http://127.0.0.1:8080/console/
rem Usage from a terminal:
rem   tunnel.cmd            start
rem   tunnel.cmd stop       stop
rem   tunnel.cmd status     show state
rem
rem Everything lives in tunnel.ps1; this wrapper only sets the execution policy
rem (unsigned local scripts are blocked by the default RemoteSigned policy).

setlocal
set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tunnel.ps1" -Action %ACTION%

if "%~1"=="" (
    echo.
    pause
)
endlocal
