"""Authentication & authorization: role seeding, bootstrap admin, role lookup,
session-backed current user, and the role/agent-token guards used by routes."""

from __future__ import annotations

import hmac
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.config import get_settings
from app.db.models import RoleModel, User, UserRole
from app.db.session import get_sessionmaker
from app.services import netinfo
from app.services.security import hash_password

log = logging.getLogger("vidhive.auth")

ROLE_RANK = {"viewer": 1, "operator": 2, "administrator": 3}

PUBLIC_OWNER_ONLY = (
    "Через публичную ссылку этот раздел открывает только главный администратор; "
    "остальные администраторы работают из домашней сети (или по Tailscale)."
)

PUBLIC_DENIED = (
    "Этот раздел доступен только из домашней сети (или по Tailscale), "
    "а не через публичную ссылку."
)


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

    def __init__(
        self, status_code: int, authenticated: bool = False, detail: str | None = None
    ) -> None:
        self.status_code = status_code
        self.authenticated = authenticated
        self.detail = detail  # shown to the user instead of the generic message


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


async def _owner_id() -> int | None:
    """The owner is the first-registered user (smallest id) — protected from demotion."""
    async with get_sessionmaker()() as session:
        return (await session.execute(select(func.min(User.id)))).scalar_one_or_none()


def require_role(min_role: str):
    minimum = ROLE_RANK[min_role]

    async def _dep(request: Request) -> User:
        # Administrator functions (servers, SSH deploy, users) over a public link are
        # limited by VIDHIVE_PUBLIC_ADMIN: nobody (default), only the owner, or any admin.
        if (
            min_role == "administrator"
            and get_settings().restrict_public
            and netinfo.is_public(request)
        ):
            mode = (get_settings().public_admin or "none").strip().lower()
            if mode == "owner":
                user = getattr(request.state, "user", None)
                if user is None:
                    raise AccessError(401, authenticated=False)  # sign in first
                if user.id != await _owner_id():
                    raise AccessError(403, authenticated=True, detail=PUBLIC_OWNER_ONLY)
            elif mode != "any":
                raise AccessError(403, authenticated=False, detail=PUBLIC_DENIED)
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
    given = request.headers.get("X-Agent-Token", "")
    # Constant-time comparison so the token can't be guessed byte by byte via timing.
    if not hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8")):
        raise AccessError(401, authenticated=False)
