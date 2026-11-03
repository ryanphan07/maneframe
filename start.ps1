<#
One-command local run: checks the setup, starts llama-server if nothing answers, starts the backend on :8000 and
opens the app. Ctrl+C stops only what this script started. Settings come from backend\.env (see .env.example).
#>
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$python = Join-Path $backend 'venv\Scripts\python.exe'
$logDir = Join-Path $root 'logs'
$port = 8000

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# Value of KEY in backend\.env, else $default. A real environment variable wins, as it does for the backend.
function Get-Setting($key, $default = $null) {
    $fromEnv = [Environment]::GetEnvironmentVariable($key)
    if ($fromEnv) { return $fromEnv }
    $envFile = Join-Path $backend '.env'
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match "^\s*$key\s*=\s*(.*?)\s*$") { return $Matches[1].Trim('"', "'") }
        }
    }
    return $default
}

function Test-Url($url) {
    try { Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3 | Out-Null; return $true } catch { return $false }
}

# --- checks ---
if (-not (Test-Path $python)) {
    Fail 'No venv at backend\venv. Run: cd backend; python -m venv venv; venv\Scripts\Activate.ps1; pip install -r requirements.txt'
}
$ffmpegPath = Get-Setting 'FFMPEG_PATH'
if ($ffmpegPath) {
    if (-not (Test-Path $ffmpegPath)) { Fail "FFMPEG_PATH does not exist: $ffmpegPath" }
} elseif (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    & $python -c "import imageio_ffmpeg; imageio_ffmpeg.get_ffmpeg_exe()" 2>$null
    if ($LASTEXITCODE -ne 0) { Fail 'ffmpeg not found. Install it (winget install Gyan.FFmpeg) or set FFMPEG_PATH in backend\.env.' }
}
if (-not (Test-Path (Join-Path $frontend 'dist\index.html'))) {
    Write-Host 'Frontend build missing; building it...'
    Push-Location $frontend
    try {
        if (-not (Test-Path 'node_modules')) { npm install; if ($LASTEXITCODE -ne 0) { Fail 'npm install failed' } }
        npm run build
        if ($LASTEXITCODE -ne 0) { Fail 'npm run build failed' }
    } finally { Pop-Location }
}
if (Test-Url "http://127.0.0.1:$port/api/styles") { Fail "Something already answers on port $port. Stop it first." }

New-Item -ItemType Directory -Force $logDir | Out-Null
$started = @()

try {
    # --- llama-server, only when /health does not answer ---
    $llmBase = Get-Setting 'LLM_BASE_URL' 'http://127.0.0.1:8080/v1'
    $llmRoot = $llmBase -replace '/v1/?$', ''
    if (Test-Url "$llmRoot/health") {
        Write-Host "llama-server already running at $llmRoot; leaving it alone."
    } else {
        $exe = Get-Setting 'LLAMA_SERVER_PATH'
        if (-not $exe) { $cmd = Get-Command llama-server -ErrorAction SilentlyContinue; if ($cmd) { $exe = $cmd.Source } }
        $model = Get-Setting 'LLM_MODEL_PATH'
        $mmproj = Get-Setting 'LLM_MMPROJ_PATH'
        $ctx = Get-Setting 'LLM_CTX' '8192'
        if (-not $exe -or -not (Test-Path $exe)) { Fail 'llama-server not found. Set LLAMA_SERVER_PATH in backend\.env.' }
        if (-not $model -or -not (Test-Path $model)) { Fail 'Model not found. Set LLM_MODEL_PATH in backend\.env.' }
        if (-not $mmproj -or -not (Test-Path $mmproj)) { Fail 'mmproj not found. Set LLM_MMPROJ_PATH in backend\.env.' }
        $llmPort = ([uri]$llmRoot).Port
        $llmLog = Join-Path $logDir 'llama-server.log'
        Write-Host "Starting llama-server (log: $llmLog)"
        $llmArgs = @('-m', $model, '--mmproj', $mmproj, '-c', $ctx, '-np', '1', '-ngl', '99', '--port', $llmPort, '--jinja')
        $llm = Start-Process -FilePath $exe -ArgumentList $llmArgs -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $llmLog -RedirectStandardError "$llmLog.err"
        $started += $llm
        Write-Host 'Waiting for the model to load...'
        $deadline = (Get-Date).AddMinutes(10)
        while (-not (Test-Url "$llmRoot/health")) {
            if ($llm.HasExited) { Fail "llama-server exited (see $llmLog.err)." }
            if ((Get-Date) -gt $deadline) { Fail "llama-server did not become healthy in 10 minutes (see $llmLog.err)." }
            Start-Sleep -Seconds 2
        }
        Write-Host 'llama-server is ready.'
    }

    # --- backend (serves the API and the built frontend) ---
    $apiLog = Join-Path $logDir 'backend.log'
    Write-Host "Starting the backend on port $port (log: $apiLog)"
    $api = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--port', $port) `
        -WorkingDirectory $backend -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $apiLog -RedirectStandardError "$apiLog.err"
    $started += $api
    $deadline = (Get-Date).AddSeconds(60)
    while (-not (Test-Url "http://127.0.0.1:$port/api/styles")) {
        if ($api.HasExited) { Fail "Backend exited (see $apiLog.err)." }
        if ((Get-Date) -gt $deadline) { Fail "Backend did not start in 60 seconds (see $apiLog.err)." }
        Start-Sleep -Seconds 1
    }
    Start-Process "http://localhost:$port"
    Write-Host "Maneframe is running at http://localhost:$port. Press Ctrl+C to stop." -ForegroundColor Green
    while (-not $api.HasExited) { Start-Sleep -Seconds 1 }
    Write-Host "The backend exited (see $apiLog.err)." -ForegroundColor Yellow
} finally {
    # Ctrl+C lands here too. Only processes this script started are stopped, with their children.
    foreach ($p in $started) {
        if (-not $p.HasExited) {
            Write-Host "Stopping $($p.ProcessName) (pid $($p.Id))"
            taskkill /PID $p.Id /T /F | Out-Null
        }
    }
}
