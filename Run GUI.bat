@echo off
rem Launcher for the Omarchy ISO Boot Installer GUI.
rem The app starts read-only and offers a "Relaunch as Administrator" button.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "omarchy_iso_boot.py"
) else (
    python "omarchy_iso_boot.py"
)
