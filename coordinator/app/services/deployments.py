"""In-memory registry of running deploy/teardown jobs and their live output.

State lives in the coordinator process only — a deployment is transient and need
not survive a restart. The web polls a job's log while it runs.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field


@dataclass
class Deployment:
    id: str
    kind: str  # "deploy" | "teardown"
    target: str  # worker name / host, for display
    status: str = "running"  # running | success | error
    lines: list[str] = field(default_factory=list)


_deployments: dict[str, Deployment] = {}


def new_deployment(kind: str, target: str) -> Deployment:
    dep = Deployment(id=secrets.token_hex(8), kind=kind, target=target)
    _deployments[dep.id] = dep
    return dep


def append(dep_id: str, line: str) -> None:
    dep = _deployments.get(dep_id)
    if dep is not None:
        dep.lines.append(line)


def finish(dep_id: str, ok: bool) -> None:
    dep = _deployments.get(dep_id)
    if dep is not None:
        dep.status = "success" if ok else "error"


def get(dep_id: str) -> Deployment | None:
    return _deployments.get(dep_id)
