"""Persistence model.

Design notes (kept here because they are interview answers):

* ``deliveries`` gives webhook idempotency: GitHub retries deliveries, and a
  retried delivery must not create a second review.  The unique constraint is
  the source of truth, not application state.
* ``runs`` is keyed by ``(repo_id, pr_number, head_sha)``: pushing three
  commits to one PR yields three runs, re-delivering the same webhook yields
  one.  That is the difference between "rerun" and "duplicate".
* ``findings.dedup_key`` lets a later run reuse the same finding without
  spamming the PR with identical comments.
* ``feedbacks`` is the seed of the improvement loop: false positives are data,
  not noise.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), default="github")
    full_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    clone_url: Mapped[str] = mapped_column(String(512), default="")
    default_branch: Mapped[str] = mapped_column(String(128), default="main")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=utcnow_iso)


class Delivery(Base):
    """One received webhook. Unique (provider, delivery_id) == idempotency."""

    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("provider", "delivery_id", name="uq_delivery"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), default="github")
    delivery_id: Mapped[str] = mapped_column(String(128), index=True)
    event: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), default="")
    repo_full_name: Mapped[str] = mapped_column(String(255), default="")
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    received_at: Mapped[str] = mapped_column(String(32), default=utcnow_iso)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("repo_id", "pr_number", "head_sha", name="uq_run_head"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"), index=True)
    pr_number: Mapped[int] = mapped_column(Integer, index=True)
    base_sha: Mapped[str] = mapped_column(String(64), default="")
    head_sha: Mapped[str] = mapped_column(String(64), default="")
    trigger: Mapped[str] = mapped_column(String(32), default="webhook")
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    # P0 only: replay/demo runs carry their diff; CLI mode clones instead.
    diff_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=utcnow_iso)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ended_at: Mapped[str | None] = mapped_column(String(32), nullable=True)

    repo: Mapped[Repo] = relationship(lazy="selectin")
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (UniqueConstraint("run_id", "dedup_key", name="uq_finding_dedup"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(1024))
    line_start: Mapped[int] = mapped_column(Integer, default=1)
    line_end: Mapped[int] = mapped_column(Integer, default=1)
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    rule_key: Mapped[str] = mapped_column(String(128), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(64), index=True)
    posted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(32), default=utcnow_iso)

    run: Mapped[Run] = relationship(back_populates="findings")
    feedbacks: Mapped[list["Feedback"]] = relationship(
        back_populates="finding", cascade="all, delete-orphan", lazy="selectin"
    )


class Feedback(Base):
    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"), index=True)
    verdict: Mapped[str] = mapped_column(String(24))  # false_positive | fixed | ignored | agreed
    actor: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32), default=utcnow_iso)

    finding: Mapped[Finding] = relationship(back_populates="feedbacks")


class CostRecord(Base):
    __tablename__ = "costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    repo_full_name: Mapped[str] = mapped_column(String(255), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    engine: Mapped[str] = mapped_column(String(32), default="fake")
    created_at: Mapped[str] = mapped_column(String(32), default=utcnow_iso)


class RuleCandidate(Base):
    """A proposed rule change, born from clustered false positives.

    The state machine is deliberately small: ``draft -> approved | rejected``.
    Nothing here edits a rule automatically — approval is a human gate, because
    a bad rule silences real findings and that is worse than a false positive.
    """

    __tablename__ = "rule_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_key: Mapped[str] = mapped_column(String(128), index=True)
    action_hint: Mapped[str] = mapped_column(String(32), default="review-rule")
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    owner: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=utcnow_iso)
    decided_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


__all__ = [
    "Repo",
    "Delivery",
    "Run",
    "Finding",
    "Feedback",
    "CostRecord",
    "RuleCandidate",
    "utcnow_iso",
]
