"""Deploy or tear down a Vidhive agent on a remote Linux host over SSH.

The coordinator connects with a one-time SSH key (never stored), pipes one of the
``deploy/agent-*.sh`` scripts to the host with the configuration in the
environment, and streams the output back line by line. Every configuration value
is shell-quoted before it reaches the remote shell.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import asyncssh

OnLine = Callable[[str], None]


def _deploy_dir() -> Path:
    candidates = [
        os.environ.get("VIDHIVE_DEPLOY_DIR"),
        "/srv/deploy",
        str(Path(__file__).resolve().parents[3] / "deploy"),
    ]
    for cand in candidates:
        if cand and (Path(cand) / "agent-bootstrap.sh").exists():
            return Path(cand)
    return Path(__file__).resolve().parents[3] / "deploy"


def read_script(name: str) -> str:
    return (_deploy_dir() / name).read_text(encoding="utf-8")


@dataclass
class DeployParams:
    ssh_host: str
    ssh_port: int
    ssh_user: str
    ssh_key: str = ""  # PEM private key text — used once, never stored
    ssh_password: str = ""  # SSH password (VPS case) — used once, never stored
    env: dict[str, str] = field(default_factory=dict)  # VIDHIVE_* + INSTALL_DIR/etc.


def build_env_prefix(env: dict[str, str]) -> str:
    """`KEY=quoted KEY2=quoted …` — values shell-quoted so nothing can inject."""
    return " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())


async def run_script(params: DeployParams, script_text: str, on_line: OnLine) -> int:
    prefix = build_env_prefix(params.env)
    command = f"{prefix} bash -s" if prefix else "bash -s"
    connect_kwargs: dict = {
        "port": params.ssh_port,
        "username": params.ssh_user,
        "known_hosts": None,  # first contact with a fresh host (trusted lab network)
    }
    if params.ssh_password:
        connect_kwargs["password"] = params.ssh_password
    else:
        connect_kwargs["client_keys"] = [asyncssh.import_private_key(params.ssh_key)]
    async with asyncssh.connect(params.ssh_host, **connect_kwargs) as conn:
        proc = await conn.create_process(command, stdin=asyncssh.PIPE, stderr=asyncssh.STDOUT)
        proc.stdin.write(script_text)
        proc.stdin.write_eof()
        async for line in proc.stdout:
            on_line(line.rstrip("\n"))
        result = await proc.wait()
        return result.exit_status or 0


async def deploy(params: DeployParams, on_line: OnLine) -> int:
    return await run_script(params, read_script("agent-bootstrap.sh"), on_line)


async def teardown(params: DeployParams, on_line: OnLine) -> int:
    return await run_script(params, read_script("agent-teardown.sh"), on_line)
