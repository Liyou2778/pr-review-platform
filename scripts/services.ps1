# Portable Postgres + Redis lifecycle for this repo (no admin, no Windows services).
# Usage: .\scripts\services.ps1 init | start | stop | status | logs
param(
    [Parameter(Position = 0)]
    [ValidateSet('init', 'start', 'stop', 'status', 'logs')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$PgBin = Join-Path $Root 'tools\pgsql\bin'
$PgData = Join-Path $Root 'tools\pgdata'
$PgLog = Join-Path $Root 'tools\pg.log'
$RedisDir = Join-Path $Root 'tools\redis'
$RedisExe = Join-Path $RedisDir 'redis-server.exe'
$RedisCli = Join-Path $RedisDir 'redis-cli.exe'
$PgPort = 5432
$RedisPort = 6379

function Test-Pg {
    & "$PgBin\pg_isready.exe" -h 127.0.0.1 -p $PgPort -q 2>$null
    return $LASTEXITCODE -eq 0
}

function Test-Redis {
    try {
        $pong = & "$RedisCli" -h 127.0.0.1 -p $RedisPort ping 2>$null
        return $pong -eq 'PONG'
    } catch { return $false }
}

switch ($Action) {
    'init' {
        if (-not (Test-Path "$PgBin\initdb.exe")) { throw "Postgres binaries missing: $PgBin" }
        if (Test-Path $PgData) {
            Write-Host 'pgdata already exists, skip initdb' -ForegroundColor Yellow
        } else {
            & "$PgBin\initdb.exe" -D $PgData -U prreview -A trust -E UTF8 --locale=C | Out-Null
            Write-Host 'initdb done' -ForegroundColor Green
        }
        if (-not (Test-Path $RedisDir)) { throw "Redis binaries missing: $RedisDir" }
        Write-Host 'init ok' -ForegroundColor Green
    }
    'start' {
        if (-not (Test-Path $PgData)) { throw "run 'services.ps1 init' first" }
        if (Test-Pg) {
            Write-Host "postgres already up on :$PgPort" -ForegroundColor Yellow
        } else {
            # Redirect stdout/stderr to files: if the postmaster inherits our pipe,
            # the calling process never sees EOF and the service dies with it.
            $out = Join-Path $Root 'tools\pgctl.out'
            # -o takes ONE string; Start-Process does not quote array items, so
            # build the command line explicitly (otherwise "5432" becomes a mode).
            $argLine = "-D `"$PgData`" -l `"$PgLog`" -o `"-p $PgPort -c listen_addresses=127.0.0.1`" -w -t 60 start"
            $proc = Start-Process -FilePath "$PgBin\pg_ctl.exe" -ArgumentList $argLine `
                -RedirectStandardOutput $out -RedirectStandardError "$out.err" -NoNewWindow -PassThru
            $proc.WaitForExit(70000) | Out-Null
            if (Test-Pg) { Write-Host "postgres up on :$PgPort" -ForegroundColor Green }
            else { throw "postgres failed to start; see $PgLog" }
        }

        if (Test-Redis) {
            Write-Host "redis already up on :$RedisPort" -ForegroundColor Yellow
        } else {
            Start-Process -FilePath $RedisExe -ArgumentList 'redis.conf' -WorkingDirectory $RedisDir `
                -WindowStyle Hidden | Out-Null
            for ($i = 0; $i -lt 40; $i++) {
                Start-Sleep -Milliseconds 250
                if (Test-Redis) { break }
            }
            if (Test-Redis) { Write-Host "redis up on :$RedisPort" -ForegroundColor Green }
            else { throw 'redis failed to start' }
        }

        # create the app database if missing
        $exists = & "$PgBin\psql.exe" -h 127.0.0.1 -p $PgPort -U prreview -d postgres -tAc "select 1 from pg_database where datname='prreview';" 2>$null
        if ("$exists".Trim() -ne '1') {
            & "$PgBin\createdb.exe" -h 127.0.0.1 -p $PgPort -U prreview prreview
            Write-Host 'database prreview created' -ForegroundColor Green
        }
    }
    'stop' {
        if (Test-Pg) { & "$PgBin\pg_ctl.exe" -D $PgData -m fast stop | Out-Null; Write-Host 'postgres stopped' }
        if (Test-Redis) { & "$RedisCli" -h 127.0.0.1 -p $RedisPort shutdown nosave 2>$null | Out-Null; Write-Host 'redis stopped' }
    }
    'status' {
        "{0,-10} {1}" -f 'postgres', $(if (Test-Pg) { "UP :$PgPort" } else { 'DOWN' })
        "{0,-10} {1}" -f 'redis', $(if (Test-Redis) { "UP :$RedisPort" } else { 'DOWN' })
    }
    'logs' {
        if (Test-Path $PgLog) { Get-Content $PgLog -Tail 30 }
    }
}
