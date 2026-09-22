"""Personal timeline notes (markers) on videos.

Notes belong to the logged-in user and attach to a video's identity
(``external_id``), so they survive re-downloads and are private per user.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Item, MediaMetadata, Note
from app.db.session import get_session
from app.services.auth import require_role
from vidhive_common.enums import ItemStatus

router = APIRouter(prefix="/api/notes", tags=["notes"])

_COLOR_RE = re.compile(r"^#?[0-9A-Za-z]{3,12}$")
_DEFAULT_COLOR = "#7c9cff"


def _clean_color(color: str) -> str:
    color = (color or "").strip()
    return color if _COLOR_RE.match(color) else _DEFAULT_COLOR


def _uid(request: Request) -> int:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user.id


class NoteCreate(BaseModel):
    external_id: int
    t_seconds: float = Field(ge=0)
    label: str = ""
    color: str = _DEFAULT_COLOR


class NoteUpdate(BaseModel):
    label: str | None = None
    color: str | None = None
    t_seconds: float | None = Field(default=None, ge=0)


class NoteOut(BaseModel):
    id: int
    external_id: int
    t_seconds: float
    label: str
    color: str


class NoteAllOut(NoteOut):
    title: str
    item_id: int | None  # latest playable item for this video, if any


async def _latest_item_for(session: AsyncSession, external_id: int) -> Item | None:
    return (
        await session.execute(
            select(Item)
            .where(Item.external_id == external_id)
            .where(Item.status == ItemStatus.COMPLETED.value)
            .order_by(Item.id.desc())
            .limit(1)
        )
    ).scalars().first()


@router.get("", response_model=list[NoteOut], dependencies=[Depends(require_role("viewer"))])
async def list_notes(
    request: Request, external_id: int, session: AsyncSession = Depends(get_session)
) -> list[NoteOut]:
    rows = (
        await session.execute(
            select(Note)
            .where(Note.user_id == _uid(request))
            .where(Note.external_id == external_id)
            .order_by(Note.t_seconds)
        )
    ).scalars().all()
    return [
        NoteOut(id=n.id, external_id=n.external_id, t_seconds=n.t_seconds, label=n.label, color=n.color)
        for n in rows
    ]


@router.get("/all", response_model=list[NoteAllOut], dependencies=[Depends(require_role("viewer"))])
async def list_all_notes(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[NoteAllOut]:
    rows = (
        await session.execute(
            select(Note).where(Note.user_id == _uid(request)).order_by(Note.updated_at.desc())
        )
    ).scalars().all()
    out: list[NoteAllOut] = []
    for n in rows:
        item = await _latest_item_for(session, n.external_id)
        title = None
        if item is not None:
            meta = (
                await session.execute(
                    select(MediaMetadata).where(MediaMetadata.item_id == item.id)
                )
            ).scalars().first()
            title = meta.title if meta else None
        out.append(
            NoteAllOut(
                id=n.id, external_id=n.external_id, t_seconds=n.t_seconds, label=n.label,
                color=n.color, title=title or f"ID {n.external_id}",
                item_id=item.id if item else None,
            )
        )
    return out


@router.post("", response_model=NoteOut, dependencies=[Depends(require_role("viewer"))])
async def create_note(
    request: Request, payload: NoteCreate, session: AsyncSession = Depends(get_session)
) -> NoteOut:
    note = Note(
        user_id=_uid(request),
        external_id=payload.external_id,
        t_seconds=payload.t_seconds,
        label=payload.label.strip()[:500],
        color=_clean_color(payload.color),
    )
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return NoteOut(id=note.id, external_id=note.external_id, t_seconds=note.t_seconds,
                   label=note.label, color=note.color)


async def _own_note(session: AsyncSession, note_id: int, user_id: int) -> Note:
    note = await session.get(Note, note_id)
    if note is None or note.user_id != user_id:
        raise HTTPException(status_code=404, detail="note not found")
    return note


@router.patch("/{note_id}", response_model=NoteOut, dependencies=[Depends(require_role("viewer"))])
async def update_note(
    request: Request, note_id: int, payload: NoteUpdate,
    session: AsyncSession = Depends(get_session),
) -> NoteOut:
    note = await _own_note(session, note_id, _uid(request))
    if payload.label is not None:
        note.label = payload.label.strip()[:500]
    if payload.color is not None:
        note.color = _clean_color(payload.color)
    if payload.t_seconds is not None:
        note.t_seconds = payload.t_seconds
    await session.commit()
    await session.refresh(note)
    return NoteOut(id=note.id, external_id=note.external_id, t_seconds=note.t_seconds,
                   label=note.label, color=note.color)


@router.delete("/{note_id}", dependencies=[Depends(require_role("viewer"))])
async def delete_note(
    request: Request, note_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    note = await _own_note(session, note_id, _uid(request))
    await session.delete(note)
    await session.commit()
    return {"deleted": note_id}
