@echo off
REM assisArm - just report where the board is
cd /d "%~dp0"
python arm_launch.py --find
pause
