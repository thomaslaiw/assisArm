@echo off
REM assisArm - eye tracking with arm control
REM Finds the board on the current network, then runs the eye tracker.
cd /d "%~dp0"
python arm_launch.py %*
if errorlevel 1 pause
