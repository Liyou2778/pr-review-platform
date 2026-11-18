# One-command dev tasks for Windows (PowerShell 7+).
# Usage: .\tasks.ps1 setup | services | test | api | worker | demo | replay | ocr
param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'services', 'test', 'test-pg', 'api', 'worker', 'demo', 'replay', 'ocr', 'bench', 'bench-ocr', 'record', 'e2e', 'gate')]
    [string]$Task = 'test'
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Get-Python {
    if (Test-Path "$Root\.venv\Scripts\python.exe") { return "$Root\.venv\Scripts\python.exe" }
    return 'python'
}

$py = Get-Python

switch ($Task) {
    'setup' {
        if (-not (Test-Path "$Root\.venv")) { uv venv .venv }
        uv pip install --python "$Root\.venv\Scripts\python.exe" -e ".[dev]"
        if (-not (Test-Path "$Root\.env")) { Copy-Item "$Root\.env.example" "$Root\.env" }
        Write-Host 'setup done. edit .env, then: .\tasks.ps1 services' -ForegroundColor Green
    }
    'services' {
        & "$Root\scripts\services.ps1" start
        & "$Root\scripts\services.ps1" status
    }
    'test' {
        & $py -m pytest -q
    }
    'test-pg' {
        $env:TEST_DATABASE_URL = 'postgresql+asyncpg://prreview@127.0.0.1:5432/prreview_test'
        & $py -m pytest -q
    }
    'api' {
        & $py -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8210 --reload
    }
    'worker' {
        & $py -m apps.worker.main
    }
    'replay' {
        & $py scripts\replay_demo.py --base-url http://127.0.0.1:8210
    }
    'ocr' {
        & $py scripts\ocr_smoke.py
    }
    'bench' {
        & $py scripts\bench.py --out eval\bench\last-run.json
    }
    'bench-ocr' {
        & $py scripts\bench.py --engine recorded --out eval\bench\ocr-last-run.json
    }
    'record' {
        & $py scripts\record_ocr.py
    }
    'e2e' {
        & $py scripts\e2e_ocr.py
    }
    'gate' {
        & $py scripts\gate.py --current eval\bench\last-run.json --baseline eval\bench\baseline.json
    }
    'demo' {
        Write-Host '1) .\tasks.ps1 services  (Postgres + Redis)' -ForegroundColor Cyan
        Write-Host '2) .\tasks.ps1 api       (terminal A)' -ForegroundColor Cyan
        Write-Host '3) .\tasks.ps1 worker    (terminal B)' -ForegroundColor Cyan
        Write-Host '4) .\tasks.ps1 replay    (terminal C)' -ForegroundColor Cyan
        Write-Host '   then GET http://127.0.0.1:8210/api/runs' -ForegroundColor Cyan
    }
}
