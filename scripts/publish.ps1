# Create the GitHub repo (if missing) and push main. Reads the token from .env;
# the token is never printed. Run: .\scripts\publish.ps1 [-Name pr-review-platform] [-Private]
param(
    [string]$Name = 'pr-review-platform',
    [switch]$Private,
    [string]$Description = 'Enterprise PR review agent platform: webhook -> queue -> review -> findings -> feedback loop'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# --- read token from .env without printing it ---
$tokenLine = Get-Content "$Root\.env" | Where-Object { $_ -match '^GITHUB_TOKEN=' } | Select-Object -First 1
if (-not $tokenLine) { throw 'GITHUB_TOKEN not found in .env' }
$token = ($tokenLine -split '=', 2)[1].Trim()
if (-not $token) { throw 'GITHUB_TOKEN is empty in .env' }

$headers = @{
    Authorization          = "Bearer $token"
    Accept                 = 'application/vnd.github+json'
    'User-Agent'           = 'pr-review-platform-publish'
    'X-GitHub-Api-Version' = '2022-11-28'
}

# --- who are we? ---
$user = Invoke-RestMethod -Uri 'https://api.github.com/user' -Headers $headers
$login = $user.login
Write-Host "authenticated as: $login" -ForegroundColor Green

# --- repo exists? ---
$exists = $true
try { Invoke-RestMethod -Uri "https://api.github.com/repos/$login/$Name" -Headers $headers | Out-Null }
catch { $exists = $false }

if (-not $exists) {
    $body = @{
        name        = $Name
        description = $Description
        private     = [bool]$Private
        has_issues  = $true
        has_wiki    = $false
    } | ConvertTo-Json
    $repo = Invoke-RestMethod -Uri 'https://api.github.com/user/repos' -Method Post -Headers $headers `
        -ContentType 'application/json' -Body $body
    Write-Host "created repo: $($repo.html_url)" -ForegroundColor Green
} else {
    Write-Host "repo already exists: https://github.com/$login/$Name" -ForegroundColor Yellow
}

# --- remote without token, push with token in the URL (not stored) ---
$remoteUrl = "https://github.com/$login/$Name.git"
$existingRemote = git remote 2>$null
if ($existingRemote -contains 'origin') {
    git remote set-url origin $remoteUrl
} else {
    git remote add origin $remoteUrl
}
git branch -M main
git push "https://x-access-token:$token@github.com/$login/$Name.git" main --quiet
Write-Host "pushed: https://github.com/$login/$Name" -ForegroundColor Green
git remote -v | Select-Object -First 2
