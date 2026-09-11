"""Pydantic API contracts shared between the coordinator, agents and web.

These are the wire types: what crosses the HTTP boundary. Database models live
only in the coordinator; everyone talks to the coordinator through these.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from vidhive_common.enums import ChunkStatus, ItemStatus, JobState, WorkerState


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class JobCreate(BaseModel):
    name: str | None = None
    # Optional textual range ("[A,B]", "A", "[A,]", ""); overrides range_start/end.
    range_spec: str | None = None
    range_start: int = Field(default=0, ge=0)
    range_end: int | None = Field(default=None, ge=0)  # None => open-ended
    chunk_size: int = Field(default=5000, ge=1)
    request_timeout_seconds: int = Field(default=20, ge=1)
    max_retries: int = Field(default=4, ge=0)
    global_rate_limit_rps: int | None = Field(default=None, ge=1)
    stop_on_consecutive_missing: int | None = Field(default=None, ge=1)


class JobOut(BaseModel):
    id: int
    name: str | None
    range_start: int | None
    range_end: int | None
    state: JobState
    chunk_size: int
    request_timeout_seconds: int
    max_retries: int
    global_rate_limit_rps: int | None
    stop_on_consecutive_missing: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Workers
# --------------------------------------------------------------------------- #
class WorkerRegister(BaseModel):
    name: str
    host: str | None = None
    # Base URL the coordinator/web use to reach this agent (files, control).
    agent_url: str | None = None
    agent_version: str | None = None
    threads: int | None = Field(default=None, ge=1)
    storage_path: str | None = None


class WorkerOut(BaseModel):
    id: int
    name: str
    host: str | None
    agent_url: str | None
    state: WorkerState
    agent_version: str | None
    threads: int | None
    storage_path: str | None
    last_heartbeat_at: datetime | None
    registered_at: datetime

    model_config = {"from_attributes": True}


class Heartbeat(BaseModel):
    cpu_percent: float | None = None
    ram_used_mb: float | None = None
    ram_total_mb: float | None = None
    disk_free_gb: float | None = None
    active_checks: int = 0
    active_downloads: int = 0


# --------------------------------------------------------------------------- #
# Lease / progress (block protocol)
# --------------------------------------------------------------------------- #
class LeaseRequest(BaseModel):
    worker_id: int
    lease_seconds: int = Field(default=120, ge=10)


class ChunkLease(BaseModel):
    chunk_id: int
    job_id: int
    range_start: int
    range_end: int
    next_id: int
    lease_expires_at: datetime

    model_config = {"from_attributes": True}


class ItemResult(BaseModel):
    external_id: int
    status: ItemStatus
    title: str | None = None
    download_url: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    # Local path on the reporting agent once the file has been downloaded.
    storage_path: str | None = None
    checksum: str | None = None
    error: str | None = None


class ProgressReport(BaseModel):
    chunk_id: int
    next_id: int
    items: list[ItemResult] = []


class ChunkComplete(BaseModel):
    chunk_id: int
    status: ChunkStatus = ChunkStatus.COMPLETED


class Ack(BaseModel):
    ok: bool = True


# --------------------------------------------------------------------------- #
# Read models for the API
# --------------------------------------------------------------------------- #
class ItemOut(BaseModel):
    id: int
    external_id: int
    status: ItemStatus
    attempts: int
    title: str | None = None
    size_bytes: int | None = None
    worker: str | None = None
    last_error: str | None = None
    checked_at: datetime | None = None


class EventOut(BaseModel):
    id: int
    ts: datetime
    level: str
    component: str | None
    operation: str | None
    result: str | None
    message: str | None
    job_id: int | None
    worker_id: int | None
    chunk_id: int | None
    item_id: int | None

    model_config = {"from_attributes": True}
