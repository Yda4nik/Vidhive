# SSH Agent Deployment — Implementation Plan

**Goal:** Let an administrator deploy a Vidhive agent onto a bare Linux server over SSH from the web, and fully tear it down again.

**Spec:** `docs/specs/2026-09-14-ssh-agent-deployment-design.md`

## Global Constraints
- Admin-only. SSH key/password never stored or logged; escape every form value with `shlex.quote`.
- New dependency: `asyncssh` (coordinator image + requirements).
- Any-Linux target: detect apt/dnf/yum/pacman/zypper/apk; systemd service with nohup fallback.
- Tests run from `coordinator/` via the session venv: `PYTHONPATH="C:/Projects/Vidhive/common" ./.venv-test/Scripts/python.exe -m pytest tests/ -p no:cacheprovider`.

---

### Task 1: Bootstrap & teardown shell scripts
- Create `deploy/agent-bootstrap.sh`: detect pkg manager; install python3/venv/pip/ffmpeg/git; `git clone`/pull repo to `$INSTALL_DIR` (default `/opt/vidhive`); venv + `pip install ./common` + agent reqs; write `$ENV_FILE` (`/etc/vidhive-agent.env`) from `VIDHIVE_*` env; install+enable+start systemd unit `vidhive-agent` (nohup fallback). `set -euo pipefail`, echo each step.
- Create `deploy/agent-teardown.sh`: stop/disable service (or kill pidfile), remove unit + daemon-reload, `rm -rf $INSTALL_DIR`, `rm -f $ENV_FILE`, `rm -rf $STORAGE_PATH`. Idempotent.
- Test: `bash -n deploy/agent-bootstrap.sh deploy/agent-teardown.sh` (syntax) — add a pytest that shells out to `bash -n` and skips if bash absent.
- Commit.

### Task 2: SSH deployer service (mock-tested)
- Add `asyncssh` to `coordinator/requirements.txt`; install into venv.
- Create `app/services/deployer.py`:
  - `DeployParams` dataclass (ssh_host/port/user/key + agent config fields).
  - `async run_script(params, script_text, extra_env, on_line) -> int`: connect with `asyncssh.connect(host, port, username, client_keys=[key], known_hosts=None)`, build `export VAR=shlex.quote(val) …; bash -s`, `conn.run(..., input=script_text)` streaming stdout+stderr lines to `on_line`, return exit status.
  - `async deploy(params, on_line)` / `async teardown(params, on_line)` read the two scripts and call `run_script` with the right env.
  - `_build_env_prefix(env)` helper (pure) — the piece unit tests assert on.
- Test `tests/test_deployer.py`: `_build_env_prefix` quotes values / rejects nothing injectable; a fake asyncssh (monkeypatched module) captures the command + input and returns a scripted exit code + lines; assert deploy/teardown pass the right env and parse output. No real network.
- Commit.

### Task 3: agent_deployments model + migration
- Add `AgentDeployment` model (`app/db/models.py`): id, worker_id FK workers ondelete SET NULL nullable, ssh_host, ssh_port, ssh_user, install_dir, service_name, storage_path, created_at.
- Alembic migration `..._agent_deployments.py` (down_revision = current head `b2c3d4e5f6a7`): create table.
- Confirm linear head; `create_all` covers it for SQLite tests.
- Commit.

### Task 4: Deploy form + live-log page
- In-memory registry `app/services/deployments.py`: `deployments: dict[str, {status, kind, lines[]}]`, helpers `new_deployment()`, `append(id,line)`, `finish(id,ok)`, `get(id)`.
- Web routes (`web/router.py`, admin):
  - GET section on `/servers`: a «Добавить сервер» form (collapsed card) posting to `/servers/deploy`.
  - POST `/servers/deploy`: validate (raise back to /servers with error on bad input); create deployment id; record `AgentDeployment` (worker_id null until the agent registers, matched later by worker_name); spawn `asyncio.create_task(deployer.deploy(params, lambda l: append(id,l)))`; redirect to `/servers/deploy/{id}`.
  - GET `/servers/deploy/{id}`: page rendering current log + status, JS polls the log fragment every 1s.
  - GET `/servers/deploy/{id}/log`: returns the log lines + status (fragment/JSON).
- Prefill `coordinator_url` from `request.base_url`; `agent_url` from host.
- Templates: `server_add.html` (or a card in servers.html) + `deploy_log.html`.
- Tests: POST with a stubbed `deployer.deploy` (monkeypatched to append a couple lines + finish ok) → deployment id created, log page shows lines, AgentDeployment row written; validation rejects a bad worker_name.
- Commit.

### Task 5: Teardown flow from the delete dialog
- Mark SSH-deployed servers (join `agent_deployments` by worker_id in `_servers_context`).
- In the delete modal (servers.html) add, for such servers, a «Полностью снести с машины» option → opens a small form (host/port/user prefilled, key textarea) posting to `/servers/{worker_id}/teardown`.
- POST `/servers/{worker_id}/teardown` (admin): load AgentDeployment; create deployment id (kind=teardown); spawn `deployer.teardown(...)`; on success delete the AgentDeployment row and the Worker; redirect to `/servers/deploy/{id}` (same live-log page).
- Tests: with stubbed `deployer.teardown` → worker + deployment row removed on success.
- Commit.

### Task 6: E2E on the user's Linux server
- Ask the user for host/port/user/key at this step.
- Deploy an agent, confirm it appears online + can download; then tear it down and confirm it is gone from the pool and the server is clean.
- Document result in the README audit if appropriate.

## Rollout
Deploy coordinator (adds asyncssh + migration). Agent code is unchanged (it is packaged natively by the bootstrap). No agent-image rebuild needed for the feature itself, but rebuild all is fine.
