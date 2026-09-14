"""ORM models — the single source of truth for the whole system.

Types are kept dialect-neutral (BigInteger, JSON, timezone-aware DateTime) so the
schema runs on PostgreSQL in production and on SQLite for local tooling/tests.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _pk() -> Mapped[int]:
    # Plain Integer PK autoincrements on both PostgreSQL (SERIAL) and SQLite.
    return mapped_column(Integer, primary_key=True, autoincrement=True)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# Users / roles
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = _pk()
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created()


class RoleModel(Base):
    __tablename__ = "roles"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class Invite(Base):
    """A time-limited self-registration link. Registering through it always
    grants the minimal ``viewer`` role; an admin promotes the user afterwards."""

    __tablename__ = "invites"

    id: Mapped[int] = _pk()
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --------------------------------------------------------------------------- #
# Workers
# --------------------------------------------------------------------------- #
class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    host: Mapped[str | None] = mapped_column(String(255))
    agent_url: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(20), default="offline", nullable=False)
    agent_version: Mapped[str | None] = mapped_column(String(50))
    threads: Mapped[int | None] = mapped_column(Integer)
    storage_path: Mapped[str | None] = mapped_column(String(500))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime] = _created()

    # passive_deletes: let the DB's ON DELETE CASCADE drop the metrics, instead of
    # the ORM trying to null worker_metrics.worker_id (which is NOT NULL) first.
    metrics: Mapped[list[WorkerMetric]] = relationship(
        back_populates="worker", passive_deletes=True
    )


class WorkerMetric(Base):
    __tablename__ = "worker_metrics"
    # Speeds up "latest metric per worker" (worker_id filter + captured_at sort).
    __table_args__ = (Index("ix_worker_metrics_worker_captured", "worker_id", "captured_at"),)

    id: Mapped[int] = _pk()
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"), nullable=False)
    captured_at: Mapped[datetime] = _created()
    cpu_percent: Mapped[float | None] = mapped_column(Float)
    ram_used_mb: Mapped[float | None] = mapped_column(Float)
    ram_total_mb: Mapped[float | None] = mapped_column(Float)
    disk_free_gb: Mapped[float | None] = mapped_column(Float)
    net_in_bps: Mapped[float | None] = mapped_column(Float)
    net_out_bps: Mapped[float | None] = mapped_column(Float)
    load_avg: Mapped[float | None] = mapped_column(Float)
    active_checks: Mapped[int | None] = mapped_column(Integer)
    active_downloads: Mapped[int | None] = mapped_column(Integer)

    worker: Mapped[Worker] = relationship(back_populates="metrics")


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = _pk()
    name: Mapped[str | None] = mapped_column(String(255))
    range_start: Mapped[int | None] = mapped_column(BigInteger)
    range_end: Mapped[int | None] = mapped_column(BigInteger)  # NULL => open-ended
    # Next identifier from which a brand-new chunk will be cut (the chunking cursor).
    next_chunk_start: Mapped[int | None] = mapped_column(BigInteger)
    # Where to fetch from: kinescope | mock | youtube. Resolved from the UI source
    # (an "auto" choice is resolved to a concrete source at creation).
    source: Mapped[str] = mapped_column(String(20), default="kinescope", nullable=False)
    # Optional library group new videos are filed into automatically.
    target_group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))
    state: Mapped[str] = mapped_column(String(20), default="created", nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, default=5000, nullable=False)
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    global_rate_limit_rps: Mapped[int | None] = mapped_column(Integer)
    stop_on_consecutive_missing: Mapped[int | None] = mapped_column(Integer)
    config: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    # passive_deletes: let the DB's ON DELETE CASCADE remove chunks, instead of
    # the ORM trying to null out range_chunks.job_id (which is NOT NULL).
    chunks: Mapped[list[RangeChunk]] = relationship(back_populates="job", passive_deletes=True)


class JobWorker(Base):
    __tablename__ = "job_workers"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"), primary_key=True)


class JobTarget(Base):
    """One input of a job: a numeric range, a single id (start==end), or a URL.

    A job may have several targets. Numeric targets drive the range scan; ``url``
    is reserved for link-based sources such as YouTube (prepared, not yet wired).
    """

    __tablename__ = "job_targets"

    id: Mapped[int] = _pk()
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    range_start: Mapped[int | None] = mapped_column(BigInteger)
    range_end: Mapped[int | None] = mapped_column(BigInteger)  # NULL + no url => open-ended
    url: Mapped[str | None] = mapped_column(String(1000))


class RangeChunk(Base):
    __tablename__ = "range_chunks"
    __table_args__ = (UniqueConstraint("job_id", "range_start", "range_end", name="uq_chunk_range"),)

    id: Mapped[int] = _pk()
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    range_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    range_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    next_id: Mapped[int | None] = mapped_column(BigInteger)  # checkpoint within the chunk
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    leased_by: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    job: Mapped[Job] = relationship(back_populates="chunks")


# --------------------------------------------------------------------------- #
# Items / media / downloads / files
# --------------------------------------------------------------------------- #
class Item(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("job_id", "external_id", name="uq_item_external"),)

    id: Mapped[int] = _pk()
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class MediaMetadata(Base):
    __tablename__ = "media_metadata"

    id: Mapped[int] = _pk()
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    download_url: Mapped[str | None] = mapped_column(String(1000))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str | None] = mapped_column(String(100))  # e.g. kinescope, youtube
    discovered_at: Mapped[datetime] = _created()
    raw: Mapped[dict | None] = mapped_column(JSON)


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[int] = _pk()
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    bytes_downloaded: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class FileRecord(Base):
    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("download_id", "storage_path", name="uq_file_path"),)

    id: Mapped[int] = _pk()
    download_id: Mapped[int] = mapped_column(ForeignKey("downloads.id", ondelete="CASCADE"), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    worker_name: Mapped[str | None] = mapped_column(String(150))
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- #
# Events / audit
# --------------------------------------------------------------------------- #
class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = _pk()
    ts: Mapped[datetime] = _created()
    level: Mapped[str] = mapped_column(String(20), default="info", nullable=False)
    component: Mapped[str | None] = mapped_column(String(50))
    job_id: Mapped[int | None] = mapped_column(Integer)
    worker_id: Mapped[int | None] = mapped_column(Integer)
    chunk_id: Mapped[int | None] = mapped_column(Integer)
    item_id: Mapped[int | None] = mapped_column(Integer)
    operation: Mapped[str | None] = mapped_column(String(100))
    result: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))
    message: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = _pk()
    ts: Mapped[datetime] = _created()
    user_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[dict | None] = mapped_column(JSON)


# --------------------------------------------------------------------------- #
# Library groups (labels): a video may belong to several groups at once.
# --------------------------------------------------------------------------- #
class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="user", nullable=False)  # user | favorites
    created_at: Mapped[datetime] = _created()


class ItemGroup(Base):
    __tablename__ = "item_groups"

    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
