"""Temporary fallback from the VPN route to the direct route.

When the agent's internet traffic goes through a VPN gateway container and a check or a
download fails (the VPN path is lossy, or a site refuses the VPN's address), the agent
switches its own default route to the direct gateway, retries, and goes back to the VPN
when the chunk it is working on is finished ("until the task is done").

The route change is container-wide, so connections of other downloads in the same chunk
that were opened through the VPN break and are retried by the same rule; that is why the
decision is kept per chunk and not per file.

A flag file tells the supervisor not to push the route back to the VPN meanwhile. It also
expires on its own (the supervisor ignores a flag older than 30 minutes), so a crash in the
middle of a fallback cannot leave the agent off the VPN for ever.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from typing import Callable

log = logging.getLogger("vidhive.egress")

STATE_DIR = os.environ.get("VIDHIVE_EGRESS_DIR", "/run/vidhive-egress")


def _run_ip(args: list[str]) -> str:
    out = subprocess.run(["ip", *args], capture_output=True, text=True, timeout=10, check=False)
    return out.stdout


class EgressManager:
    def __init__(self, run: Callable[[list[str]], str] | None = None, state_dir: str = STATE_DIR) -> None:
        self._run = run or _run_ip
        self.dir = state_dir
        self._lock = asyncio.Lock()
        self.generation = 0            # bumped on every route change
        self._saved_gw: str | None = None

    # -- helpers ----------------------------------------------------------- #
    def _direct_gw(self) -> str | None:
        try:
            with open(os.path.join(self.dir, "direct_gw"), encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None

    @property
    def _flag(self) -> str:
        return os.path.join(self.dir, "direct")

    async def current_gw(self) -> str | None:
        out = await asyncio.to_thread(self._run, ["route", "show", "default"])
        m = re.search(r"default via (\S+)", out)
        return m.group(1) if m else None

    async def _set(self, gw: str) -> bool:
        await asyncio.to_thread(self._run, ["route", "replace", "default", "via", gw])
        return await self.current_gw() == gw

    # -- public ------------------------------------------------------------ #
    @property
    def fallen_back(self) -> bool:
        return self._saved_gw is not None

    async def fall_back(self, seen_generation: int, reason: str = "VPN route failed") -> bool:
        """Switch to the direct route. True if a retry is worthwhile: the route was changed now
        or since the failed attempt began; False if there is no VPN route to leave."""
        async with self._lock:
            if self.generation != seen_generation:
                return True                       # somebody switched while this attempt ran
            direct, cur = self._direct_gw(), await self.current_gw()
            if not direct or not cur or cur == direct:
                return False                      # not using a VPN route: nothing to fall back from
            if not await self._set(direct):
                return False
            self._saved_gw = cur
            self.generation += 1
            try:
                os.makedirs(self.dir, exist_ok=True)
                with open(self._flag, "w", encoding="utf-8") as f:
                    f.write("1")
            except OSError:
                pass
            log.warning("%s: using the direct route until this chunk is done", reason)
            return True

    async def restore(self) -> None:
        """Go back to the VPN (called when the chunk is finished or abandoned)."""
        async with self._lock:
            if self._saved_gw is None:
                return
            gw, self._saved_gw = self._saved_gw, None
            await self._set(gw)
            self.generation += 1
            try:
                os.remove(self._flag)
            except OSError:
                pass
            log.info("back on the VPN route")
