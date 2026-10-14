<#
.SYNOPSIS
  The V0.6 browser demo on Windows: demo site, a visible Chrome, the decision viewer; everything is stopped at the end.

.DESCRIPTION
  -Mode run    one attempt of the verified order-note task (reflexmesh browser run), watched in the viewer.
  -Mode chain  the handoff chain through the session service (reflexmesh browser serve + experiments\v06\chain_demo.py).

  -Provider jev needs an OpenRouter key. It is taken from OPENROUTER_API_KEY (or TYPESAFE_API_KEY) if set, otherwise
  asked for here and kept in this process only. Before the run one request checks the key and the route:
  401 = key rejected or expired, 403 = the exit country is blocked (turn on the VPN, or pass -Proxy).

  Run it from your own Windows session (not over SSH) to see the browser window.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File experiments\v06\windows\run-demo.ps1
  powershell -ExecutionPolicy Bypass -File experiments\v06\windows\run-demo.ps1 -Mode chain
  powershell -ExecutionPolicy Bypass -File experiments\v06\windows\run-demo.ps1 -Provider script -Headless
#>
param(
    [ValidateSet('run', 'chain')] [string] $Mode = 'run',
    [ValidateSet('jev', 'script')] [string] $Provider = 'jev',
    [switch] $Headless,
    [string] $Proxy,
    [string] $OutRoot,
    [switch] $NoPause
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path (Join-Path $PSScriptRoot '..') '..\..')).Path
$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "No virtual environment at $py (see docs\guides\V0.6-browser-run.md, Windows)" }
if (-not $env:REFLEXMESH_CHROME) { $env:REFLEXMESH_CHROME = 'C:\Program Files\Google\Chrome\Application\chrome.exe' }
if (-not (Test-Path $env:REFLEXMESH_CHROME)) { throw "Chrome not found: $env:REFLEXMESH_CHROME (set REFLEXMESH_CHROME)" }
if ($Proxy) { $env:HTTPS_PROXY = $Proxy; $env:HTTP_PROXY = $Proxy }
if (-not $OutRoot) { $OutRoot = Join-Path $repo 'runs' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location $repo

function Invoke-Py([string[]] $arguments) {
    # Native stderr must not become a terminating error (Windows PowerShell 5.1 with ErrorActionPreference Stop).
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $py @arguments 2>$null } finally { $ErrorActionPreference = $saved }
}

function Test-Port([int] $port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { $client.Connect('127.0.0.1', $port); return $true } catch { return $false } finally { $client.Close() }
}

if ($Provider -eq 'jev') {
    if (-not $env:OPENROUTER_API_KEY -and -not $env:TYPESAFE_API_KEY) {
        $secure = Read-Host -AsSecureString 'OpenRouter API key (kept in this process only)'
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $env:OPENROUTER_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
    if ($env:OPENROUTER_API_KEY) {
        # The same HTTP stack as the provider (httpx: HTTPS_PROXY, else the Windows proxy settings).
        # Single quotes only: Windows PowerShell 5.1 drops double quotes inside native arguments.
        $probe = "import os,httpx; r=httpx.get('https://openrouter.ai/api/v1/key', headers={'Authorization': 'Bearer '+os.environ['OPENROUTER_API_KEY']}, timeout=20); print(r.status_code)"
        $status = Invoke-Py @('-c', $probe)
        Write-Host "OpenRouter key check: HTTP $status"
        if ($status -ne '200') {
            if ($status -eq '403') { throw 'HTTP 403: the exit country is blocked. Turn on the VPN (Clash) or pass -Proxy http://127.0.0.1:PORT' }
            if ($status -eq '401') { throw 'HTTP 401: the key is rejected or expired. Clear OPENROUTER_API_KEY and run again to enter a new one.' }
            throw "The key check did not succeed (HTTP '$status'): no route to openrouter.ai?"
        }
    }
}

if ($Mode -eq 'chain' -and $Provider -eq 'script') { throw 'Mode chain needs -Provider jev (the scripted chain is covered by the tests)' }
if (Test-Port 8770) { throw 'Port 8770 is already in use: is a demo site from an earlier run still alive?' }
$started = @()
try {
    $site = Start-Process -PassThru -WindowStyle Hidden -FilePath $py -ArgumentList 'experiments\v06\site\server.py', '--port', '8770'
    $started += $site
    for ($i = 0; $i -lt 50 -and -not (Test-Port 8770); $i++) { Start-Sleep -Milliseconds 200 }
    if (-not (Test-Port 8770)) { throw 'The demo site did not start' }
    Write-Host 'Demo site: http://127.0.0.1:8770/orders'
    $common = @('--action-provider', $Provider, '--checks', 'experiments\v06\checks\order-desk.json')
    if (-not $Headless) { $common += '--headed' }

    if ($Mode -eq 'run') {
        $out = Join-Path $OutRoot "run-$stamp"
        New-Item -ItemType Directory -Force $out | Out-Null
        if (Test-Port 8780) { throw 'Port 8780 (viewer) is already in use' }
        $viewer = Start-Process -PassThru -WindowStyle Hidden -FilePath $py -ArgumentList '-m', 'reflexmesh', 'browser', 'view', '--run-dir', "`"$out`"", '--port', '8780'
        $started += $viewer
        for ($i = 0; $i -lt 50 -and -not (Test-Port 8780); $i++) { Start-Sleep -Milliseconds 200 }
        Write-Host 'Viewer: http://127.0.0.1:8780/  (opening it)'
        try { Start-Process 'http://127.0.0.1:8780/' } catch { Write-Host '(could not open a browser here; open the link yourself)' }
        $args_ = @('-m', 'reflexmesh', 'browser', 'run', '--input', 'experiments\v06\tasks\order-note-verified.json',
                   '--output-dir', $out) + $common
        if ($Provider -eq 'script') { $args_ += @('--script', 'experiments\v06\tasks\order-note.script.json') }
        $runner = Start-Process -PassThru -Wait -NoNewWindow -FilePath $py -ArgumentList ($args_ | ForEach-Object { if ($_ -match ' ') { "`"$_`"" } else { $_ } }) `
            -RedirectStandardOutput (Join-Path $OutRoot "run-$stamp.result.json") -RedirectStandardError (Join-Path $OutRoot "run-$stamp.stderr.log")
        $code = $runner.ExitCode
        $json = Get-Content -Raw (Join-Path $OutRoot "run-$stamp.result.json")
        if (-not $json) { throw "The run printed no result (exit $code); see $OutRoot\run-$stamp.stderr.log" }
        $result = $json | ConvertFrom-Json
        Write-Host ''
        Write-Host ("Result: {0}/{1} (exit {2})  steps {3}, model calls {4}" -f $result.attempt_status, $result.stop_reason, $code,
                    $result.budget.steps, $result.budget.model_calls)
        foreach ($row in $result.verification) { Write-Host ("  {0}: {1}" -f $row.id, $row.status) }
        Write-Host "Files: $out  (Browser Use log: $OutRoot\run-$stamp.stderr.log)"
        if (-not $NoPause) { Read-Host 'Press Enter to stop the viewer and the demo site' | Out-Null }
    } else {
        $state = Join-Path $OutRoot "chain-$stamp"
        New-Item -ItemType Directory -Force $state | Out-Null
        $sock = Join-Path $state 'svc.addr'
        $serve = @('-m', 'reflexmesh', 'browser', 'serve', '--socket', "`"$sock`"", '--state-dir', "`"$state`"") + $common
        $service = Start-Process -PassThru -WindowStyle Hidden -FilePath $py -ArgumentList $serve -RedirectStandardError (Join-Path $state 'serve.err')
        $started += $service
        for ($i = 0; $i -lt 50 -and -not (Test-Path $sock); $i++) { Start-Sleep -Milliseconds 200 }
        if (-not (Test-Path $sock)) { throw 'The session service did not start (see serve.err)' }
        $saved = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & $py experiments\v06\chain_demo.py --socket $sock } finally { $ErrorActionPreference = $saved }
        Invoke-Py @('-m', 'reflexmesh', 'browser', 'shutdown', '--socket', $sock) | Out-Null
        for ($i = 0; $i -lt 100 -and -not $service.HasExited; $i++) { Start-Sleep -Milliseconds 200 }
        Write-Host "Files: $state (chain-*\attempt-*\result.json, trace.jsonl)"
        Write-Host "To look at one attempt: $py -m reflexmesh browser view --run-dir <attempt dir>"
    }
} finally {
    # The venv python.exe is a launcher whose child is the real interpreter: stop the whole tree, not the launcher.
    $ErrorActionPreference = 'Continue'
    foreach ($p in $started) { & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null }
    $left = @(8770, 8780 | Where-Object { Test-Port $_ })
    if ($left) { Write-Host "Still listening: $left" } else { Write-Host 'Stopped: demo site, viewer, service.' }
}
