[CmdletBinding()]
param(
    [string]$ProxyUrl,
    [string]$MusePython = $env:MUSE_PYTHON,
    [string]$BrowserDirectory = $env:MUSE_BROWSER_BIN,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
if (-not $MusePython) { $MusePython = 'python' }
$musePythonCommand = Get-Command -Name $MusePython -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $musePythonCommand) {
    throw 'Python is missing. Set -MusePython or MUSE_PYTHON to a Python installation with muse_mcp installed.'
}
if ($BrowserDirectory) {
    if (-not (Test-Path -LiteralPath $BrowserDirectory -PathType Container)) {
        throw 'BrowserDirectory must be an existing directory containing native agent-browser.exe.'
    }
    $museBrowserSpec = Join-Path $BrowserDirectory 'agent-browser.exe'
} else {
    $museBrowserSpec = 'agent-browser.exe'
}
$museBrowserCommand = Get-Command -Name $museBrowserSpec -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $museBrowserCommand) {
    throw 'Native agent-browser.exe is missing. A .cmd npm shim cannot be launched by Muse. Put agent-browser.exe on PATH or set -BrowserDirectory / MUSE_BROWSER_BIN to its directory.'
}
if ($CheckOnly) {
    @{ python = $musePythonCommand.Source; browser = $museBrowserCommand.Source } | ConvertTo-Json -Compress
    return
}
$museBrowserBinDir = Split-Path -Parent $museBrowserCommand.Source
$env:PATH = "$museBrowserBinDir$([IO.Path]::PathSeparator)$env:PATH"
if ($ProxyUrl) {
    $env:HTTP_PROXY = $ProxyUrl
    $env:HTTPS_PROXY = $ProxyUrl
    $env:ALL_PROXY = $ProxyUrl
}
$env:AGENT_BROWSER_SESSION = 'muse-auth-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
Write-Host 'Connecting to the logged-in Muse Chrome session (maximum 60 seconds)...'
Write-Host "Using isolated browser session: $env:AGENT_BROWSER_SESSION"
$museAuthProcess = Start-Process -FilePath $musePythonCommand.Source -ArgumentList @('-u', '-m', 'muse_mcp.cli', 'auth', 'export') -WindowStyle Hidden -PassThru -RedirectStandardOutput "$PSScriptRoot\muse-auth-output.log" -RedirectStandardError "$PSScriptRoot\muse-auth-error.log"
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
& $musePythonCommand.Source "$PSScriptRoot\verify_muse.py" --python $musePythonCommand.Source
if ($LASTEXITCODE -ne 0) { throw 'Muse status verification failed.' }
