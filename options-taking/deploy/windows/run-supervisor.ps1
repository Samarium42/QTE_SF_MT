# Keep the supervisor running on an always-on Windows PC, as systemd does on Linux: restart
# it if it dies, mass-cancelling first. Task Scheduler runs this at start-up (deploy/README.md).
#
#     powershell -ExecutionPolicy Bypass -File deploy\windows\run-supervisor.ps1 [--dry-run]
#
# Settings come from deploy\ops.env (copy deploy\ops.env.example; git ignores it).
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"

$envFile = Join-Path $root "deploy\ops.env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim(), "Process")
        }
    }
}

while ($true) {
    & $python -m ops.supervisor @args
    $code = $LASTEXITCODE
    if ($code -eq 0) { exit 0 }  # stopped on purpose
    if ($code -eq 2) { Write-Error "set-up problem (exit 2): see logs\supervisor.log"; exit 2 }
    Write-Warning "supervisor exited with code $($code): mass-cancelling, restarting in 10 s"
    & $python -m ops.emergency
    Start-Sleep -Seconds 10
}
