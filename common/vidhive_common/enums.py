"""Status enumerations shared across coordinator, agent and web.

Stored in the database as plain strings so the schema stays portable and new
values can be added without a migration of an enum type.
"""

from enum import Enum


class JobState(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class ChunkStatus(str, Enum):
    PENDING = "pending"       # in the queue, not leased
    LEASED = "leased"         # rented by a worker
    COMPLETED = "completed"
    FAILED = "failed"


class ItemStatus(str, Enum):
    PENDING = "pending"
    CHECKING = "checking"
    NOT_FOUND = "not_found"
    FOUND = "found"
    DOWNLOAD_QUEUED = "download_queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerState(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class DownloadStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"


class Role(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMINISTRATOR = "administrator"
