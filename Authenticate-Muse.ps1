param([string]$ProxyUrl)

$ErrorActionPreference = 'Stop'
$museBrowserBinary = "$PSScriptRoot\.tools\node_modules\agent-browser\bin\agent-browser-win32-x64.exe"
$museBrowserBinDir = "$PSScriptRoot\.tools\bin"
if (-not (Test-Path -LiteralPath $museBrowserBinary)) {
    throw 'agent-browser Windows executable is missing.'
}
New-Item -ItemType Directory -Path $museBrowserBinDir -Force | Out-Null
$museBrowserTarget = "$museBrowserBinDir\agent-browser.exe"
if (-not (Test-Path -LiteralPath $museBrowserTarget)) {
    Copy-Item -LiteralPath $museBrowserBinary -Destination $museBrowserTarget
}
$env:PATH = "$museBrowserBinDir;$env:PATH"
$env:AGENT_BROWSER_SESSION = 'muse-auth-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
Write-Host 'Connecting to the logged-in Muse Chrome session (maximum 60 seconds)...'
Write-Host "Using isolated browser session: $env:AGENT_BROWSER_SESSION"
$museAuthProcess = Start-Process -FilePath "$PSScriptRoot\muse-mcp\.venv\Scripts\python.exe" -ArgumentList @('-u', '-m', 'muse_mcp.cli', 'auth', 'export') -WindowStyle Hidden -PassThru -RedirectStandardOutput "$PSScriptRoot\muse-auth-output.log" -RedirectStandardError "$PSScriptRoot\muse-auth-error.log"
$museAuthElapsed = 0
while (-not $museAuthProcess.WaitForExit(5000)) {
    $museAuthElapsed += 5
    Write-Host "Waiting for browser connection: $museAuthElapsed seconds..."
    if ($museAuthElapsed -ge 60) {
        & taskkill.exe /PID $museAuthProcess.Id /T /F | Out-Null
        throw 'Browser authentication timed out after 60 seconds. No successful Muse connection has been verified.'
    }
}
$museAuthProcess.WaitForExit()
Get-Content -LiteralPath "$PSScriptRoot\muse-auth-output.log"
Get-Content -LiteralPath "$PSScriptRoot\muse-auth-error.log"
if ($museAuthProcess.ExitCode -ne 0) { throw "Muse authentication failed (exit $($museAuthProcess.ExitCode))." }
$museCookiePath = Join-Path $env:USERPROFILE '.config\muse-cli\cookies.txt'
if (Test-Path -LiteralPath $museCookiePath) {
    $museIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $museCookiePath /inheritance:r /grant:r "${museIdentity}:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restrict cookie-file access.' }
}
if ($ProxyUrl) {
    $env:HTTP_PROXY = $ProxyUrl
    $env:HTTPS_PROXY = $ProxyUrl
    $env:ALL_PROXY = $ProxyUrl
}
& "$PSScriptRoot\muse-mcp\.venv\Scripts\python.exe" "$PSScriptRoot\verify_muse.py"
if ($LASTEXITCODE -ne 0) { throw 'Muse status verification failed.' }
