# Contributing

## Local loop

```powershell
.\scripts\services.ps1 start     # portable Postgres + Redis
.\tasks.ps1 test                 # SQLite fast lane
.\tasks.ps1 test-pg              # same tests against Postgres
.\tasks.ps1 bench                # deterministic engine
.\tasks.ps1 bench-ocr            # recorded real-model run (no key needed)
.\tasks.ps1 gate                 # quality gate (F1 floor + regression)
```

## Rules are code

A rule lives in `rules/<language>/*.yaml` and must ship **positive and negative
fixtures**. CI runs every fixture; a rule without them is a rule nobody can
change safely.

```yaml
- key: py-eval-usage
  severity: high
  languages: [python]
  paths: ["**/*.py"]
  pattern: '\beval\s*\('
  message: "…"
  fixtures:
    positive: "eval(x)"
    negative: "ast.literal_eval(x)"
```

## Before opening a PR

1. `ruff check core apps tests scripts` and `pytest -q` are green.
2. `scripts/bench.py` + `scripts/gate.py` still pass (no F1 regression).
3. Behaviour changes come with a test that would fail before the change.
4. Architecture-level decisions get an ADR in `docs/adr/` (options, choice, cost).

## Commit style

`type(scope): summary` — e.g. `fix(delivery): retry only 5xx/transport errors`.
Explain *why* in the body; the diff already explains *what*.
