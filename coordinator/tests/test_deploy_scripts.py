import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = [REPO / "deploy" / "agent-bootstrap.sh", REPO / "deploy" / "agent-teardown.sh"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shell_script_syntax(script):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    assert script.exists(), f"missing {script}"
    r = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
