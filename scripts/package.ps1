<#
Builds the portable Windows package: build\maneframe-<git describe>-win64.zip. Run from the repo root on a machine with
Node, git, the backend venv (Python 3.12) and internet. Not run in CI. Nothing in build\ is ever committed.
Contents: private Python (python.org embeddable) + backend\requirements.txt, frontend\dist, backend\app, the llama.cpp
Vulkan llama-server (pinned) with its LICENSE, THIRD_PARTY_NOTICES (licence texts copied from each component), and Start Maneframe.bat, Change model.bat, launcher.py and models.json from packaging\.
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$pyVersion = '3.12.10'   # must match the venv's minor version (3.12), wheels are fetched for cp312
$llamaTag = 'b10679'
$pyUrl = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"
$llamaUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$llamaTag/llama-$llamaTag-bin-win-vulkan-x64.zip"
$pySha = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'      # python.org, checked twice on 2026-11-09
$llamaSha = 'd288a375a324f650a587d3b876afe692ca3586110f20b863fadbe91dd3b93469'   # matches GitHub's release digest
$llamaLicenseUrl = "https://raw.githubusercontent.com/ggml-org/llama.cpp/$llamaTag/LICENSE"

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Run($exe, $argList) { & $exe @argList; if ($LASTEXITCODE -ne 0) { Fail "$exe failed ($LASTEXITCODE)" } }

$sw = [Diagnostics.Stopwatch]::StartNew()
$venvPython = Join-Path $root 'backend\venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) { Fail 'No backend\venv. See README Quickstart.' }
$version = (git describe --tags --always).Trim()
$name = "maneframe-$version-win64"
$build = Join-Path $root 'build'
$cache = Join-Path $build 'cache'
$stage = Join-Path $build "stage\$name"
$zip = Join-Path $build "$name.zip"
New-Item -ItemType Directory -Force $cache | Out-Null
if (Test-Path (Join-Path $build 'stage')) { Remove-Item -Recurse -Force (Join-Path $build 'stage') }
if (Test-Path $zip) { Remove-Item -Force $zip }
New-Item -ItemType Directory -Force $stage | Out-Null

function Get-Cached($url, $sha256) {
    $file = Join-Path $cache ([IO.Path]::GetFileName(([uri]$url).AbsolutePath))
    if (-not (Test-Path $file)) { Write-Host "Downloading $url"; Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing }
    $got = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $sha256) { Remove-Item -Force $file; Fail "sha256 mismatch for $(Split-Path $file -Leaf): got $got, expected $sha256 (file deleted)" }
    Write-Host ("  {0}  sha256 ok" -f (Split-Path $file -Leaf))
    return $file
}

# 1. Frontend
Write-Host '== frontend =='
Push-Location (Join-Path $root 'frontend')
try {
    if (-not (Test-Path 'node_modules')) { Run npm @('ci') }
    Run npm @('run', 'build')
} finally { Pop-Location }
if (-not (Test-Path (Join-Path $root 'frontend\dist\index.html'))) { Fail 'frontend\dist\index.html missing after build' }

# 2. Python: the embeddable runtime, then the packages installed into it from wheels (no pip inside the package)
Write-Host '== python =='
$pyDir = Join-Path $stage 'python'
Expand-Archive -Path (Get-Cached $pyUrl $pySha) -DestinationPath $pyDir
$pth = Get-ChildItem $pyDir -Filter 'python*._pth' | Select-Object -First 1
$zipName = (Get-ChildItem $pyDir -Filter 'python*.zip' | Select-Object -First 1).Name
# Paths are relative to the ._pth file. ..\backend makes "app" importable with the backend as working directory.
Set-Content -Path $pth.FullName -Encoding ascii -Value @($zipName, '.', 'Lib\site-packages', '..\backend')
$site = Join-Path $pyDir 'Lib\site-packages'
New-Item -ItemType Directory -Force $site | Out-Null
$req = Join-Path $root 'backend\requirements.txt'
if (Select-String -Path $req -Pattern 'piper|kokoro' -Quiet) { Fail 'requirements.txt lists piper or kokoro; the package excludes them.' }
Run $venvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-compile', '--target', $site, '--only-binary=:all:',
    '--platform', 'win_amd64', '--python-version', '3.12', '--implementation', 'cp', '-r', $req)
# ffmpeg is not bundled (D18): drop the GPL binary that imageio-ffmpeg ships; the launcher downloads it at first run
$ffPin = Get-Content (Join-Path $root 'packaging\ffmpeg.json') -Raw | ConvertFrom-Json
if (-not (Select-String -Path $req -Pattern ("^imageio-ffmpeg==" + [regex]::Escape($ffPin.version) + "\s*$") -Quiet)) {
    Fail "packaging\ffmpeg.json version $($ffPin.version) differs from the imageio-ffmpeg pin in backend\requirements.txt"
}
$ffBin = Join-Path $site 'imageio_ffmpeg\binaries'
if (Test-Path $ffBin) { Get-ChildItem $ffBin -Force | Remove-Item -Recurse -Force }
# VC++ runtime DLLs next to python.exe and llama-server.exe (app-local deployment) for machines without the redistributable
$vcDlls = 'msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'

# 3. llama.cpp (Vulkan build, pinned tag) and its license
Write-Host '== llama.cpp =='
$llamaDir = Join-Path $stage 'llama'
Expand-Archive -Path (Get-Cached $llamaUrl $llamaSha) -DestinationPath $llamaDir
if (-not (Test-Path (Join-Path $llamaDir 'llama-server.exe'))) { Fail 'llama-server.exe not found in the llama.cpp zip' }
Invoke-WebRequest -Uri $llamaLicenseUrl -OutFile (Join-Path $llamaDir 'LICENSE') -UseBasicParsing
# Only llama-server is used; drop the other tools to keep the zip small
Get-ChildItem $llamaDir -Filter '*.exe' | Where-Object { $_.Name -ne 'llama-server.exe' } | Remove-Item -Force
foreach ($d in $vcDlls) {
    $src = Join-Path $env:SystemRoot "System32\$d"
    if (Test-Path $src) { Copy-Item $src $pyDir -Force; Copy-Item $src $llamaDir -Force }
}

# 4. The app itself (same layout as the repo, so backend code finds frontend\dist unchanged)
Write-Host '== app =='
New-Item -ItemType Directory -Force (Join-Path $stage 'backend') | Out-Null
Copy-Item (Join-Path $root 'backend\app') (Join-Path $stage 'backend\app') -Recurse
Get-ChildItem (Join-Path $stage 'backend') -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
New-Item -ItemType Directory -Force (Join-Path $stage 'frontend') | Out-Null
Copy-Item (Join-Path $root 'frontend\dist') (Join-Path $stage 'frontend\dist') -Recurse
Copy-Item (Join-Path $root 'packaging\Start Maneframe.bat') $stage
Copy-Item (Join-Path $root 'packaging\launcher.py') $stage
Copy-Item (Join-Path $root 'packaging\models.json') $stage
Copy-Item (Join-Path $root 'packaging\ffmpeg.json') $stage
Copy-Item (Join-Path $root 'packaging\Change model.bat') $stage
Copy-Item (Join-Path $root 'packaging\Read me first.txt') $stage
Copy-Item (Join-Path $root 'LICENSE') (Join-Path $stage 'LICENSE.txt')
Set-Content -Path (Join-Path $stage 'VERSION.txt') -Encoding ascii -Value $version

# 4b. Licence texts of everything bundled, copied from the files themselves (nothing is written by hand)
Write-Host '== licence notices =='
$notices = Join-Path $stage 'THIRD_PARTY_NOTICES'
$licPattern = '^(LICEN[CS]E|COPYING|NOTICE|COPYRIGHT)'
function Copy-Notice($src, $destDir) {
    New-Item -ItemType Directory -Force $destDir | Out-Null
    Copy-Item $src $destDir -Force
}
$pyLicense = Join-Path $pyDir 'LICENSE.txt'
if (-not (Test-Path $pyLicense)) { Fail 'python\LICENSE.txt missing from the embeddable Python' }
Copy-Notice $pyLicense (Join-Path $notices 'Python')
foreach ($f in 'LICENSE', 'LICENSE-LLVM-OpenMP') {
    $src = Join-Path $llamaDir $f
    if (Test-Path $src) { Copy-Notice $src (Join-Path $notices 'llama.cpp') } elseif ($f -eq 'LICENSE') { Fail 'llama.cpp LICENSE missing' }
}
$noLicense = @()
$count = 0
foreach ($di in Get-ChildItem $site -Directory -Filter '*.dist-info') {
    $files = @(Get-ChildItem $di.FullName -Recurse -File | Where-Object { $_.Name -match $licPattern })
    if ($files.Count -eq 0) { $noLicense += $di.Name -replace '\.dist-info$', ''; continue }
    $dest = Join-Path $notices (Join-Path 'python-packages' ($di.Name -replace '\.dist-info$', ''))
    New-Item -ItemType Directory -Force $dest | Out-Null
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($di.FullName.Length).TrimStart([char]92)
        $target = Join-Path $dest $rel
        New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
        Copy-Item $f.FullName $target -Force
    }
    $count++
}
Set-Content -Path (Join-Path $notices 'INDEX.txt') -Encoding utf8 -Value @(
    'Licence texts of the software bundled in this package, copied unchanged from each component at build time.',
    'Python\            the Python runtime',
    'llama.cpp\         llama-server and its libraries',
    'python-packages\   one folder per Python package (from its .dist-info)',
    'ffmpeg is not bundled: the launcher downloads it at first run from PyPI (imageio-ffmpeg), licensed GPL v3.',
    "OpenCV's opencv_videoio_ffmpeg DLL is an LGPL ffmpeg build; its licence is in opencv's LICENSE-3RD-PARTY.txt and its source is published by the opencv-python project.",
    'Language models are not bundled; they are downloaded at first run and carry their own licences (see models.json).')
Write-Host ("  licence files for Python, llama.cpp and {0} packages" -f $count)
if ($noLicense.Count) { Write-Host ("  NO licence file in: " + ($noLicense -join ', ')) -ForegroundColor Yellow }

# Precompile so the first start is not slow (bytecode is not written at run time)
Run (Join-Path $pyDir 'python.exe') @('-m', 'compileall', '-q', '-j', '0', $site, (Join-Path $stage 'backend\app'))

$ffLeft = @(Get-ChildItem $stage -Recurse -File -Filter 'ffmpeg*.exe')
if ($ffLeft.Count) { Fail ("ffmpeg executable still in the stage: " + ($ffLeft.FullName -join ', ')) }

# 5. Zip (.NET writes forward-slash entries; Compress-Archive in 5.1 does not)
Write-Host '== zip =='
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $build 'stage'), $zip, [IO.Compression.CompressionLevel]::Optimal, $false)
Remove-Item -Recurse -Force (Join-Path $build 'stage')
$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ("Built {0}: {1} MB in {2:N0} s" -f $zip, $mb, $sw.Elapsed.TotalSeconds) -ForegroundColor Green
