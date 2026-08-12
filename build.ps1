# Builds dist\WorkspaceAgent.exe — a single file with Python, every dependency,
# and your Google OAuth client baked in. Nothing to install on the far end.
#
#   .\build.ps1
#
# Put your downloaded Desktop OAuth client at client_secret.json in this folder
# first, or the build produces an app that cannot sign in to Google.
#
# No AI API key is ever built into the .exe, and there is no switch to put one
# there. Each recipient connects their own provider on first run — see the BYOK
# section of README.md.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No virtualenv. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}

# PyInstaller is a build-time tool only, not a runtime dependency.
# (Probing with `python -c` rather than `-m PyInstaller --version`: redirecting
# a native command's stderr in PowerShell 5.1 raises a terminating error.)
& $python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller (build-time only)..." -ForegroundColor Yellow
    & $python -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Could not install PyInstaller." }
}

# Accept Google's long default download name as well as a tidy rename.
$secret = Get-ChildItem -Path $PSScriptRoot -Filter "client_secret*.json" -File |
    Sort-Object Name | Select-Object -First 1
$bundleSecret = $null -ne $secret
if ($bundleSecret) {
    Write-Host "Bundling OAuth client: $($secret.Name)" -ForegroundColor DarkGray
} else {
    Write-Host ""
    Write-Host "WARNING: no client_secret*.json in this folder." -ForegroundColor Yellow
    Write-Host "The build will succeed, but users will not be able to sign in to Google." -ForegroundColor Yellow
    Write-Host "See DISTRIBUTION.md step 1." -ForegroundColor Yellow
    Write-Host ""
}

# The accounts gate needs the Supabase project baked in, because a downloaded
# copy has no .env to read it from and would otherwise run ungated. Only the
# anon key goes in -- it is the public half of the project and is what Supabase
# intends clients to ship. SUPABASE_SERVICE_ROLE_KEY is never read here.
$supabaseJson = Join-Path $PSScriptRoot "supabase.json"
$bundleSupabase = $false
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    $envLines = Get-Content $envFile
    $sbUrl = ($envLines | Where-Object { $_ -match '^\s*SUPABASE_URL\s*=' } |
        Select-Object -First 1) -replace '^\s*SUPABASE_URL\s*=\s*', ''
    $sbKey = ($envLines | Where-Object { $_ -match '^\s*SUPABASE_ANON_KEY\s*=' } |
        Select-Object -First 1) -replace '^\s*SUPABASE_ANON_KEY\s*=\s*', ''
    $sbUrl = $sbUrl.Trim().Trim('"').Trim("'")
    $sbKey = $sbKey.Trim().Trim('"').Trim("'")
    if ($sbUrl -and $sbKey) {
        @{ url = $sbUrl; anon_key = $sbKey } | ConvertTo-Json |
            Out-File -FilePath $supabaseJson -Encoding utf8
        $bundleSupabase = $true
        Write-Host "Bundling Supabase project: $sbUrl" -ForegroundColor DarkGray
    }
}
if (-not $bundleSupabase) {
    Write-Host ""
    Write-Host "WARNING: no SUPABASE_URL/SUPABASE_ANON_KEY found in .env." -ForegroundColor Yellow
    Write-Host "The build will run WITHOUT the account gate - anyone who downloads it" -ForegroundColor Yellow
    Write-Host "can use the agent without signing up." -ForegroundColor Yellow
    Write-Host ""
}

# A .env in the build folder is a developer's own file. It is never bundled:
# the packaged app reads keys from the OS credential store, and a stray .env
# riding along inside the .exe is exactly how a developer's key would leak to
# whoever they sent it to.
if (Test-Path (Join-Path $PSScriptRoot ".env")) {
    Write-Host "Note: .env stays on this machine — it is not bundled into the .exe." -ForegroundColor DarkGray
}

$arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--console",
    "--name", "WorkspaceAgent",
    # Google's client library ships discovery documents as package data.
    "--collect-data", "googleapiclient",
    "--collect-data", "google_auth_oauthlib",
    # anthropic reads its own version from installed metadata at import time.
    "--copy-metadata", "anthropic",
    # Tool modules are imported dynamically by registry.load_all().
    "--hidden-import", "agent.tools.gmail",
    "--hidden-import", "agent.tools.drive",
    "--hidden-import", "agent.tools.calendar",
    "--hidden-import", "agent.tools.contacts",
    "--hidden-import", "agent.tools.tasks",
    "--hidden-import", "agent.tools.localfiles",
    # Provider modules are reached through the registry in providers/catalog.py.
    "--hidden-import", "agent.providers.openai_provider",
    "--hidden-import", "agent.providers.anthropic_provider",
    "--hidden-import", "agent.providers.gemini_provider"
)
if ($bundleSecret) {
    $arguments += @("--add-data", "$($secret.Name);.")
}
if ($bundleSupabase) {
    $arguments += @("--add-data", "supabase.json;.")
}
$arguments += "launcher.py"

Write-Host "Building..." -ForegroundColor Cyan
# PyInstaller logs progress to stderr. Under ErrorActionPreference=Stop that
# becomes a terminating NativeCommandError the moment anything redirects this
# script's output, so relax it here and judge success by the exit code.
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $python @arguments
$buildCode = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($buildCode -ne 0) { throw "PyInstaller failed with exit code $buildCode." }

$exe = Join-Path $PSScriptRoot "dist\WorkspaceAgent.exe"
$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)

Write-Host ""
Write-Host "Built dist\WorkspaceAgent.exe ($sizeMb MB)" -ForegroundColor Green
if ($bundleSecret) {
    Write-Host "Google OAuth client is bundled. Send the .exe as-is." -ForegroundColor Green
} else {
    Write-Host "No OAuth client bundled - sign-in will not work." -ForegroundColor Yellow
}
Write-Host "No AI API key is in this build. Each recipient connects their own provider." -ForegroundColor Green
Write-Host "Smoke test it with:  .\dist\WorkspaceAgent.exe --check" -ForegroundColor DarkGray
