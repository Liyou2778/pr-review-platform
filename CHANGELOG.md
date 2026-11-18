# Changelog

All notable changes to this project. Format follows Keep a Changelog; versions
follow SemVer.

## [0.2.0] - 2026-11-18

### Added
- **Per-repo policy**: `min_severity` floor and `max_runs_per_hour` budget
  (`PUT /api/repos/{owner}/{repo}/policy`), enforced before delivery and run creation.
- **Resolved state**: a finding reported earlier and gone in a later run of the
  same PR is marked `resolved` with a timestamp.
- **Delivery retries**: 3 attempts with exponential backoff; 4xx is not retried.
- **Prometheus `/metrics`**: runs by status, findings by severity, p95 latency by
  engine, estimated cost, false-positive rate.
- **Structured logging**: `LOG_JSON=true` emits one JSON object per line; every
  request carries `X-Request-Id` (echoed, and logged).
- **Retention**: `scripts/retention.py` prunes runs/findings/feedback/costs
  (dry-run by default).
- **Worker concurrency**: `WORKER_CONCURRENCY` pull loops; errors are contained
  per loop so a broker failure no longer kills the process.
- **CI**: Postgres matrix (SQLite + Postgres) and a `docker build` job.
- **ADR set** and a cross-check review report under `docs/`.

### Changed
- Review summary comment now includes a severity breakdown, not just a count.

### Fixed
- Missing token is only a delivery failure when not in dry-run.
- Oversized diffs fail fast with `diff_too_large` instead of exhausting memory.
- Orphan recovery only reclaims runs older than `RUN_STALE_AFTER_S` (a second
  worker used to steal in-flight jobs).

## [0.1.0] - 2026-11-18

### Added
- Webhook HMAC verification + delivery idempotency, atomic run claiming.
- Queue backends: memory / db / redis; worker DB fallback when the broker fails.
- Rules-as-code engine with fixtures; OCR CLI + DeepSeek adapter; git workspaces.
- Findings dedupe, feedback, rule candidates with a human approval gate.
- 20-case bench (Python/TypeScript/Java) and a two-tier CI gate.
- Dashboard: latency by engine, cost by repo, severity mix, false-positive clusters.
