#!/usr/bin/env python3
r"""resumebot install: one script that installs, finishes or updates resumebot on this PC. Python's standard library
only (3.12), so it runs before the app exists: `uv run --python 3.12 --no-project install.py [options]`.

Every step looks first. Done and working: it prints `already ...` and skips. Present but broken: it repairs only that
piece. Missing: it does it. A second run on a finished install changes nothing under the code folder or the data
folder (apart from this script's own `install/` and `run/` files in the data folder).

The steps, in order:

    prelude   rebuild PATH from the registry (read only); find git, uv and Node 22 or newer
    look      the data folder, the code folder, the installed tag, the newest `app-v*` tag, the settings
    fetch     when the installed tag is not the newest: clone the newest tag to `<code>-new` (the app keeps running);
              a newer copy of this script re-runs itself from that tag
    stop      only when the code is to be replaced: `stack.py stop`, then confirm nothing answers
    backup    the same moment: the database (SQLite backup API, then PRAGMA quick_check), `.env` and `secrets/`
    replace   `<code>` becomes `<code>-old-<time>` and `<code>-new` becomes `<code>` (an interrupted swap is finished
              or undone on the next run); the data folder is never removed
    setup     `stack.py setup` unless the environment answers and the lock files are the ones it was made from
    init      `resumebot init` only when the settings are incomplete (needs --owner)
    start     `resumebot start` unless this version already answers
    smoke     `resumebot smoke --lanes`, polled
    signin    `resumebot signin` unless a token is stored (the one step that needs the user)
    connect   `resumebot connect`
    shortcut  `resumebot shortcut`
    doctor    `resumebot doctor`

The app's own commands do their own looking; this script calls them by absolute path and trusts their exit codes.

Exit codes: 0 done (or already done); 2 usage; 3 a prerequisite is missing; 4 the data could not be backed up (nothing
changed); 5 setup (fetch, replace, setup, init or shortcut) failed; 6 the app would not start or smoke failed; 7 sign-in
failed or timed out; 8 a door could not be registered; 9 doctor found a problem; 10 another install is running.
A failure is one sentence, `<step>: <what failed>: <what to do>`, and the same text with the tail of the child's output
is in `<data>/install/install.log`. Nothing printed or logged holds a token.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

WINDOWS = sys.platform == "win32"
SCRIPT = Path(__file__).resolve()
DATA_VARIABLE = "RESUMEBOT_DATA_DIR"
REPO_VARIABLE = "RESUMEBOT_REPO"
DEFAULT_REPO = "https://github.com/EricWurth/resumebot"
TAG_PREFIX = "app-v"
DEFAULT_APP_URL = "http://127.0.0.1:8430"
ENGINE_OFFSET = 100
NODE_MINIMUM = 22
WAITING_LIMIT = 11 * 60  # sign-in: the user has ten minutes; the command itself stops at ten
SMOKE_TRIES, SMOKE_WAIT = 6, 10.0
STOP_POLLS, STOP_WAIT = 33, 10.0  # up to 330 seconds for the app to let go
#: What a backup leaves out: nothing in them is the user's.
SKIP_IN_BACKUP = frozenset({"logs", "run", "claude", "work", "browser-profile", "install", "backups"})

OK, USAGE, PREREQUISITE, NO_BACKUP, SETUP_FAILED, NO_START, SIGNIN_FAILED, NO_DOOR, DOCTOR_FOUND, BUSY = (
    0, 2, 3, 4, 5, 6, 7, 8, 9, 10)

#: The exit code of a step's unmapped failure.
UNMAPPED = {"prelude": 3, "stop": 4, "backup": 4, "start": 6, "smoke": 6, "signin": 7, "connect": 8, "doctor": 9}

STEPS = ("prelude", "look", "fetch", "stop", "backup", "replace", "setup", "init", "start", "smoke", "signin",
         "connect", "shortcut", "doctor")

#: Token-shaped text: Claude's own prefix, a bearer header, and any long unbroken run of token characters.
TOKEN_SHAPES = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(bearer\s+)[^\s\"']+"),
    re.compile(r"[A-Za-z0-9_\-]{40,}"),
)


def redact(text: str) -> str:
    """The text with anything token-shaped replaced, for everything this script prints or logs."""
    text = TOKEN_SHAPES[0].sub("[token]", text)
    text = TOKEN_SHAPES[1].sub(lambda found: found.group(1) + "[token]", text)
    return TOKEN_SHAPES[2].sub("[token]", text)


class Fail(Exception):
    """A step that cannot go on: `<step>: <what failed>: <what to do>`, and the exit code."""

    def __init__(self, code: int, step: str, what: str, todo: str, detail: str = "") -> None:
        super().__init__(f"{step}: {what}: {todo}")
        self.code, self.step, self.detail = code, step, detail


@dataclass
class Result:
    rc: int
    out: str = ""


# --- the machine: every side effect goes through this, and a test replaces it -----------------------------------------


class Host:
    """What the script does to the machine. The tests' fake overrides each of these."""

    def __init__(self) -> None:
        self.path = os.environ.get("PATH", "")

    def run(self, argv: list[str], cwd: Path | None = None, timeout: float = 600) -> Result:
        """Run a program without a window, its output (both streams) read as UTF-8, bad bytes replaced."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if WINDOWS else 0
        env = {**os.environ, "PATH": self.path, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            done = subprocess.run(argv, check=False, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=timeout,
                                  creationflags=flags)
        except subprocess.TimeoutExpired:
            return Result(124, f"timed out after {int(timeout)} seconds")
        except OSError as error:
            return Result(127, f"could not start {argv[0]}: {error.strerror or error}")
        return Result(done.returncode, done.stdout or "")

    def now(self) -> dt.datetime:
        return dt.datetime.now().astimezone()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if WINDOWS:
            found = self.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=20)
            return f'"{pid}"' in found.out
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def health(self, url: str) -> dict | None:
        """What `<url>/health` says, or None when nothing answers. Never through a proxy."""
        import urllib.error
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"{url}/health", timeout=5) as reply:
                return json.loads(reply.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            try:
                return json.loads(error.read().decode("utf-8", "replace"))
            except (ValueError, OSError):
                return None
        except (OSError, ValueError):
            return None

    def which(self, name: str) -> str | None:
        return shutil.which(name, path=self.path)

    def registry_path(self) -> list[str]:
        """The user's and the machine's PATH entries from the registry (read only); empty away from Windows."""
        if not WINDOWS:
            return []
        import winreg

        entries: list[str] = []
        for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(root, key) as opened:
                    value, _ = winreg.QueryValueEx(opened, "Path")
            except OSError:
                continue
            entries += [os.path.expandvars(part) for part in str(value).split(os.pathsep) if part.strip()]
        return entries

    def registry_user_variable(self, name: str) -> str | None:
        if not WINDOWS:
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as opened:
                value, _ = winreg.QueryValueEx(opened, name)
        except OSError:
            return None
        return os.path.expandvars(str(value)) or None

    def registered_data_folder(self) -> str | None:
        """The `--data` named by a registered resumebot door in Claude Code's own configuration file (read only), or
        None when no door names one."""
        folder = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        config = (Path(folder).expanduser() if folder else Path.home()) / ".claude.json"
        try:
            servers = json.loads(config.read_text(encoding="utf-8")).get("mcpServers") or {}
        except (OSError, ValueError, AttributeError):
            return None
        for name, entry in servers.items():
            words = entry.get("args") if isinstance(entry, dict) else None
            if str(name).startswith("resumebot") and isinstance(words, list) and "--data" in words[:-1]:
                return str(words[words.index("--data") + 1]) or None
        return None


# --- paths ------------------------------------------------------------------------------------------------------------


def local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def default_data_folder() -> Path:
    if WINDOWS:
        return local_app_data() / "resumebot"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "resumebot"


def default_code_folder() -> Path:
    if WINDOWS:
        return local_app_data() / "Programs" / "resumebot"
    return Path.home() / ".local" / "share" / "resumebot-app"


def venv_python(code: Path) -> Path:
    return code / "app" / "backend" / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def resumebot_program(code: Path) -> Path:
    return code / "app" / "backend" / ".venv" / ("Scripts/resumebot.exe" if WINDOWS else "bin/resumebot")


def stack_script(code: Path) -> Path:
    return code / "app" / "scripts" / "local" / "stack.py"


def installed_script(code: Path) -> Path:
    return code / "app" / "scripts" / "local" / "install.py"


def inside(path: Path, folder: Path) -> bool:
    """True when `path` is `folder` or lies under it (resolved, case folded on Windows)."""
    def norm(value: Path) -> str:
        text = os.path.normcase(str(value.resolve()))
        return text.rstrip("\\/")

    one, two = norm(path), norm(folder)
    return one == two or one.startswith(two + os.sep)


def assert_apart(code: Path, data: Path) -> None:
    """The code folder and the data folder never contain each other: replacing the one can never touch the other."""
    if inside(code, data) or inside(data, code):
        raise Fail(USAGE, "look", f"the code folder {code} and the data folder {data} lie inside each other",
                   "name two separate folders with --code and --data")


def safe_remove(path: Path, code: Path, data: Path) -> None:
    """Delete one folder this script made (`<code>-new`, never anything else). The data folder, or any folder holding
    it or lying in it, is refused whatever is asked."""
    allowed = path.name == code.name + "-new" and path.parent == code.parent
    if not allowed or inside(path, data) or inside(data, path):
        raise Fail(SETUP_FAILED, "replace", f"refused to remove {path}",
                   "this script only clears its own <code>-new folder")

    def writable(function: Callable[..., object], target: str, _error: object) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=writable) if sys.version_info >= (3, 12) else shutil.rmtree(path, onerror=writable)


# --- the script's own state: log, status file, lock -------------------------------------------------------------------


class Journal:
    """`<data>/install/install.log` (everything, redacted), `install.json` (each step's state, written atomically) and
    the printed lines."""

    def __init__(self, data: Path, host: Host, out: Callable[[str], None] = print) -> None:
        self.folder = data / "install"
        self.host, self.out = host, out
        self.state: dict = {"steps": {}, "state": "running"}
        try:
            self.state = json.loads((self.folder / "install.json").read_text(encoding="utf-8"))
            self.state["steps"] = {}
            self.state["state"] = "running"
            self.state.pop("action", None)
        except (OSError, ValueError):
            pass

    @property
    def log_path(self) -> Path:
        return self.folder / "install.log"

    def log(self, text: str) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            stamp = self.host.now().isoformat(timespec="seconds")
            with self.log_path.open("a", encoding="utf-8", errors="replace") as file:
                file.write(f"{stamp} {redact(text)}\n")
        except OSError:
            pass

    def say(self, text: str) -> None:
        text = redact(text)
        self.out(text)
        self.log(text)

    def save(self) -> None:
        self.state["updated"] = self.host.now().isoformat(timespec="seconds")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            target = self.folder / "install.json"
            temporary = target.with_name("install.json.tmp")
            temporary.write_text(json.dumps(self.state, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, target)
        except OSError:
            pass

    def step(self, name: str, state: str, text: str) -> None:
        """Record and print one step's outcome: `done`, `already`, `skipped` or `failed`."""
        self.state["steps"][name] = {"state": state, "text": redact(text)}
        self.save()
        self.say(f"[{name}] {text}")

    def waiting(self, action: str) -> None:
        self.state["state"], self.state["action"] = "waiting-for-user", action
        self.save()
        self.say(f"[signin] waiting for you: {action}")

    def resume(self) -> None:
        self.state["state"] = "running"
        self.state.pop("action", None)
        self.save()


class Lock:
    """`<data>/run/install.lock`, created exclusively and holding this process's id. A lock whose process is gone is
    stale and is taken over; a live one refuses the second run (exit 10)."""

    def __init__(self, data: Path, host: Host) -> None:
        self.path = data / "run" / "install.lock"
        self.host = host
        self.held = False

    def take(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    pid = 0
                if pid and pid != os.getpid() and self.host.pid_alive(pid):
                    raise Fail(BUSY, "lock", f"an install is already running (pid {pid})",
                               "wait for it to finish, then run this again") from None
                self.path.unlink(missing_ok=True)  # stale: its process is gone
                continue
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(str(os.getpid()))
            self.held = True
            return
        raise Fail(BUSY, "lock", "could not take the install lock", f"delete {self.path} if no install is running")

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


# --- the run ----------------------------------------------------------------------------------------------------------


def tag_version(tag: str) -> tuple[int, ...] | None:
    found = re.fullmatch(re.escape(TAG_PREFIX) + r"(\d+(?:\.\d+)*)", tag)
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.removeprefix("export ").strip()] = value.strip().strip("\"'")
    return values


def file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


class Install:
    def __init__(self, args: argparse.Namespace, host: Host, out: Callable[[str], None] = print) -> None:
        self.args, self.host = args, host
        # the data folder: --data, then the variable, then the registered doors' --data, then the default
        variable = os.environ.get(DATA_VARIABLE, "").strip() or (host.registry_user_variable(DATA_VARIABLE) or "")
        self.data = Path(args.data or variable or host.registered_data_folder() or default_data_folder()).expanduser()
        self.code = Path(args.code or default_code_folder()).expanduser()
        self.repo = args.repo or os.environ.get(REPO_VARIABLE) or DEFAULT_REPO
        self.bootstrap = (Path(args.bootstrap).expanduser() if args.bootstrap
                          else local_app_data() / "resumebot-bootstrap")
        self.journal = Journal(self.data, host, out)
        self.lock = Lock(self.data, host)
        self.newest: str | None = None
        self.installed: str | None = None
        self.stopped = False
        self.backed_up = False
        self.replacing = False

    # -- helpers --

    @property
    def new_folder(self) -> Path:
        return self.code.with_name(self.code.name + "-new")

    @property
    def env(self) -> dict[str, str]:
        return read_env(self.data / ".env")

    @property
    def app_url(self) -> str:
        return (self.env.get("RESUMEBOT_APP_URL") or DEFAULT_APP_URL).rstrip("/")

    @property
    def engine_url(self) -> str:
        found = re.fullmatch(r"(.*:)(\d+)", self.app_url)
        return f"{found.group(1)}{int(found.group(2)) + ENGINE_OFFSET}" if found else self.app_url

    def child(self, step: str, argv: list[str], cwd: Path | None = None, timeout: float = 900) -> Result:
        """Run a program and log it; the output goes to the log, redacted, and is not printed."""
        self.journal.log(f"run {' '.join(str(part) for part in argv)}")
        done = self.host.run([str(part) for part in argv], cwd=cwd, timeout=timeout)
        self.journal.log(f"exit {done.rc}\n{tail(done.out, 60)}")
        return done

    def app_command(self, *words: str, timeout: float = 900) -> Result:
        return self.child(words[0], [resumebot_program(self.code), *words, "--data", self.data], timeout=timeout)

    def stack(self, *words: str, timeout: float = 900) -> Result:
        python = venv_python(self.code)
        if not python.exists():
            python = Path(sys.executable)
        return self.child("stack", [python, stack_script(self.code), "--data", self.data, *words], cwd=self.code,
                          timeout=timeout)

    def installed_tag(self) -> str | None:
        if not (self.code / ".git").exists():
            return None
        found = self.host.run(["git", "-C", str(self.code), "describe", "--tags", "--exact-match",
                               "--match", TAG_PREFIX + "*"], timeout=60)
        text = found.out.strip()
        return text if found.rc == 0 and tag_version(text) else None

    def settings_complete(self) -> bool:
        if not resumebot_program(self.code).exists():
            return False
        if not self.env.get("JOBSEARCH_OWNER"):
            return False
        found = self.child("look", [resumebot_program(self.code), "status", "--for-claude", "--data", self.data],
                           timeout=120)
        # `status` says "not set up" for a missing settings file and "not finished" for an init a stop cut short
        return found.rc == 0 and "not set up" not in found.out and "not finished" not in found.out

    # -- the steps --

    def prelude(self) -> None:
        merged: list[str] = []
        for part in [*self.host.registry_path(), *self.host.path.split(os.pathsep)]:
            if part and part.lower() not in (seen.lower() for seen in merged):
                merged.append(part)
        self.host.path = os.pathsep.join(merged)
        missing = [name for name in ("git", "uv", "node") if not self.host.which(name)]
        if missing:
            raise Fail(PREREQUISITE, "prelude", f"{', '.join(missing)} not found",
                       "install " + ", ".join(missing)
                       + " (winget install Git.Git, astral-sh.uv, OpenJS.NodeJS.LTS), then run this again")
        said = self.host.run(["node", "--version"], timeout=30).out.strip()
        found = re.match(r"v?(\d+)", said)
        if not found or int(found.group(1)) < NODE_MINIMUM:
            raise Fail(PREREQUISITE, "prelude", f"Node {said or 'unknown'} is older than {NODE_MINIMUM}",
                       f"install Node {NODE_MINIMUM} or newer, then run this again")
        self.journal.step("prelude", "done", "git, uv and Node are there")

    def recover(self) -> None:
        """Finish or undo a swap that a kill left half done: the code folder missing with `-old-` folders beside it."""
        if self.code.exists():
            return
        olds = sorted(self.code.parent.glob(self.code.name + "-old-*"))
        if (self.new_folder / ".install-ready").exists():
            os.replace(self.new_folder, self.code)
            (self.code / ".install-ready").unlink(missing_ok=True)  # the marker is for the swap only
            self.journal.say("[look] finished an interrupted replace: the new code is in place")
        elif olds:
            os.replace(olds[-1], self.code)
            self.journal.say(f"[look] undid an interrupted replace: {olds[-1].name} is back")

    def look(self) -> None:
        self.recover()
        self.installed = self.installed_tag()
        found = self.host.run(["git", "ls-remote", "--tags", "--refs", self.repo, TAG_PREFIX + "*"], timeout=120)
        tags = [(tag_version(name), name) for name in re.findall(r"refs/tags/(\S+)", found.out)]
        tags = [pair for pair in tags if pair[0]]
        if found.rc != 0 or not tags:
            raise Fail(SETUP_FAILED, "look", "could not read the newest release from the repository",
                       "check the internet connection, then run this again", found.out)
        self.newest = max(tags)[1]
        self.journal.state["tag"] = self.installed
        self.journal.say(f"[look] data folder {self.data}")
        self.journal.say(f"[look] code folder {self.code}: installed {self.installed or 'none'}, newest {self.newest}")
        self.journal.step("look", "done", "looked")

    def fetch(self) -> None:
        if self.installed == self.newest:
            self.journal.step("fetch", "already", f"already at {self.newest}: nothing to download")
            self.reexec(installed_script(self.code))
            return
        if self.new_folder.exists():
            safe_remove(self.new_folder, self.code, self.data)  # cleared first: a kill may have left it half made
        done = self.child("fetch",
                          ["git", "clone", "--depth", "1", "--branch", self.newest, self.repo, self.new_folder],
                          timeout=900)
        if done.rc != 0 or not stack_script(self.new_folder).exists():
            raise Fail(SETUP_FAILED, "fetch", f"could not download {self.newest}",
                       "check the internet connection, then run this again", done.out)
        (self.new_folder / ".install-ready").write_text(self.newest or "", encoding="utf-8")
        self.replacing = True
        self.journal.step("fetch", "done", f"downloaded {self.newest} to {self.new_folder.name}")
        inside_code = inside(SCRIPT, self.code)
        self.reexec(installed_script(self.new_folder), force=inside_code)

    def reexec(self, newest_copy: Path, force: bool = False) -> None:
        """Hand over to the newest tag's copy of this script (copied out of the folders it may replace)."""
        if self.args.no_reexec or not newest_copy.exists():
            return
        if file_hash(newest_copy) == file_hash(SCRIPT) and not force:
            return
        self.bootstrap.mkdir(parents=True, exist_ok=True)
        copy = self.bootstrap / "install.py"
        shutil.copyfile(newest_copy if file_hash(newest_copy) != file_hash(SCRIPT) else SCRIPT, copy)
        self.journal.say("[fetch] a newer copy of this script is taking over")
        self.lock.release()
        words = [sys.executable, str(copy), *self.args.argv, "--no-reexec"]
        done = self.host.run(words, timeout=6 * 3600)
        for line in done.out.splitlines():
            self.journal.out(redact(line))
        raise Handover(done.rc)

    def stop(self) -> None:
        if not self.replacing:
            self.journal.step("stop", "already", "already as it should be: the code is current, so nothing is stopped")
            return
        if not self.code.exists():
            self.journal.step("stop", "skipped", "nothing installed yet: nothing to stop")
            return
        asked = self.stack("stop", timeout=400)
        for _ in range(STOP_POLLS):
            if not self.host.health(self.app_url) and not self.host.health(self.engine_url):
                self.stopped = True
                self.journal.step("stop", "done", "stopped: nothing answers")
                return
            self.host.sleep(STOP_WAIT)
        raise Fail(NO_BACKUP, "backup", f"the app still answers at {self.app_url}, so the data cannot be copied safely",
                   "close resumebot, then run this again; nothing was changed", asked.out)

    def backup(self) -> None:
        database = self.data / "resumebot.db"
        if not self.replacing:
            self.journal.step("backup", "already", "already safe: the code is current, so nothing is replaced")
            return
        if not self.code.exists() or not (database.exists() or (self.data / ".env").exists()):
            self.journal.step("backup", "skipped", "nothing to back up")
            return
        if self.backed_up:
            self.journal.step("backup", "already", "already backed up in this run")
            return
        if self.host.health(self.app_url) or self.host.health(self.engine_url):
            raise Fail(NO_BACKUP, "backup", f"the app still answers at {self.app_url}",
                       "close resumebot, then run this again; nothing was changed")
        stamp = self.host.now().strftime("%Y%m%d-%H%M%S")
        final = self.data / "backups" / f"install-{stamp}"
        target = final.with_name(final.name + ".partial")
        try:
            shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True)
            if database.exists():
                with contextlib.closing(sqlite3.connect(database)) as source, \
                        contextlib.closing(sqlite3.connect(target / database.name)) as copy:
                    source.backup(copy)
                    checked = copy.execute("PRAGMA quick_check").fetchone()
                if not checked or checked[0] != "ok":
                    raise OSError("the copy did not pass PRAGMA quick_check")
            for entry in self.data.iterdir():
                if (entry.name in SKIP_IN_BACKUP or entry.name == database.name
                        or entry.name.startswith(database.name + "-")):
                    continue
                if entry.is_dir() and entry.name == "secrets":
                    shutil.copytree(entry, target / entry.name)
                elif entry.is_file() and entry.name == ".env":
                    shutil.copy2(entry, target / entry.name)
            os.replace(target, final)
        except (OSError, sqlite3.Error, shutil.Error) as error:
            shutil.rmtree(target, ignore_errors=True)
            raise Fail(NO_BACKUP, "backup", f"the data could not be copied ({type(error).__name__})",
                       "free some disk space or close programs holding the data folder, then run this again; "
                       "nothing was changed") from error
        self.backed_up = True
        self.journal.state["backup"] = str(final)
        self.journal.step("backup", "done", f"backed up the data to {final}")

    def replace(self) -> None:
        if not self.replacing:
            self.journal.step("replace", "already", f"already at {self.installed}")
            return
        old = None
        try:
            if self.code.exists():
                old = self.code.with_name(f"{self.code.name}-old-{self.host.now().strftime('%Y%m%d-%H%M%S')}")
                os.replace(self.code, old)
            self.code.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.new_folder, self.code)
        except OSError as error:
            if old is not None and old.exists() and not self.code.exists():
                os.replace(old, self.code)
            raise Fail(SETUP_FAILED, "replace", f"could not swap the code folder ({error.strerror or error})",
                       "close programs using the code folder, then run this again; "
                       "the old code was put back") from error
        (self.code / ".install-ready").unlink(missing_ok=True)
        self.installed = self.newest
        kept = f", the old code is kept as {old.name}" if old else ""
        self.journal.step("replace", "done", f"now {self.newest}{kept}")

    def lock_hash(self) -> str:
        parts = (self.code / "app" / "backend" / "requirements.lock",
                 self.code / "app" / "frontend" / "package-lock.json")
        digest = hashlib.sha256("".join(file_hash(part) for part in parts).encode()).hexdigest()
        return f"{digest[:16]}:{self.installed}"

    def environment_works(self) -> bool:
        python = venv_python(self.code)
        if not python.exists() or not resumebot_program(self.code).exists():
            return False
        probe = self.host.run([str(python), "-c", "import pip, sqlite3, sys; print(sqlite3.sqlite_version)"],
                              timeout=60)
        return probe.rc == 0

    def setup(self) -> None:
        if self.environment_works() and self.journal.state.get("setup_hash") == self.lock_hash():
            self.journal.step("setup", "already", "the Python environment and the page are already set up")
            return
        words = ["setup"]
        works = self.environment_works()
        if works and self.journal.state.get("page_hash") == self.lock_hash():
            words.append("--no-page")  # only the environment is broken: leave the built page alone
        if not works:  # missing or half made: uv's Python builds it (the venv's own python.exe cannot delete its venv)
            self.child("setup", ["uv", "python", "install", "3.12"], timeout=900)
            found = self.host.run(["uv", "python", "find", "--managed-python", "3.12"], timeout=60)
            python = found.out.strip().splitlines()[0] if found.rc == 0 and found.out.strip() else ""
            if not python:
                raise Fail(SETUP_FAILED, "setup", "uv could not find Python 3.12",
                           "run `uv python install 3.12`, then run this again", found.out)
            done = self.child("setup", [python, stack_script(self.code), "--data", self.data, *words], cwd=self.code,
                              timeout=3600)
        else:
            done = self.stack(*words, timeout=3600)
        if done.rc != 0 or not self.environment_works():
            raise Fail(SETUP_FAILED, "setup", "building the Python environment or the page failed",
                       f"read {self.journal.log_path}, fix what it names, then run this again", done.out)
        self.journal.state["setup_hash"] = self.journal.state["page_hash"] = self.lock_hash()
        self.journal.step("setup", "done", "set up the Python environment and the page")

    def init(self) -> None:
        if self.settings_complete():
            self.journal.step("init", "already", "already complete: the settings are there")
            return
        if not self.args.owner:
            raise Fail(USAGE, "init", "the settings are incomplete and no owner name was given",
                       "run this again with --owner \"Your Name\"")
        words = ["init", "--owner", self.args.owner]
        if self.args.timezone:
            words += ["--timezone", self.args.timezone]
        done = self.app_command(*words)
        if done.rc != 0:
            raise Fail(SETUP_FAILED, "init", "writing the settings failed", f"read {self.journal.log_path}", done.out)
        self.journal.step("init", "done", "wrote the settings")

    def running_this_version(self) -> bool:
        page, engine = self.host.health(self.app_url), self.host.health(self.engine_url)
        wanted = ".".join(str(part) for part in (tag_version(self.installed or "") or ()))
        return bool(page and engine and page.get("status") == "ok" and engine.get("status") == "ok"
                    and page.get("version") == wanted)

    def start(self) -> None:
        if self.running_this_version():
            self.journal.step("start", "already", "already running this version")
            return
        if self.host.health(self.app_url) or self.host.health(self.engine_url):
            self.stack("stop", timeout=400)  # half up, or an older version: stop first
        done = self.app_command("start", timeout=900)
        if done.rc != 0:
            raise Fail(NO_START, "start", "the app did not start",
                       f"read {self.data / 'logs'} and {self.journal.log_path}", done.out)
        self.journal.step("start", "done", f"started at {self.app_url}")

    def smoke(self) -> None:
        done = Result(1)
        for attempt in range(SMOKE_TRIES):
            done = self.app_command("smoke", "--lanes", timeout=300)
            if done.rc == 0:
                self.journal.step("smoke", "done", "the app and its five lanes answer")
                return
            if attempt + 1 < SMOKE_TRIES:
                self.host.sleep(SMOKE_WAIT)
        failing = next((line for line in done.out.splitlines() if "FAIL" in line), "a check failed")
        raise Fail(NO_START, "smoke", f"the app is up but a check failed ({failing.strip()})",
                   f"read {self.data / 'logs'} and {self.journal.log_path}", done.out)

    def signin(self) -> None:
        token = self.data / "secrets" / "claude-oauth-token"
        try:
            stored = token.stat().st_size > 0
        except OSError:
            stored = False
        if stored:
            done = self.app_command("signin", timeout=120)  # the command itself skips a good token
            if done.rc == 0:
                self.journal.step("signin", "already", "already signed in")
                return
        self.journal.waiting("approve the sign-in in the browser tab that opened")
        done = self.app_command("signin", timeout=WAITING_LIMIT)
        self.journal.resume()
        if done.rc != 0:
            raise Fail(SIGNIN_FAILED, "signin", "the agents' sign-in did not finish",
                       "run this again and approve the sign-in in the browser tab within ten minutes", done.out)
        self.journal.step("signin", "done", "signed in")

    def connect(self) -> None:
        done = self.app_command("connect", timeout=300)
        if done.rc != 0:
            raise Fail(NO_DOOR, "connect", "could not register the doors with Claude Code",
                       "make sure Claude Code is installed and signed in, then run this again", done.out)
        already = "already" in done.out.lower()
        self.journal.step("connect", "already" if already else "done",
                          "the doors are already registered" if already else "registered the doors")

    def shortcut(self) -> None:
        done = self.app_command("shortcut", timeout=120)
        if done.rc != 0:
            raise Fail(SETUP_FAILED, "shortcut", "could not put the shortcut on the desktop",
                       f"read {self.journal.log_path}, then run this again", done.out)
        already = "already" in done.out.lower()
        self.journal.step("shortcut", "already" if already else "done",
                          "the shortcut is already on your desktop" if already else "the shortcut is on your desktop")

    def doctor(self) -> None:
        done = self.app_command("doctor", timeout=300)
        for line in done.out.splitlines():
            self.journal.say(f"[doctor] {line}")
        if done.rc != 0:
            raise Fail(DOCTOR_FOUND, "doctor", "the check found something missing",
                       "do what the MISSING lines say", done.out)
        self.journal.step("doctor", "done", "ok")


class Handover(Exception):
    """A newer copy of this script ran the install; its exit code is ours."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])


def parser() -> argparse.ArgumentParser:
    ask = argparse.ArgumentParser(
        prog="install.py",
        description="Install, finish or update resumebot; every step looks first and skips what is already done.")
    ask.add_argument("--owner", help="the name the roles are kept under (only needed when the settings are missing)")
    ask.add_argument("--timezone", help="an IANA zone such as America/New_York (default: this machine's)")
    ask.add_argument("--data", help=f"the data folder (default {DATA_VARIABLE}, else the user's own folder)")
    ask.add_argument("--code", help="the code folder (default %%LOCALAPPDATA%%\\Programs\\resumebot)")
    ask.add_argument("--repo", help=f"where to download from (default {REPO_VARIABLE}, else the project's repository)")
    ask.add_argument("--bootstrap", help="where a newer copy of this script is kept while it runs")
    ask.add_argument("--no-reexec", action="store_true", help="do not hand over to a newer copy of this script")
    return ask


def main(argv: list[str] | None = None, host: Host | None = None, out: Callable[[str], None] = print) -> int:
    words = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser().parse_args(words)
    except SystemExit as stop:
        return USAGE if stop.code not in (0, None) else OK
    args.argv = words
    host = host or Host()
    install = Install(args, host, out)
    current = "start-up"
    try:
        assert_apart(install.code, install.data)
        install.data.mkdir(parents=True, exist_ok=True)
        install.lock.take()
        for name in STEPS:
            current = name
            getattr(install, name)()
        install.journal.state["state"] = "done"
        install.journal.save()
        install.journal.say("resumebot is installed and answering at " + install.app_url)
        return OK
    except Handover as other:
        return other.code
    except Fail as problem:
        install.journal.state["state"] = "failed"
        install.journal.state["failure"] = redact(str(problem))
        install.journal.step(problem.step, "failed", str(problem)) if problem.step in STEPS else install.journal.save()
        install.journal.log(tail(problem.detail, 40))
        out(redact(str(problem)))
        out(f"details: {install.journal.log_path}")
        return problem.code
    except Exception as error:
        code = UNMAPPED.get(current, SETUP_FAILED)
        problem = Fail(code, current, f"it stopped on an unexpected {type(error).__name__}",
                       f"read {install.journal.log_path} and run this again, or report the log",
                       traceback.format_exc())
        install.journal.state["state"] = "failed"
        install.journal.state["failure"] = redact(str(problem))
        try:
            install.journal.step(current, "failed", str(problem)) if current in STEPS else install.journal.save()
        except Exception:
            pass
        for report in (lambda: install.journal.log(problem.detail), lambda: out(redact(str(problem))),
                       lambda: out(f"details: {install.journal.log_path}")):
            try:  # the data folder itself may have failed: the log and the screen are best effort
                report()
            except Exception:
                pass
        return code
    finally:
        install.lock.release()


if __name__ == "__main__":
    sys.exit(main())
