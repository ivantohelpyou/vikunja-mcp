# restart-claude.ps1
# Restarts Claude Desktop to reload MCP server configuration
#
# Usage: Right-click and "Run with PowerShell" or run from terminal:
#   .\restart-claude.ps1

Write-Host "Stopping Claude Desktop..." -ForegroundColor Yellow

# Find and stop all Claude processes
$claudeProcesses = Get-Process -Name "Claude*" -ErrorAction SilentlyContinue
if ($claudeProcesses) {
    $claudeProcesses | Stop-Process -Force
    Write-Host "  Stopped $($claudeProcesses.Count) Claude process(es)" -ForegroundColor Gray
    # Wait for processes to fully exit
    Start-Sleep -Seconds 2
} else {
    Write-Host "  No Claude processes found" -ForegroundColor Gray
}

Write-Host "Starting Claude Desktop..." -ForegroundColor Yellow

# Find Claude Desktop executable
$claudePaths = @(
    "$env:LOCALAPPDATA\Programs\Claude\Claude.exe",
    "$env:LOCALAPPDATA\Claude\Claude.exe",
    "$env:ProgramFiles\Claude\Claude.exe"
)

$claudeExe = $null
foreach ($path in $claudePaths) {
    if (Test-Path $path) {
        $claudeExe = $path
        break
    }
}

if ($claudeExe) {
    Start-Process $claudeExe
    Write-Host "  Started Claude Desktop" -ForegroundColor Green
    Write-Host ""
    Write-Host "Claude Desktop restarted! MCP servers will reload." -ForegroundColor Cyan
} else {
    Write-Host "  Could not find Claude Desktop installation" -ForegroundColor Red
    Write-Host "  Please start Claude Desktop manually" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Searched locations:" -ForegroundColor Gray
    foreach ($path in $claudePaths) {
        Write-Host "  - $path" -ForegroundColor Gray
    }
}
