"""Controllable mock of the target video service (spec sections 20 and 23).

Lets the whole pipeline be exercised safely and repeatably — found + download,
forbidden, rate-limited, server errors — without touching a real external site.

Response is chosen deterministically from the identifier:

    id % 13 == 0  -> 500 Internal Server Error   (retry path)
    id % 11 == 0  -> 429 + Retry-After: 1        (rate-limit path)
    id %  7 == 0  -> 403 Forbidden               (no bypass expected)
    id %  5 == 0  -> 200 video/mp4 + payload     (found -> downloadable)
    otherwise     -> 404 Not Found               (the common case)

`?delay=<seconds>` slows any response, for timeout testing.
"""

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

# A real, playable MP4 is generated at image build time so the whole pipeline —
# including playback in the browser — can be verified end to end. If it is
# missing, fall back to a synthetic payload (downloadable, but not decodable).
SAMPLE = Path("/srv/sample.mp4")
PAYLOAD_KB = int(os.environ.get("MOCK_PAYLOAD_KB", "256"))

if SAMPLE.exists():
    VIDEO_BYTES = SAMPLE.read_bytes()
    PLAYABLE = True
else:
    VIDEO_BYTES = bytes((i * 7 + 13) % 251 for i in range(PAYLOAD_KB * 1024))
    PLAYABLE = False

app = FastAPI(title="Vidhive Mock Target", version="1.0.0")


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "mock",
        "payload_bytes": len(VIDEO_BYTES),
        "playable": PLAYABLE,
    }


@app.get("/{external_id}")
async def target(external_id: int, delay: float = 0.0):
    if delay > 0:
        await asyncio.sleep(min(delay, 30.0))

    if external_id % 13 == 0:
        return JSONResponse({"error": "internal"}, status_code=500)
    if external_id % 11 == 0:
        return JSONResponse({"error": "slow down"}, status_code=429, headers={"Retry-After": "1"})
    if external_id % 7 == 0:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if external_id % 5 == 0:
        return Response(
            VIDEO_BYTES,
            media_type="video/mp4",
            headers={"Content-Disposition": f'inline; filename="video-{external_id}.mp4"'},
        )
    return JSONResponse({"error": "not found"}, status_code=404)
