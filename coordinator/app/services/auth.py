"""Authentication & authorization: role seeding, bootstrap admin, role lookup,
session-backed current user, and the role/agent-token guards used by routes."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.config import get_settings
from app.db.models import RoleModel, User, UserRole
from app.db.session import get_sessionmaker
from app.services.security import hash_password

log = logging.getLogger("vidhive.auth")

ROLE_RANK = {"viewer": 1, "operator": 2, "administrator": 3}


async def seed_roles(session: AsyncSession) -> None:
    existing = set((await session.execute(select(RoleModel.name))).scalars().all())
    for name in ROLE_RANK:
        if name not in existing:
            session.add(RoleModel(name=name))
    await session.flush()


async def ensure_bootstrap_admin(
    session: AsyncSession, admin_user: str, admin_password: str
) -> None:
    count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if count:
        return
    if not admin_user or not admin_password:
        log.warning(
            "no users and VIDHIVE_ADMIN_USER/PASSWORD unset — login impossible until an admin exists"
        )
        return
    user = User(username=admin_user, password_hash=hash_password(admin_password))
    session.add(user)
    await session.flush()
    role = (
        await session.execute(select(RoleModel).where(RoleModel.name == "administrator"))
    ).scalar_one()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    await session.flush()
    log.info("bootstrap administrator '%s' created", admin_user)


async def role_of(session: AsyncSession, user_id: int) -> str | None:
    return (
        await session.execute(
            select(RoleModel.name)
            .join(UserRole, UserRole.role_id == RoleModel.id)
            .where(UserRole.user_id == user_id)
        )
    ).scalars().first()


class AccessError(Exception):
    """Raised by role guards; the app's handler renders it per request type."""

    def __init__(self, status_code: int, authenticated: bool = False) -> None:
        self.status_code = status_code
        self.authenticated = authenticated


async def load_current_user(request: Request) -> None:
    """Populate request.state.user / request.state.role from the session cookie."""
    request.state.user = None
    request.state.role = None
    try:
        uid = request.session.get("user_id")
    except (AssertionError, KeyError):
        uid = None
    if uid is None:
        return
    async with get_sessionmaker()() as session:
        user = await session.get(User, uid)
        if user is None:
            return
        request.state.user = user
        request.state.role = await role_of(session, user.id)


def require_role(min_role: str):
    minimum = ROLE_RANK[min_role]

    async def _dep(request: Request) -> User:
        user = getattr(request.state, "user", None)
        if user is None:
            raise AccessError(401, authenticated=False)
        role = getattr(request.state, "role", None)
        if ROLE_RANK.get(role or "", 0) < minimum:
            raise AccessError(403, authenticated=True)
        return user

    return _dep


async def require_agent_token(request: Request) -> None:
    token = get_settings().agent_token
    if not token:
        return  # feature disabled until a token is configured
    if request.headers.get("X-Agent-Token") != token:
        raise AccessError(401, authenticated=False)
