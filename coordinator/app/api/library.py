"""Library management: groups (labels), favorites, and full deletion.

Groups are many-to-many labels over completed items. "Все" is virtual (no
filter); "Избранное" is a single system group. Deleting a video removes the
file from the owning agent and then the database rows (cascade).
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, Item, ItemGroup, Worker
from app.db.session import get_session
from app.services.events import log_event
from vidhive_common.enums import ItemStatus
from vidhive_common.schemas import Ack, GroupCreate, GroupOut, LibraryAction

log = logging.getLogger("vidhive.library")
router = APIRouter(prefix="/api", tags=["library"])

FAVORITES_NAME = "Избранное"


async def get_or_create_favorites(session: AsyncSession) -> Group:
    fav = (
        await session.execute(select(Group).where(Group.kind == "favorites"))
    ).scalars().first()
    if fav is None:
        fav = Group(name=FAVORITES_NAME, kind="favorites")
        session.add(fav)
        await session.flush()
    return fav


async def _count(session: AsyncSession, group_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(ItemGroup).where(ItemGroup.group_id == group_id)
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# Groups
# --------------------------------------------------------------------------- #
@router.get("/groups", response_model=list[GroupOut])
async def list_groups(session: AsyncSession = Depends(get_session)) -> list[GroupOut]:
    await get_or_create_favorites(session)
    await session.commit()
    groups = (await session.execute(select(Group).order_by(Group.kind.desc(), Group.id))).scalars().all()
    out = []
    for g in groups:
        out.append(GroupOut(id=g.id, name=g.name, kind=g.kind, count=await _count(session, g.id)))
    return out


@router.post("/groups", response_model=GroupOut)
async def create_group(payload: GroupCreate, session: AsyncSession = Depends(get_session)) -> GroupOut:
    group = Group(name=payload.name.strip(), kind="user")
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return GroupOut(id=group.id, name=group.name, kind=group.kind, count=0)


@router.patch("/groups/{group_id}", response_model=GroupOut)
async def rename_group(
    group_id: int, payload: GroupCreate, session: AsyncSession = Depends(get_session)
) -> GroupOut:
    group = await session.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    group.name = payload.name.strip()
    await session.commit()
    return GroupOut(id=group.id, name=group.name, kind=group.kind, count=await _count(session, group_id))


@router.delete("/groups/{group_id}", response_model=Ack)
async def delete_group(group_id: int, session: AsyncSession = Depends(get_session)) -> Ack:
    group = await session.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    if group.kind != "user":
        raise HTTPException(status_code=409, detail="системную группу удалить нельзя")
    await session.delete(group)  # cascades item_groups; videos are untouched
    await session.commit()
    return Ack()


# --------------------------------------------------------------------------- #
# Membership actions
# --------------------------------------------------------------------------- #
async def _add_memberships(session: AsyncSession, item_ids: list[int], group_id: int) -> None:
    existing = set(
        (
            await session.execute(
                select(ItemGroup.item_id)
                .where(ItemGroup.group_id == group_id)
                .where(ItemGroup.item_id.in_(item_ids))
            )
        ).scalars().all()
    )
    for iid in item_ids:
        if iid not in existing:
            session.add(ItemGroup(item_id=iid, group_id=group_id))


@router.post("/library/assign", response_model=Ack)
async def assign(payload: LibraryAction, session: AsyncSession = Depends(get_session)) -> Ack:
    if payload.group_id is None:
        raise HTTPException(status_code=422, detail="group_id required")
    if await session.get(Group, payload.group_id) is None:
        raise HTTPException(status_code=404, detail="group not found")
    await _add_memberships(session, payload.item_ids, payload.group_id)
    await session.commit()
    return Ack()


@router.post("/library/unassign", response_model=Ack)
async def unassign(payload: LibraryAction, session: AsyncSession = Depends(get_session)) -> Ack:
    if payload.group_id is None:
        raise HTTPException(status_code=422, detail="group_id required")
    await session.execute(
        delete(ItemGroup)
        .where(ItemGroup.group_id == payload.group_id)
        .where(ItemGroup.item_id.in_(payload.item_ids))
    )
    await session.commit()
    return Ack()


@router.post("/library/favorite", response_model=Ack)
async def favorite(payload: LibraryAction, session: AsyncSession = Depends(get_session)) -> Ack:
    fav = await get_or_create_favorites(session)
    if payload.on:
        await _add_memberships(session, payload.item_ids, fav.id)
    else:
        await session.execute(
            delete(ItemGroup)
            .where(ItemGroup.group_id == fav.id)
            .where(ItemGroup.item_id.in_(payload.item_ids))
        )
    await session.commit()
    return Ack()


# --------------------------------------------------------------------------- #
# Full deletion (file on the agent + database rows)
# --------------------------------------------------------------------------- #
@router.post("/library/delete", response_model=Ack)
async def delete_videos(payload: LibraryAction, session: AsyncSession = Depends(get_session)) -> Ack:
    items = (
        await session.execute(select(Item).where(Item.id.in_(payload.item_ids)))
    ).scalars().all()

    async with httpx.AsyncClient(timeout=15.0) as client:
        for item in items:
            worker = await session.get(Worker, item.worker_id) if item.worker_id else None
            if worker and worker.agent_url:
                url = f"{worker.agent_url.rstrip('/')}/files/{item.external_id}"
                try:
                    await client.delete(url)
                except httpx.HTTPError as exc:
                    log.warning("agent file delete failed for %s: %s", item.external_id, exc)
            log_event(
                session,
                component="library",
                operation="delete",
                result="removed",
                item_id=item.id,
                worker_id=item.worker_id,
                message=f"deleted external_id={item.external_id}",
            )
            await session.delete(item)  # cascades metadata/downloads/files/item_groups

    await session.commit()
    return Ack()
