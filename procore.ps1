# Convenience wrapper: `.\procore.ps1 companies --table`
# Uses the project venv so you never have to activate it.
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $py -m procli.cli @args
