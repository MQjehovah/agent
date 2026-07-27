@echo off
pushd %~dp0..
echo === AI Agent Builder ===
echo.
echo [1/4] Installing pyinstaller...
pip install pyinstaller
if errorlevel 1 (echo FAILED: pip install & pause & exit /b 1)
echo.
echo [2/4] Cleaning old builds...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
echo.
echo [3/4] Building main executable...
pyinstaller --clean build.spec
if errorlevel 1 (echo FAILED: pyinstaller & pause & exit /b 1)
echo.
echo [4/4] Done!
echo.
echo Output:
echo   dist\ai-agent.exe
echo.
echo Usage:
echo   dist\ai-agent.exe -w . -a "AI Agent Name"
echo.
pause