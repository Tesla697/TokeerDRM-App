# =============================================================================
#  TokeerDRM App - one-click installer
#  Downloads the latest TokeerDRM.exe, installs it to %LOCALAPPDATA%\TokeerDRM,
#  makes Desktop + Start Menu shortcuts, and launches it.
#  Run:  irm https://git.lua.tools/luatools-dedivision/TokeerDRM-App/raw/branch/main/install_app.ps1 | iex
# =============================================================================
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
$Host.UI.RawUI.WindowTitle = 'TokeerDRM App Setup'
$UA = @{ 'User-Agent' = 'TokeerDRM' }

function Step($m) { Write-Host "`n[*] $m" -ForegroundColor Cyan }
function Good($m) { Write-Host "    [+] $m" -ForegroundColor Green }
function Die($m)  { Write-Host "`n[-] $m" -ForegroundColor Red; Read-Host 'Press Enter to exit'; exit 1 }

Write-Host "`n=== TokeerDRM App Setup ===`n" -ForegroundColor Magenta

# 1. find the latest release exe
Step 'Finding the latest TokeerDRM release...'
# luastools.xyz mirror first: it serves the exe from Cloudflare's edge. Forgejo's
# own download links skip the CDN and crawl on some routes.
$rel = $null
foreach ($api in @('https://luastools.xyz/app/latest',
                   'https://git.lua.tools/api/v1/repos/luatools-dedivision/TokeerDRM-App/releases/latest')) {
    try { $rel = Invoke-RestMethod $api -Headers $UA -TimeoutSec 20; if ($rel.tag_name) { break } } catch { $rel = $null }
}
if (-not $rel) { Die "Couldn't reach the release server. Check your connection and try again." }
$asset = $rel.assets | Where-Object { $_.name -match '(?i)\.exe$' } | Select-Object -First 1
if (-not $asset) { Die 'No .exe found on the latest release.' }
Good "Latest: $($rel.tag_name)"
$sources = @(
    "https://luastools.xyz/app/asset?tag=$($rel.tag_name)&name=$($asset.name)",
    "https://git.lua.tools/luatools-dedivision/TokeerDRM-App/releases/download/$($rel.tag_name)/$($asset.name)"
) | Select-Object -Unique

# 2. close any running instance so we can overwrite it
Get-Process -Name 'TokeerDRM' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 1

# 3. download into a stable per-user folder (so the app's self-update works cleanly)
$dir = Join-Path $env:LOCALAPPDATA 'TokeerDRM'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$exe = Join-Path $dir 'TokeerDRM.exe'
Step "Downloading $($asset.name)..."
$ok = $false
foreach ($src in $sources) {
    try {
        Invoke-WebRequest $src -OutFile $exe -Headers $UA -UseBasicParsing
        if ((Get-Item $exe).Length -eq [int64]$asset.size -or -not $asset.size) { $ok = $true; break }
    } catch { Write-Host "    [!] $src failed, trying the next one" -ForegroundColor Yellow }
}
if (-not $ok) { Die 'Download failed from every server. Check your connection and try again.' }
Good "Installed to $exe"

# 4. Desktop + Start Menu shortcuts
Step 'Creating shortcuts...'
try {
    $ws = New-Object -ComObject WScript.Shell
    foreach ($lnkDir in @([Environment]::GetFolderPath('Desktop'),
                          (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
        $lnk = $ws.CreateShortcut((Join-Path $lnkDir 'TokeerDRM.lnk'))
        $lnk.TargetPath       = $exe
        $lnk.WorkingDirectory = $dir
        $lnk.IconLocation     = $exe
        $lnk.Description       = 'TokeerDRM - activate Denuvo codes'
        $lnk.Save()
    }
    Good 'Desktop + Start Menu shortcuts created.'
} catch { Write-Host "    [!] Couldn't create a shortcut (not fatal)." -ForegroundColor Yellow }

# 5. launch
Step 'Launching TokeerDRM...'
Start-Process $exe
Write-Host "`n[OK] Done! TokeerDRM is open. In the app: set up the engine (or use LuaTools)," -ForegroundColor Green
Write-Host "     then paste your code in Activate and click Apply.`n" -ForegroundColor Green
Start-Sleep 2
