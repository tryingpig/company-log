' hidden runner for daily_run.bat --light (scheduler: CompanyLogLight)
CreateObject("WScript.Shell").Run "cmd /c ""C:\ClaudeCode\company-log\tools\daily_run.bat"" --light", 0, False
