"""Runtime configuration.

Everything is env-driven so that the same image runs in three modes:

* laptop P0  : sqlite + in-memory queue, reviewer = deterministic rules
* staging    : postgres + redis, reviewer = upstream OCR CLI, dry-run comments
* production : postgres + redis, real comments, real budgets

No module reads ``os.environ`` directly outside this file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    app_name: str = "pr-review-platform"
    version: str = "0.2.0"
    # Accept both APP_ENV (compose/.env) and ENV (12-factor convention).
    env: str = Field("dev", validation_alias=AliasChoices("APP_ENV", "ENV"))

    # --- storage ---
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    queue_backend: str = "memory"  # memory | redis
    redis_url: str = "redis://127.0.0.1:6379/0"
    data_dir: str = "./data"

    # --- github ---
    github_webhook_secret: str = "dev-secret"
    github_token: str = ""
    github_api_base: str = "https://api.github.com"
    github_dry_run: bool = True

    # --- reviewer ---
    ocr_mode: str = "fake"  # fake | cli
    ocr_command: str = "ocr"
    ocr_timeout_s: int = 600
    # The CLI stores its config under %USERPROFILE%\.opencodereview and chmods it;
    # on some Windows profiles that path is not writable, so we point HOME at a
    # project-local directory instead of fighting the OS.
    ocr_home: str = "./tools/ocrhome"
    rules_dir: str = "./rules"
    max_findings_per_file: int = 10
    # Guard rails: a 50MB diff would OOM the worker before any review happens.
    max_diff_bytes: int = 2_000_000
    # A run stuck in 'running' longer than this is considered orphaned.
    run_stale_after_s: int = 900

    # --- worker / delivery ---
    worker_concurrency: int = 2
    post_retries: int = 3
    post_retry_base_s: float = 0.5
    # 0 = unlimited; can be overridden per repo.
    repo_hourly_run_limit: int = 0
    # 0 = keep forever.
    retention_days: int = 30
    log_json: bool = False

    # --- model pricing (USD per 1M tokens, used to estimate cost) ---
    llm_price_in_per_1m: float = 0.27
    llm_price_out_per_1m: float = 1.10

    # --- crypto ---
    fernet_key: str = ""

    # --- API hardening ---
    # When set, every mutating /api/* call must carry X-API-Token.
    api_token: str = ""
    # Only these hosts may be cloned from (comma separated).
    allowed_clone_hosts: str = "github.com"
    # Hard cap for request bodies and diffs (bytes).
    max_request_bytes: int = 2_000_000

    @property
    def allowed_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.allowed_clone_hosts.split(",") if h.strip()}

    def security_problems(self) -> list[str]:
        """Configuration that is acceptable locally but not in production."""
        problems: list[str] = []
        if self.env in ("dev", "test"):
            return problems
        if self.github_webhook_secret in ("", "dev-secret", "change-me-in-local"):
            problems.append("GITHUB_WEBHOOK_SECRET is missing or a dev default")
        if not self.api_token:
            problems.append("API_TOKEN is empty: mutating endpoints would be unauthenticated")
        if not self.fernet_key:
            problems.append("FERNET_KEY is empty: per-repo tokens cannot be stored encrypted")
        return problems

    def enforce_security(self) -> None:
        """Fail fast at boot instead of discovering this after exposure."""
        problems = self.security_problems()
        if problems:
            raise RuntimeError("insecure configuration: " + "; ".join(problems))

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def rules_path(self) -> Path:
        return Path(self.rules_dir).resolve()

    def boot_warnings(self) -> list[str]:
        warns: list[str] = []
        if self.env != "dev" and self.github_webhook_secret in ("", "dev-secret", "change-me-in-local"):
            warns.append("webhook secret is the dev default; rotate before exposing the endpoint")
        if self.env != "dev" and not self.fernet_key:
            warns.append("FERNET_KEY is empty; repo tokens cannot be stored safely")
        if self.github_dry_run:
            warns.append("GITHUB_DRY_RUN=true: reviews are recorded, never posted")
        if self.queue_backend == "memory":
            warns.append("QUEUE_BACKEND=memory: jobs are lost on restart (single-process only)")
        return warns


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
