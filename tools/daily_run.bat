@echo off
rem company-log daily update (scheduler: CompanyLogDaily, weekdays 10:00, 20:00)
cd /d C:\ClaudeCode\company-log
set PYTHONUTF8=1
if not exist logs mkdir logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set D=%%i
"C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe" tools\daily.py >> logs\daily_%D%.log 2>&1
