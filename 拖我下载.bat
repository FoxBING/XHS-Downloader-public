@echo off
rem Drag one or more txt files onto this icon to download their links.
rem Double-click with nothing dropped = default queue 1.txt
cd /d "%~dp0"
if "%~1"=="" (
    echo No txt dropped, using default 1.txt
    uv run dl.py 1.txt
) else (
    for %%f in (%*) do (
        echo ================ %%f ================
        uv run dl.py "%%~f"
    )
)
pause
