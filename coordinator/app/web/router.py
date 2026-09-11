"""Server-rendered web UI, served by the coordinator itself.

Because the UI lives inside the coordinator, the dashboard reads the database
directly through the session — no HTTP self-call. HTMX (loaded in the template)
will drive live updates later; the logic stays in Python.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job, Worker
from app.db.session import get_session

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(tags=["web"])


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    jobs = (await session.execute(select(Job).order_by(Job.id.desc()))).scalars().all()
    workers = (await session.execute(select(Worker).order_by(Worker.name))).scalars().all()
    return templates.TemplateResponse(
        request, "index.html", {"jobs": list(jobs), "workers": list(workers)}
    )
