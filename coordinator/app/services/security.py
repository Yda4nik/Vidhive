"""Password hashing with the standard library (pbkdf2_hmac). No extra deps."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
from concurrent.futures import ThreadPoolExecutor

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 240_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Async wrappers
#
# PBKDF2 is deliberately slow (~100 ms). Run inline in an async handler it freezes
# the whole event loop for that long — SSE streams, agent heartbeats and every
# other request stall, and a stream of bad logins becomes a cheap denial of
# service. hashlib releases the GIL, so a small dedicated thread pool gives real
# parallelism; keeping it small (and separate from the default executor that
# yt-dlp probes use) bounds how much CPU a login flood can take.
# --------------------------------------------------------------------------- #
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pbkdf2")

# A valid hash of a random value: verifying against it costs the same as a real
# user's hash, so "unknown user" and "wrong password" take the same time.
DUMMY_HASH = hash_password(base64.b64encode(os.urandom(16)).decode())


async def ahash_password(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_pool, hash_password, password)


async def averify_password(password: str, stored: str) -> bool:
    return await asyncio.get_running_loop().run_in_executor(_pool, verify_password, password, stored)
