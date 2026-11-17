@echo off
REM assisArm - web remote with direction buttons
REM Finds the board on the current network, then serves the button page.
cd /d "%~dp0"
python arm_launch.py --web %*
if errorlevel 1 pause
