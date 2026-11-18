"""The install skill (plugins/resumebot-setup/skills/install-resumebot/SKILL.md): the one shell-neutral Python command
it still holds, the one that finds the newest release, is cut out of the skill and run as written, and the skill's own
rules are checked. The look, the backup and the replacement moved into the install script (app/scripts/local/install.py)
in 2.9.0: test_install_script.py covers them. No network (a local repository stands in for the release repository)."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / "plugins" / "resumebot-setup" / "skills" / "install-resumebot" / "SKILL.md"
DOC = ROOT / "docs" / "install.md"

pytestmark = pytest.mark.skipif(not SKILL.exists(), reason="the setup plugin is not in this image")

NEWEST = "re.findall"


def command(marker: str) -> str:
    """The Python code of the one `& "$PY" -c "..."` line in the skill that holds `marker`."""
    lines = [line for line in SKILL.read_text(encoding="utf-8").splitlines()
             if line.startswith('& "$PY" -c "') and marker in line]
    assert len(lines) == 1, f"{marker!r} is in {len(lines)} commands"
    found = re.fullmatch(r'& "\$PY" -c "(.*?)"((?: "\$\w+")*)', lines[0])
    assert found, lines[0]
    return found.group(1)


def run(marker: str, *args: Path | str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", command(marker), *map(str, args)], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


# --- the newest release -----------------------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not here")
def test_the_newest_release_is_the_highest_version_number_not_the_latest_tag_and_pre_releases_are_ignored(tmp_path):
    repo = tmp_path / "release-repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com"}

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "one")
    for tag in ("app-v2.4.0", "app-v2.10.0", "app-v2.9.9", "app-v3.0.0-rc1", "plugin-v9.9.9"):  # tagged in this order
        git("tag", tag)
    done = run(NEWEST, repo)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "app-v2.10.0"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not here")
def test_a_repository_with_no_release_tag_makes_the_release_command_fail_so_nothing_is_deleted(tmp_path):
    repo = tmp_path / "release-repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    assert run(NEWEST, repo).returncode != 0


# --- the skill itself -------------------------------------------------------------------------------------------------


def test_the_skill_never_stops_at_an_existing_install_and_writes_every_command_for_powershell_5_and_sh():
    text = SKILL.read_text(encoding="utf-8")
    assert text.count("already installed") == 1 and 'Never stop at "already installed"' in text
    assert "install.py" in text, "the skill runs the install script"
    fenced = [line for block in re.findall(r"```\w*\n(.*?)```", text, re.S) for line in block.splitlines()]
    assert not [line for line in fenced if "&&" in line or "||" in line]
    quoted = [line for line in fenced if line.startswith('"$') and " -c " in line]
    assert not quoted, "a quoted program is called with &"
    code = command(NEWEST)
    assert '"' not in code and "\\" not in code.replace("\\s", ""), NEWEST


def test_the_install_page_describes_the_same_one_flow_and_not_a_separate_update_job():
    page = DOC.read_text(encoding="utf-8")
    assert "stops when it finds an existing install" not in page
    assert "There is no `update` command yet" not in page
