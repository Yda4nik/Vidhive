# Access Control (accounts, login, roles) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add username/password login and three access roles (viewer/operator/administrator) to the coordinator; protect the web UI and management API behind a session, and the machine endpoints behind an agent token.

**Architecture:** Cookie sessions via Starlette `SessionMiddleware` (secret from env). Passwords hashed with stdlib `pbkdf2_hmac`. A middleware loads the current user onto `request.state` from the session cookie; a `require_role(min)` FastAPI dependency guards routes (401→login redirect for pages, 401/403 JSON for `/api`). Machine worker endpoints check an `X-Agent-Token` header when `VIDHIVE_AGENT_TOKEN` is set. First admin is created at startup from env when the users table is empty.

**Tech Stack:** FastAPI, Starlette SessionMiddleware, SQLAlchemy 2 async, Jinja2, pytest + Starlette TestClient. No new third-party dependencies.

**Spec:** `docs/specs/2026-09-14-access-control-design.md`

## Global Constraints

- No new third-party dependencies (password hashing uses stdlib `hashlib`/`hmac`).
- Secrets only from env (`VIDHIVE_SECRET_KEY`, `VIDHIVE_AGENT_TOKEN`, `VIDHIVE_ADMIN_USER`, `VIDHIVE_ADMIN_PASSWORD`); never in code, logs, URLs.
- Role ranks fixed: `viewer=1, operator=2, administrator=3`; `require_role` passes when `rank(user) >= rank(min)`.
- Tables `users/roles/user_roles` already exist (migration `4f9b48b05fc8_initial_schema`); do NOT add columns or a migration.
- Run coordinator tests with: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/ -p no:cacheprovider` from `coordinator/` (venv already set up this session). Keep every commit green.
- Machine endpoints requiring the agent token: `POST /api/workers/{register,lease,heartbeat,progress,complete,fail}` only. Reads of workers/jobs/library are `viewer`.

---

### Task 1: Password hashing service

**Files:**
- Create: `coordinator/app/services/security.py`
- Test: `coordinator/tests/test_security.py`

**Interfaces:**
- Produces: `hash_password(password: str) -> str`, `verify_password(password: str, stored: str) -> bool`. Stored format: `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`.

- [ ] **Step 1: Write the failing test**

```python
# coordinator/tests/test_security.py
from app.services.security import hash_password, verify_password


def test_hash_verify_roundtrip():
    h = hash_password("s3cret")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret", h) is True
    assert verify_password("wrong", h) is False


def test_salt_makes_hashes_differ():
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_garbage():
    assert verify_password("x", "not-a-valid-hash") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `coordinator/`): `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_security.py -q -p no:cacheprovider`
Expected: FAIL (ModuleNotFoundError: app.services.security).

- [ ] **Step 3: Write minimal implementation**

```python
# coordinator/app/services/security.py
"""Password hashing with the standard library (pbkdf2_hmac). No extra deps."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_security.py -q -p no:cacheprovider`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add coordinator/app/services/security.py coordinator/tests/test_security.py
git commit -m "Add pbkdf2 password hashing"
```

---

### Task 2: Config + role/admin bootstrap

**Files:**
- Modify: `coordinator/app/core/config.py` (add fields)
- Create: `coordinator/app/services/auth.py` (seed + bootstrap + role lookup)
- Modify: `coordinator/app/main.py` (run bootstrap in lifespan)
- Test: `coordinator/tests/test_auth_bootstrap.py`

**Interfaces:**
- Consumes: `hash_password` (Task 1).
- Produces: `ROLE_RANK: dict[str,int]`; `async seed_roles(session) -> None`; `async ensure_bootstrap_admin(session, admin_user: str, admin_password: str) -> None`; `async role_of(session, user_id: int) -> str | None`.

- [ ] **Step 1: Write the failing test**

```python
# coordinator/tests/test_auth_bootstrap.py
def test_roles_seeded_and_admin_created(client, raw_sql):
    # The client fixture starts the app (lifespan runs the bootstrap).
    names = {r[0] for r in raw_sql("SELECT name FROM roles")}
    assert {"viewer", "operator", "administrator"} <= names
    admins = raw_sql(
        "SELECT u.username FROM users u "
        "JOIN user_roles ur ON ur.user_id=u.id "
        "JOIN roles r ON r.id=ur.role_id WHERE r.name='administrator'"
    )
    assert ("tester-admin",) in admins


def test_bootstrap_is_idempotent(client, raw_sql):
    # Only one admin even though lifespan ran; a second login attempt does not duplicate.
    n = raw_sql("SELECT COUNT(*) FROM users")[0][0]
    assert n == 1
```

Note: conftest is updated in this task to set admin env + secret and to expose `client` running the lifespan (see Step 3).

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_auth_bootstrap.py -q -p no:cacheprovider`
Expected: FAIL (no roles seeded / no admin).

- [ ] **Step 3: Write minimal implementation**

Add to `coordinator/app/core/config.py` inside `Settings` (after `log_level`):

```python
    # Access control (all from env; empty admin/token = feature effectively off).
    secret_key: str = "dev-insecure-change-me"
    agent_token: str = ""
    admin_user: str = ""
    admin_password: str = ""
```

Create `coordinator/app/services/auth.py`:

```python
"""Authentication & authorization: role seeding, bootstrap admin, role lookup."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RoleModel, User, UserRole
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
```

In `coordinator/app/main.py`, extend the lifespan to bootstrap before yielding:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    async with get_sessionmaker()() as session:
        from app.services import auth
        await auth.seed_roles(session)
        await auth.ensure_bootstrap_admin(session, settings.admin_user, settings.admin_password)
        await session.commit()
    task = asyncio.create_task(_completion_sweeper())
    try:
        yield
    finally:
        task.cancel()
```

Update `coordinator/tests/conftest.py`: set env BEFORE importing the app (near the top, right after `os.environ["VIDHIVE_DATABASE_URL"] = ...`):

```python
os.environ["VIDHIVE_SECRET_KEY"] = "test-secret"
os.environ["VIDHIVE_ADMIN_USER"] = "tester-admin"
os.environ["VIDHIVE_ADMIN_PASSWORD"] = "adminpass"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_auth_bootstrap.py -q -p no:cacheprovider`
Expected: PASS. Also run the full suite — still green (no routes protected yet).

- [ ] **Step 5: Commit**

```bash
git add coordinator/app/core/config.py coordinator/app/services/auth.py coordinator/app/main.py coordinator/tests/conftest.py coordinator/tests/test_auth_bootstrap.py
git commit -m "Seed roles and bootstrap an admin from env at startup"
```

---

### Task 3: Sessions, login/logout, current-user middleware, require_role

**Files:**
- Modify: `coordinator/app/services/auth.py` (add `AccessError`, `require_role`, `require_agent_token`, `load_current_user`)
- Modify: `coordinator/app/main.py` (SessionMiddleware + load-user middleware + AccessError handler)
- Modify: `coordinator/app/web/router.py` (login/logout routes; expose user/role to templates)
- Create: `coordinator/app/web/templates/login.html`
- Modify: `coordinator/app/web/templates/base.html` (header shows user/role/logout)
- Modify: `coordinator/tests/conftest.py` (auto-login admin in `client`; add `anon_client`, `make_user`, `login`)
- Test: `coordinator/tests/test_login.py`

**Interfaces:**
- Consumes: `role_of`, `ROLE_RANK`, `verify_password`.
- Produces:
  - `class AccessError(Exception)` with `.status_code: int` and `.authenticated: bool`.
  - `async load_current_user(request) -> None` — sets `request.state.user: User | None` and `request.state.role: str | None`.
  - `require_role(min_role: str)` -> FastAPI dependency `async (request) -> User` that raises `AccessError`.
  - `async require_agent_token(request) -> None` — raises `AccessError(401)` when token configured and header missing/wrong.

- [ ] **Step 1: Write the failing test**

```python
# coordinator/tests/test_login.py
def test_login_page_renders(anon_client):
    r = anon_client.get("/login")
    assert r.status_code == 200
    assert "Вход" in r.text


def test_login_success_sets_session(anon_client):
    r = anon_client.post("/login", data={"username": "tester-admin", "password": "adminpass"})
    assert r.status_code == 200            # followed redirect to /
    # A protected page (added in later tasks) is not needed here; the session cookie is set.
    assert anon_client.cookies.get("session") is not None


def test_login_failure_shows_error(anon_client):
    r = anon_client.post("/login", data={"username": "tester-admin", "password": "nope"})
    assert r.status_code == 200
    assert "неверный" in r.text.lower()


def test_logout_clears_session(client):
    # `client` is already logged in as admin.
    r = client.post("/logout")
    assert r.status_code == 200            # followed redirect to /login
    assert "Вход" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_login.py -q -p no:cacheprovider`
Expected: FAIL (no `/login`, no `anon_client`).

- [ ] **Step 3: Write minimal implementation**

Append to `coordinator/app/services/auth.py`:

```python
from starlette.requests import Request

from app.core.config import get_settings
from app.db.session import get_sessionmaker


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
```

In `coordinator/app/main.py`, wire middleware and the handler. Add imports at top:

```python
from urllib.parse import quote

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.services.auth import AccessError, load_current_user
```

Inside `create_app`, after the `_broadcast_changes` middleware definition, add (order matters — SessionMiddleware must be added LAST so it is outermost and `request.session` exists for the inner middleware):

```python
    async def _attach_user(request: Request, call_next):
        await load_current_user(request)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=_attach_user)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax")

    @app.exception_handler(AccessError)
    async def _access_error(request: Request, exc: AccessError):
        if request.url.path.startswith("/api"):
            detail = "forbidden" if exc.status_code == 403 else "unauthorized"
            return JSONResponse({"detail": detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
        return HTMLResponse("<h1>403 — недостаточно прав</h1>", status_code=403)
```

In `coordinator/app/web/router.py`, add imports and login/logout routes (near the other routes). Add to the top imports:

```python
from app.services.auth import role_of
from app.services.security import verify_password
from app.db.models import RoleModel, User, UserRole
from sqlalchemy import select
```

Add routes:

```python
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = ""):
    return _page(request, "login.html", {"next": next, "error": error})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    session: AsyncSession = Depends(get_session),
):
    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return _page(request, "login.html", {"next": next, "error": "Неверный логин или пароль"})
    request.session["user_id"] = user.id
    return RedirectResponse(url=next or "/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
```

Create `coordinator/app/web/templates/login.html`:

```html
{% extends "base.html" %}
{% block title %}Vidhive — вход{% endblock %}
{% block content %}
<div class="card" style="max-width:380px; margin:60px auto">
  <h2>Вход</h2>
  {% if error %}<p style="color:var(--bad)">⚠ {{ error }}</p>{% endif %}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{{ next }}">
    <label>Логин</label>
    <input type="text" name="username" autofocus>
    <label>Пароль</label>
    <input type="password" name="password">
    <div style="margin-top:16px">
      <input type="submit" class="btn primary" value="Войти">
    </div>
  </form>
</div>
{% endblock %}
```

In `coordinator/app/web/templates/base.html`, replace the `.head-right` block so it shows the user and a logout button when logged in (uses `request.state`, which TemplateResponse exposes as `request`):

```html
    <div class="head-right" style="gap:10px; align-items:center">
      {% if request.state.user %}
        <span class="muted tiny">{{ request.state.user.username }} · {{ request.state.role }}</span>
        <form method="post" action="/logout" class="inline">
          <input type="submit" class="btn" value="Выйти">
        </form>
      {% endif %}
      <button id="theme-btn" type="button" onclick="toggleTheme()" title="Тема" aria-label="Переключить тему">☀</button>
    </div>
```

Update `coordinator/tests/conftest.py`: after the `with TestClient(app) as c:` block, log the default `client` in as admin, and add an `anon_client` fixture plus helpers. Replace the `client` fixture's `yield c` region:

```python
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        c.post("/login", data={"username": "tester-admin", "password": "adminpass"})
        yield c
    # ... existing async-dispose teardown stays here ...
```

Add new fixtures at the end of conftest:

```python
@pytest.fixture()
def anon_client():
    """A client with the app started but NOT logged in."""
    _unlink_when_free(DB_FILE)
    sync_engine = create_engine(f"sqlite:///{DB_FILE.as_posix()}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    from app.db import session as sess
    sess.get_engine.cache_clear()
    sess.get_sessionmaker.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
    import asyncio, gc
    try:
        asyncio.run(sess.get_engine().dispose())
    except Exception:
        sess.get_engine().sync_engine.dispose()
    gc.collect()


@pytest.fixture()
def make_user(raw_sql):
    """Create a user with a role directly in the DB and return its username."""
    from app.services.security import hash_password

    def _make(username: str, password: str, role: str) -> str:
        raw_sql("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, hash_password(password)))
        uid = raw_sql("SELECT id FROM users WHERE username=?", (username,))[0][0]
        rid = raw_sql("SELECT id FROM roles WHERE name=?", (role,))[0][0]
        raw_sql("INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)", (uid, rid))
        return username

    return _make


def login(client, username: str, password: str):
    return client.post("/login", data={"username": username, "password": password})
```

Note: `anon_client` duplicates the client setup so the two fixtures don't share a TestClient. If conftest already factors setup into a helper, reuse it instead.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_login.py -q -p no:cacheprovider`
Expected: PASS. Full suite still green (routes not yet guarded; `client` is now logged in but that changes nothing until Task 4).

- [ ] **Step 5: Commit**

```bash
git add coordinator/app/services/auth.py coordinator/app/main.py coordinator/app/web/router.py coordinator/app/web/templates/login.html coordinator/app/web/templates/base.html coordinator/tests/conftest.py coordinator/tests/test_login.py
git commit -m "Add login/logout, sessions and role-check primitives"
```

---

### Task 4: Enforce viewer/operator on jobs, add, library, groups

**Files:**
- Modify: `coordinator/app/web/router.py` (add `dependencies=` to job/add/library web routes)
- Modify: `coordinator/app/api/jobs.py` (guard mutations with operator; reads with viewer)
- Modify: `coordinator/app/api/library.py` (guard mutations with operator; reads with viewer)
- Modify: `coordinator/app/web/templates/jobs.html` (hide the add form + controls from viewer)
- Modify: `coordinator/app/web/templates/library.html` (hide delete/fav/group controls from viewer)
- Test: `coordinator/tests/test_roles_jobs.py`

**Interfaces:**
- Consumes: `require_role` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
# coordinator/tests/test_roles_jobs.py
def test_viewer_cannot_create_job_via_api(anon_client, make_user):
    make_user("val", "pw", "viewer")
    anon_client.post("/login", data={"username": "val", "password": "pw"})
    r = anon_client.post("/api/jobs", json={"range_start": 0, "range_end": 9})
    assert r.status_code == 403


def test_viewer_can_read_jobs(anon_client, make_user):
    make_user("val", "pw", "viewer")
    anon_client.post("/login", data={"username": "val", "password": "pw"})
    assert anon_client.get("/api/jobs").status_code == 200
    assert anon_client.get("/").status_code == 200


def test_operator_can_create_job(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    r = anon_client.post("/api/jobs", json={"range_start": 0, "range_end": 9})
    assert r.status_code == 201


def test_anonymous_api_is_unauthorized(anon_client):
    assert anon_client.get("/api/jobs").status_code == 401


def test_anonymous_page_redirects_to_login(anon_client):
    r = anon_client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_viewer_ui_hides_add_form(anon_client, make_user):
    make_user("val", "pw", "viewer")
    anon_client.post("/login", data={"username": "val", "password": "pw"})
    assert 'action="/add"' not in anon_client.get("/jobs").text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_roles_jobs.py -q -p no:cacheprovider`
Expected: FAIL (routes not guarded; viewer create returns 201).

- [ ] **Step 3: Write minimal implementation**

In `coordinator/app/web/router.py`, add the import and apply guards:

```python
from fastapi import Depends
from app.services.auth import require_role
```

Add `dependencies=[Depends(require_role("viewer"))]` to the read pages/fragments/stream: `dashboard`, `dashboard_fragment`, `events_stream`, `jobs_page`, `jobs_fragment`, `library`, `player`, `watch`. Add `dependencies=[Depends(require_role("operator"))]` to `job_action` and `add_submit`. Example:

```python
@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_role("viewer"))])
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    ...

@router.post("/add", dependencies=[Depends(require_role("operator"))])
async def add_submit(...):
    ...
```

In `coordinator/app/api/jobs.py`, add at top:

```python
from fastapi import Depends
from app.services.auth import require_role
```

Guard the router-level reads and mutations. Apply per-route:
- viewer on: `list_jobs`, `get_job`, `list_items`, `list_events`.
- operator on: `create_job`, `start_job`, `pause_job`, `resume_job`, `stop_job`, `retry_failed`, `retry_all`, `delete_job`.

Example:

```python
@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_role("operator"))])
async def create_job(...):
    ...

@router.get("", response_model=list[JobOut], dependencies=[Depends(require_role("viewer"))])
async def list_jobs(...):
    ...
```

In `coordinator/app/api/library.py`, add the same import and put `require_role("operator")` on every mutating route (favorite/assign/delete/groups create/rename/delete) and `require_role("viewer")` on any GET. (Open `library.py`, apply to each `@router.<verb>` decorator.)

In `coordinator/app/web/templates/jobs.html`, wrap the "Добавить загрузку" card and the retry control so only operator+ sees them:

```html
{% if request.state.role in ('operator', 'administrator') %}
<div class="card">
  <h2>Добавить загрузку</h2>
  ... existing form ...
</div>
{% endif %}
```

Wrap the active-jobs retry `<div class="row" ...>` control and per-row stop/retry buttons similarly (guard `_jobs_table.html` buttons with the same `{% if request.state.role in ('operator','administrator') %}`).

In `coordinator/app/web/templates/library.html`, guard the bulk bar, the star/delete buttons and group controls with `{% if request.state.role in ('operator', 'administrator') %}` (viewers still see the list + "смотреть").

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_roles_jobs.py tests/test_web.py tests/test_library.py -q -p no:cacheprovider`
Expected: PASS. `client` (admin) keeps existing tests green.

- [ ] **Step 5: Commit**

```bash
git add coordinator/app/web/router.py coordinator/app/api/jobs.py coordinator/app/api/library.py coordinator/app/web/templates/jobs.html coordinator/app/web/templates/_jobs_table.html coordinator/app/web/templates/library.html coordinator/tests/test_roles_jobs.py
git commit -m "Enforce viewer/operator access on jobs and library"
```

---

### Task 5: Enforce administrator on server management

**Files:**
- Modify: `coordinator/app/web/router.py` (guard `servers_page`, `servers_fragment`, `server_delete`)
- Modify: `coordinator/app/api/workers.py` (guard `delete_worker` with administrator; `list_workers`/`get_worker` with viewer)
- Modify: `coordinator/app/web/templates/_dashboard.html` (hide ⚙ from non-admin)
- Test: `coordinator/tests/test_roles_servers.py`

**Interfaces:**
- Consumes: `require_role`.

- [ ] **Step 1: Write the failing test**

```python
# coordinator/tests/test_roles_servers.py
def test_operator_cannot_open_servers_page(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    r = anon_client.get("/servers", follow_redirects=False)
    assert r.status_code == 403


def test_operator_cannot_delete_worker(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    wid = anon_client.post("/api/workers/register", json={"name": "agent-01"})
    # register is agent-only later; here token is unset so it still works.
    wid = wid.json()["id"] if wid.status_code == 200 else 1
    assert anon_client.request("DELETE", f"/api/workers/{wid}").status_code == 403


def test_admin_sees_gear_and_can_open_servers(client):
    assert "⚙ Управление" in client.get("/").text
    assert client.get("/servers").status_code == 200


def test_operator_ui_hides_gear(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    assert "⚙ Управление" not in anon_client.get("/").text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_roles_servers.py -q -p no:cacheprovider`
Expected: FAIL (servers open to operator).

- [ ] **Step 3: Write minimal implementation**

In `coordinator/app/web/router.py`, add `dependencies=[Depends(require_role("administrator"))]` to `servers_page`, `servers_fragment`, `server_delete`.

In `coordinator/app/api/workers.py`, add:

```python
from app.services.auth import require_role
```

Add `dependencies=[Depends(require_role("administrator"))]` to `delete_worker`, and `dependencies=[Depends(require_role("viewer"))]` to `list_workers` and `get_worker`. Leave the six machine endpoints unguarded here (Task 6 adds the agent-token guard).

In `coordinator/app/web/templates/_dashboard.html`, guard the gear button:

```html
    {% if request.state.role == 'administrator' %}
    <a class="btn" href="/servers" title="Управление серверами" aria-label="Управление серверами"
       style="text-decoration:none">⚙ Управление</a>
    {% endif %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_roles_servers.py tests/test_web.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coordinator/app/web/router.py coordinator/app/api/workers.py coordinator/app/web/templates/_dashboard.html coordinator/tests/test_roles_servers.py
git commit -m "Restrict server management to administrators"
```

---

### Task 6: Agent token on machine endpoints + agent sends it

**Files:**
- Modify: `coordinator/app/api/workers.py` (agent-token dependency on the six machine endpoints)
- Modify: `coordinator/app/web/router.py` (nothing) — N/A
- Modify: `agent/app/core/config.py` (add `agent_token`)
- Modify: `agent/app/coordinator_client.py` (send `X-Agent-Token` header)
- Modify: `agent/app/main.py` (pass `settings.agent_token` into `CoordinatorClient`)
- Modify: `deploy/.env.example` (document all new env vars)
- Test: `coordinator/tests/test_agent_token.py`

**Interfaces:**
- Consumes: `require_agent_token` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
# coordinator/tests/test_agent_token.py
import importlib


def _reload_settings(token: str, monkeypatch):
    import os
    monkeypatch.setenv("VIDHIVE_AGENT_TOKEN", token)
    from app.core import config
    config.get_settings.cache_clear()


def test_register_requires_token_when_configured(anon_client, monkeypatch):
    _reload_settings("secret-tok", monkeypatch)
    try:
        # no header -> 401
        r = anon_client.post("/api/workers/register", json={"name": "a1"})
        assert r.status_code == 401
        # correct header -> ok
        r = anon_client.post("/api/workers/register", json={"name": "a1"},
                             headers={"X-Agent-Token": "secret-tok"})
        assert r.status_code == 200
    finally:
        _reload_settings("", monkeypatch)


def test_register_open_when_token_unset(anon_client):
    # Default env has no token -> machine endpoints stay open.
    r = anon_client.post("/api/workers/register", json={"name": "a2"})
    assert r.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_agent_token.py -q -p no:cacheprovider`
Expected: FAIL (token ignored; register returns 200 even with token set and no header).

- [ ] **Step 3: Write minimal implementation**

In `coordinator/app/api/workers.py`, import and guard the six machine endpoints:

```python
from app.services.auth import require_agent_token
```

Add `dependencies=[Depends(require_agent_token)]` to: `register_worker`, `heartbeat`, `lease`, `progress`, `complete`, `fail`. (Add `from fastapi import Depends` if not already imported.)

In `agent/app/core/config.py`, add after `coordinator_url`:

```python
    # Shared secret sent to the coordinator's machine endpoints (empty = none).
    agent_token: str = ""
```

In `agent/app/coordinator_client.py`, send the header:

```python
    def __init__(self, base_url: str, timeout: float = 15.0, agent_token: str = "") -> None:
        headers = {"X-Agent-Token": agent_token} if agent_token else {}
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers)
```

In `agent/app/main.py`, where `CoordinatorClient(...)` is constructed, pass the token:

```python
    client = CoordinatorClient(settings.coordinator_url, agent_token=settings.agent_token)
```

(Match the existing constructor call; add only the `agent_token=` keyword.)

In `deploy/.env.example`, append:

```dotenv
# --- Access control (coordinator) ---
VIDHIVE_SECRET_KEY=change-me-to-a-long-random-string
VIDHIVE_ADMIN_USER=admin
VIDHIVE_ADMIN_PASSWORD=change-me
# Shared secret for agents' machine endpoints (leave empty to disable the check).
VIDHIVE_AGENT_TOKEN=
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_agent_token.py -q -p no:cacheprovider`
Expected: PASS. Then run the agent's own suite: from `agent/`, `PYTHONPATH="C:/Projects/Vidhive/common" python -m pytest tests/ -q` (or the agent venv) — still green.

- [ ] **Step 5: Commit**

```bash
git add coordinator/app/api/workers.py agent/app/core/config.py agent/app/coordinator_client.py agent/app/main.py deploy/.env.example coordinator/tests/test_agent_token.py
git commit -m "Guard machine endpoints with an agent token"
```

---

### Task 7: User management page (administrator)

**Files:**
- Create: `coordinator/app/web/templates/users.html`
- Modify: `coordinator/app/web/router.py` (users page + actions)
- Modify: `coordinator/app/web/templates/base.html` (nav link to /users for admin)
- Test: `coordinator/tests/test_users.py`

**Interfaces:**
- Consumes: `require_role`, `hash_password`, `role_of`, `ROLE_RANK`.

- [ ] **Step 1: Write the failing test**

```python
# coordinator/tests/test_users.py
def test_admin_can_create_user_and_it_can_login(client, anon_client):
    r = client.post("/users", data={"username": "newop", "password": "pw", "role": "operator"})
    assert r.status_code == 200            # followed redirect to /users
    assert "newop" in r.text
    # the new user can log in and act as operator
    anon_client.post("/login", data={"username": "newop", "password": "pw"})
    assert anon_client.post("/api/jobs", json={"range_start": 0, "range_end": 1}).status_code == 201


def test_non_admin_cannot_open_users(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    assert anon_client.get("/users", follow_redirects=False).status_code == 403


def test_cannot_delete_last_administrator(client, raw_sql):
    uid = raw_sql("SELECT id FROM users WHERE username='tester-admin'")[0][0]
    r = client.post(f"/users/{uid}/delete")
    assert r.status_code in (200, 409)
    # the admin still exists
    assert raw_sql("SELECT COUNT(*) FROM users WHERE id=?", (uid,))[0][0] == 1


def test_admin_can_change_role(client, make_user, raw_sql):
    make_user("mover", "pw", "viewer")
    uid = raw_sql("SELECT id FROM users WHERE username='mover'")[0][0]
    r = client.post(f"/users/{uid}/role", data={"role": "operator"})
    assert r.status_code == 200
    role = raw_sql(
        "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?",
        (uid,),
    )[0][0]
    assert role == "operator"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_users.py -q -p no:cacheprovider`
Expected: FAIL (no `/users`).

- [ ] **Step 3: Write minimal implementation**

In `coordinator/app/web/router.py`, add a helper and routes (all admin-guarded). Uses `hash_password`, `RoleModel`, `User`, `UserRole`, `role_of`, `ROLE_RANK` (import `from app.services.auth import ROLE_RANK, require_role, role_of` and `from app.services.security import hash_password`).

```python
async def _users_context(session: AsyncSession) -> dict:
    users = (await session.execute(select(User).order_by(User.username))).scalars().all()
    rows = [{"id": u.id, "username": u.username, "role": await role_of(session, u.id)} for u in users]
    return {"users": rows, "roles": list(ROLE_RANK.keys())}


async def _admin_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(UserRole)
            .join(RoleModel, RoleModel.id == UserRole.role_id)
            .where(RoleModel.name == "administrator")
        )
    ).scalar_one()


async def _set_role(session: AsyncSession, user_id: int, role: str) -> None:
    await session.execute(UserRole.__table__.delete().where(UserRole.user_id == user_id))
    rid = (await session.execute(select(RoleModel.id).where(RoleModel.name == role))).scalar_one()
    session.add(UserRole(user_id=user_id, role_id=rid))


@router.get("/users", response_class=HTMLResponse, dependencies=[Depends(require_role("administrator"))])
async def users_page(request: Request, error: str = "", session: AsyncSession = Depends(get_session)):
    ctx = await _users_context(session)
    ctx["error"] = error
    return _page(request, "users.html", ctx)


@router.post("/users", dependencies=[Depends(require_role("administrator"))])
async def users_create(
    username: str = Form(...), password: str = Form(...), role: str = Form("viewer"),
    session: AsyncSession = Depends(get_session),
):
    if role not in ROLE_RANK:
        return RedirectResponse("/users?error=неизвестная+роль", status_code=303)
    exists = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if exists is not None:
        return RedirectResponse("/users?error=логин+занят", status_code=303)
    user = User(username=username, password_hash=hash_password(password))
    session.add(user)
    await session.flush()
    await _set_role(session, user.id, role)
    await session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/role", dependencies=[Depends(require_role("administrator"))])
async def users_set_role(
    user_id: int, role: str = Form(...), session: AsyncSession = Depends(get_session)
):
    if role not in ROLE_RANK:
        return RedirectResponse("/users?error=неизвестная+роль", status_code=303)
    # Guard against demoting the last administrator.
    current = await role_of(session, user_id)
    if current == "administrator" and role != "administrator" and await _admin_count(session) <= 1:
        return RedirectResponse("/users?error=нельзя+снять+последнего+админа", status_code=303)
    await _set_role(session, user_id, role)
    await session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/password", dependencies=[Depends(require_role("administrator"))])
async def users_set_password(
    user_id: int, password: str = Form(...), session: AsyncSession = Depends(get_session)
):
    user = await session.get(User, user_id)
    if user is not None:
        user.password_hash = hash_password(password)
        await session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/delete", dependencies=[Depends(require_role("administrator"))])
async def users_delete(
    request: Request, user_id: int, session: AsyncSession = Depends(get_session)
):
    me = request.state.user
    if me is not None and me.id == user_id:
        return RedirectResponse("/users?error=нельзя+удалить+себя", status_code=303)
    if await role_of(session, user_id) == "administrator" and await _admin_count(session) <= 1:
        return RedirectResponse("/users?error=нельзя+удалить+последнего+админа", status_code=303)
    user = await session.get(User, user_id)
    if user is not None:
        await session.delete(user)
        await session.commit()
    return RedirectResponse("/users", status_code=303)
```

Create `coordinator/app/web/templates/users.html`:

```html
{% extends "base.html" %}
{% block title %}Vidhive — пользователи{% endblock %}
{% block content %}
{% if error %}<div class="card" style="border-color:var(--bad)"><span style="color:var(--bad)">⚠ {{ error }}</span></div>{% endif %}

<div class="card">
  <h2>Добавить пользователя</h2>
  <form method="post" action="/users" class="row" style="gap:10px; flex-wrap:wrap; align-items:flex-end">
    <div><label>Логин</label><input type="text" name="username" required></div>
    <div><label>Пароль</label><input type="password" name="password" required></div>
    <div><label>Роль</label>
      <select name="role">{% for r in roles %}<option value="{{ r }}">{{ r }}</option>{% endfor %}</select>
    </div>
    <input type="submit" class="btn primary" value="Создать">
  </form>
</div>

<div class="card">
  <h2>Пользователи</h2>
  <table>
    <thead><tr><th>Логин</th><th>Роль</th><th>Действия</th></tr></thead>
    <tbody>
    {% for u in users %}
      <tr>
        <td>{{ u.username }}</td>
        <td>
          <form method="post" action="/users/{{ u.id }}/role" class="inline">
            <select name="role" onchange="this.form.submit()">
              {% for r in roles %}<option value="{{ r }}" {{ 'selected' if u.role == r else '' }}>{{ r }}</option>{% endfor %}
            </select>
          </form>
        </td>
        <td style="white-space:nowrap">
          <form method="post" action="/users/{{ u.id }}/password" class="inline"
                onsubmit="this.pw.value=prompt('Новый пароль для {{ u.username }}')||''; return !!this.pw.value">
            <input type="hidden" name="password" class="pw">
            <span class="btn" onclick="this.closest('form').requestSubmit()">Сбросить пароль</span>
          </form>
          <form method="post" action="/users/{{ u.id }}/delete" class="inline"
                onsubmit="return confirm('Удалить пользователя {{ u.username }}?')">
            <input type="submit" class="btn" style="border-color:var(--bad); color:var(--bad)" value="🗑">
          </form>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

In `coordinator/app/web/templates/base.html`, add a nav link for admins (inside `<nav>`, after the Library link):

```html
      {% if request.state.role == 'administrator' %}<a href="/users">Пользователи</a>{% endif %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/test_users.py -q -p no:cacheprovider`
Expected: PASS. Then the whole suite: `... -m pytest tests/ -q -p no:cacheprovider` — all green.

- [ ] **Step 5: Commit**

```bash
git add coordinator/app/web/router.py coordinator/app/web/templates/users.html coordinator/app/web/templates/base.html coordinator/tests/test_users.py
git commit -m "Add administrator user-management page"
```

---

## Rollout (after all tasks, when deploying to the stand)

Follow the spec §8 order so the stand never breaks:
1. Deploy code with `VIDHIVE_AGENT_TOKEN` empty (machine endpoints stay open; agents keep working).
2. Set `VIDHIVE_SECRET_KEY`, `VIDHIVE_ADMIN_USER`, `VIDHIVE_ADMIN_PASSWORD` in `deploy/.env`; rebuild+restart coordinator → admin created, login on.
3. Set `VIDHIVE_AGENT_TOKEN` for coordinator and agents; restart → machine endpoints closed.
