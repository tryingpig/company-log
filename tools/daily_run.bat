@echo off
rem company-log daily update (CompanyLogDaily weekdays 10:00, 15:40, 20:05 / CompanyLogLight 08:30, 11:30, 14:00 with --light)
cd /d C:\ClaudeCode\company-log
set PYTHONUTF8=1
if not exist logs mkdir logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set D=%%i
"C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe" tools\daily.py %* >> logs\daily_%D%.log 2>&1
