import asyncio

import asyncssh

from app.services import deployer
from app.services.deployer import DeployParams, build_env_prefix


def test_build_env_prefix_quotes_values():
    prefix = build_env_prefix({"VIDHIVE_WORKER_NAME": "agent-04", "X": "a b; rm -rf /"})
    assert "VIDHIVE_WORKER_NAME=agent-04" in prefix
    # A value with shell metacharacters must be quoted so it cannot inject.
    assert "'a b; rm -rf /'" in prefix


def test_read_script_returns_bootstrap():
    text = deployer.read_script("agent-bootstrap.sh")
    assert "Vidhive agent bootstrap" in text
    assert "systemctl" in text


class _FakeStdin:
    def __init__(self):
        self.data = ""

    def write(self, s):
        self.data += s

    def write_eof(self):
        pass


class _FakeStdout:
    def __init__(self, lines):
        self._lines = lines

    async def __aiter__(self):
        for line in self._lines:
            yield line


class _FakeResult:
    def __init__(self, code):
        self.exit_status = code


class _FakeProc:
    def __init__(self, capture, lines, code):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self._capture = capture
        self._code = code

    async def wait(self):
        self._capture["script"] = self.stdin.data
        return _FakeResult(self._code)


class _FakeConn:
    def __init__(self, capture, lines, code):
        self._capture, self._lines, self._code = capture, lines, code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def create_process(self, command, **kwargs):
        self._capture["command"] = command
        return _FakeProc(self._capture, self._lines, self._code)


def test_run_script_streams_lines_and_returns_code(monkeypatch):
    capture = {}

    def fake_connect(host, **kwargs):
        capture["host"] = host
        capture["kwargs"] = kwargs
        return _FakeConn(capture, ["line 1", "line 2"], 0)

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    monkeypatch.setattr(asyncssh, "import_private_key", lambda key: "PARSED_KEY")

    params = DeployParams(
        ssh_host="1.2.3.4", ssh_port=2222, ssh_user="root", ssh_key="KEYTEXT",
        env={"VIDHIVE_WORKER_NAME": "agent-04", "VIDHIVE_COORDINATOR_URL": "http://c:8000"},
    )
    lines = []
    code = asyncio.run(deployer.run_script(params, "echo hi", lines.append))

    assert code == 0
    assert lines == ["line 1", "line 2"]
    assert capture["host"] == "1.2.3.4"
    assert capture["kwargs"]["port"] == 2222
    assert capture["kwargs"]["username"] == "root"
    # env made it into the remote command, quoted
    assert "VIDHIVE_WORKER_NAME=agent-04" in capture["command"]
    assert capture["command"].endswith("bash -s")
    assert capture["script"] == "echo hi"


def test_run_script_uses_password_when_given(monkeypatch):
    capture = {}

    def fake_connect(host, **kwargs):
        capture["kwargs"] = kwargs
        return _FakeConn(capture, ["ok"], 0)

    def no_key(_k):
        raise AssertionError("import_private_key must not run for password auth")

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    monkeypatch.setattr(asyncssh, "import_private_key", no_key)

    params = DeployParams(ssh_host="h", ssh_port=22, ssh_user="root", ssh_password="pw", env={})
    code = asyncio.run(deployer.run_script(params, "x", lambda _l: None))
    assert code == 0
    assert capture["kwargs"].get("password") == "pw"
    assert "client_keys" not in capture["kwargs"]


def test_run_script_propagates_failure(monkeypatch):
    def fake_connect(host, **kwargs):
        return _FakeConn({}, ["boom"], 3)

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    monkeypatch.setattr(asyncssh, "import_private_key", lambda key: "K")
    params = DeployParams("h", 22, "root", "K", env={})
    code = asyncio.run(deployer.run_script(params, "x", lambda _l: None))
    assert code == 3
