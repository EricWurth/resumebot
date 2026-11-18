"""The install script (app/scripts/local/install.py): its steps, in order, each looking before it acts. Everything it
touches on the machine (git, the app's commands, the clock, the health check, the process table) is a fake here; the
folders are made up under tmp_path. Nothing starts a program, a server or a window."""

import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "local" / "install.py"
spec = importlib.util.spec_from_file_location("install_script", SCRIPT)
inst = importlib.util.module_from_spec(spec)
sys.modules["install_script"] = inst
spec.loader.exec_module(inst)

#: Token-shaped text a child prints; none of it may reach the screen, the log or the status file.
SECRET = "sk-ant-oat01-" + "A1b2C3d4" * 6
LONG_RUN = "Z9y8X7w6" * 7


class FakeHost(inst.Host):
    """git, uv, node, the venv's Python, stack.py and the resumebot program, as one table of canned answers."""

    def __init__(self, code: Path, data: Path) -> None:
        super().__init__()
        self.code, self.data = code, data
        self.tags = ["app-v2.5.5"]
        self.calls: list[list[str]] = []
        self.missing: set[str] = set()
        self.fail: dict[str, int] = {}
        self.app_up = False
        self.stop_works = True
        self.version = ""
        self.live_pids: set[int] = set()
        self.script_text = SCRIPT.read_bytes().decode("utf-8")
        self.registered = False
        self.child_rc = 0
        self.unfinished = False  #: `status` says an init was cut short
        self.broken_env = False  #: the venv has a python.exe that does not run (no pip)

    # the machine
    def which(self, name):
        return None if name in self.missing else f"C:/fake/{name}"

    def registry_path(self):
        return []

    def registry_user_variable(self, name):
        return None

    def pid_alive(self, pid):
        return pid in self.live_pids

    def sleep(self, seconds):
        pass

    def health(self, url):
        return {"status": "ok", "version": self.version} if self.app_up else None

    # programs
    def run(self, argv, cwd=None, timeout=600):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        name = Path(argv[0]).name.lower()
        if name == "git":
            return self.git(argv)
        if name == "node":
            return inst.Result(0, "v22.4.0\n")
        if name == "uv":
            return inst.Result(0, "C:/py/python.exe\n")
        if len(argv) > 1 and argv[1].endswith("install.py"):
            return inst.Result(self.child_rc, "the newer script ran\n")
        if len(argv) > 1 and argv[1].endswith("stack.py"):
            return self.stack(argv)
        if len(argv) > 1 and argv[1] == "-c":
            return inst.Result(1, "No module named 'pip'\n") if self.broken_env else inst.Result(0, "3.51.3\n")
        if name.startswith("resumebot"):
            return self.app(argv)
        raise AssertionError(f"an unfaked program was run: {argv}")

    def git(self, argv):
        if "ls-remote" in argv:
            return inst.Result(0, "".join(f"abc123\trefs/tags/{tag}\n" for tag in self.tags))
        if "clone" in argv:
            tag, target = argv[argv.index("--branch") + 1], Path(argv[-1])
            files = (("app/scripts/local/stack.py", "# stack"), ("app/scripts/local/install.py", self.script_text),
                     ("app/backend/requirements.lock", "locked " + tag),
                     ("app/frontend/package-lock.json", "{}"), (".git/HEAD", "ref"))
            for part, text in files:
                (target / part).parent.mkdir(parents=True, exist_ok=True)
                (target / part).write_bytes(text.encode("utf-8"))
            (target / ".fake-tag").write_text(tag, encoding="utf-8")
            return inst.Result(0, "")
        if "describe" in argv:
            folder = Path(argv[argv.index("-C") + 1])
            try:
                return inst.Result(0, (folder / ".fake-tag").read_text(encoding="utf-8") + "\n")
            except OSError:
                return inst.Result(128, "fatal: no tag")
        raise AssertionError(argv)

    def stack(self, argv):
        word = argv[-1] if argv[-1] != "--no-page" else argv[-2]
        if self.fail.get(word):
            return inst.Result(self.fail[word], f"{word} broke: {SECRET}\n")
        if word == "setup":
            self.broken_env = False
            for path in (inst.venv_python(self.code), inst.resumebot_program(self.code)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fake", encoding="utf-8")
        elif word == "stop" and self.stop_works:
            self.app_up = False
        return inst.Result(0, "")

    def app(self, argv):
        word = argv[1]
        if self.fail.get(word):
            return inst.Result(self.fail[word], f"{word} broke\nAuthorization: Bearer {SECRET}\n{LONG_RUN * 2}\n")
        if word == "status":
            env = self.data / ".env"
            ready = env.exists() and "JOBSEARCH_OWNER" in env.read_text(encoding="utf-8")
            if ready and self.unfinished:
                return inst.Result(0, "install: started here but not finished: missing the secret owner-token\n")
            return inst.Result(0, "install: set up\n" if ready else "install: not set up\n")
        if word == "init":
            self.unfinished = False
            (self.data / ".env").write_text(f"JOBSEARCH_OWNER={argv[argv.index('--owner') + 1]}\n", encoding="utf-8")
        elif word == "start":
            self.app_up = True
            self.version = ".".join(re.findall(r"\d+", (self.code / ".fake-tag").read_text(encoding="utf-8")))
        elif word == "signin":
            token = self.data / "secrets" / "claude-oauth-token"
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text("stored", encoding="utf-8")
            return inst.Result(0, "signed in\n")
        elif word == "connect":
            said = "already registered\n" if self.registered else "registered\n"
            self.registered = True
            return inst.Result(0, said)
        elif word == "shortcut":
            marker = self.data.parent / "shortcut-made"
            said = "shortcut already on your desktop\n" if marker.exists() else "shortcut is on your desktop\n"
            marker.write_text("x", encoding="utf-8")
            return inst.Result(0, said)
        elif word == "doctor":
            return inst.Result(0, "ok       everything\n")
        return inst.Result(0, "")


@pytest.fixture
def world(tmp_path):
    code, data = tmp_path / "Programs" / "resumebot", tmp_path / "local" / "resumebot"
    data.mkdir(parents=True)
    return code, data, FakeHost(code, data), tmp_path


def run(world, *extra):
    code, data, host, tmp = world
    lines: list[str] = []
    args = ["--data", str(data), "--code", str(code), "--repo", "fake://repo", "--bootstrap", str(tmp / "boot"),
            "--owner", "Test Person", *extra]
    return inst.main(args, host=host, out=lines.append), lines


def snapshot(*folders: Path, skip: tuple[str, ...] = ()) -> dict[str, str]:
    seen = {}
    for folder in folders:
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder.parent).as_posix()
            if path.is_file() and not any(relative.startswith(f"{folder.name}/{part}/") for part in skip):
                seen[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return seen


def step_order(lines):
    found = []
    for line in lines:
        match = re.match(r"\[(\w+)\]", line)
        if match and match.group(1) in inst.STEPS and match.group(1) not in found:
            found.append(match.group(1))
    return found


def test_a_fresh_install_runs_the_steps_in_order(world):
    code, data, host, _ = world
    rc, lines = run(world)
    assert rc == 0
    assert step_order(lines) == list(inst.STEPS)
    assert (code / ".fake-tag").read_text(encoding="utf-8") == "app-v2.5.5"
    assert not code.with_name("resumebot-new").exists()
    assert json.loads((data / "install" / "install.json").read_text(encoding="utf-8"))["state"] == "done"


def test_a_second_run_says_already_and_changes_no_file_in_the_code_or_data_folders(world):
    code, data, host, _ = world
    assert run(world)[0] == 0
    before = snapshot(code, data, skip=("install", "run"))
    host.calls.clear()
    rc, lines = run(world)
    assert rc == 0
    text = "\n".join(lines)
    for step in ("fetch", "stop", "backup", "replace", "setup", "init", "start", "signin", "connect", "shortcut"):
        assert re.search(rf"\[{step}\] .*already", text), step
    assert snapshot(code, data, skip=("install", "run")) == before
    assert not [call for call in host.calls if "clone" in call], "the tag is current: nothing is downloaded"
    assert not [call for call in host.calls if call[-1] in ("stop", "setup")], "nothing is stopped or rebuilt"


def test_an_update_stops_backs_up_swaps_and_keeps_the_old_code(world):
    code, data, host, _ = world
    assert run(world)[0] == 0
    with sqlite3.connect(data / "resumebot.db") as db:
        db.execute("create table t (a)")
        db.execute("insert into t values (1)")
    db.close()
    for folder in ("logs", "claude", "work", "browser-profile"):
        (data / folder).mkdir()
        (data / folder / "big.bin").write_text("skip me", encoding="utf-8")
    host.tags.append("app-v2.6.0")
    host.app_up, host.version = True, "2.5.5"
    owned = snapshot(data, skip=("install", "run", "backups"))
    rc, lines = run(world)
    assert rc == 0, chr(10).join(lines)
    assert (code / ".fake-tag").read_text(encoding="utf-8") == "app-v2.6.0"
    assert list(code.parent.glob("resumebot-old-*")), "the old code is kept"
    backup = next((data / "backups").iterdir())
    assert (backup / ".env").exists() and (backup / "secrets" / "claude-oauth-token").exists()
    assert not any((backup / folder).exists() for folder in ("logs", "claude", "work", "browser-profile"))
    with sqlite3.connect(backup / "resumebot.db") as copy:
        assert copy.execute("pragma quick_check").fetchone() == ("ok",)
        assert copy.execute("select a from t").fetchone() == (1,)
    assert snapshot(data, skip=("install", "run", "backups")) == owned, "the data folder is only added to"
    assert step_order(lines).index("stop") < step_order(lines).index("backup") < step_order(lines).index("replace")


def test_the_backup_refuses_while_the_app_still_answers(world, monkeypatch):
    code, data, host, _ = world
    assert run(world)[0] == 0
    host.tags.append("app-v2.6.0")
    host.stop_works = False
    monkeypatch.setattr(inst, "STOP_POLLS", 2)
    before = snapshot(code, data, skip=("install", "run"))
    rc, lines = run(world)
    assert rc == inst.NO_BACKUP == 4
    assert snapshot(code, data, skip=("install", "run")) == before
    assert not list(code.parent.glob("resumebot-old-*")) and not (data / "backups").exists()
    assert any(line.startswith("backup: ") and "still answers" in line for line in lines)


def test_an_interrupted_replace_is_finished_when_the_new_code_is_ready(world):
    code, data, host, _ = world
    old = code.with_name("resumebot-old-1")
    old.mkdir(parents=True)
    (old / ".fake-tag").write_text("app-v2.5.5", encoding="utf-8")
    new = code.with_name("resumebot-new")
    (new / "app/scripts/local").mkdir(parents=True)
    (new / ".install-ready").write_text("app-v2.6.0", encoding="utf-8")
    (new / ".fake-tag").write_text("app-v2.6.0", encoding="utf-8")
    (new / ".git").mkdir()
    host.tags = ["app-v2.6.0"]
    args = inst.parser().parse_args(["--data", str(data), "--code", str(code)])
    install = inst.Install(args, host, lambda text: None)
    install.recover()
    assert (code / ".fake-tag").read_text(encoding="utf-8") == "app-v2.6.0" and not new.exists()
    assert not (code / ".install-ready").exists()  # the swap's marker does not stay in the finished code folder


def test_an_interrupted_replace_is_undone_when_the_new_code_is_not_ready(world):
    code, data, host, _ = world
    old = code.with_name("resumebot-old-1")
    old.mkdir(parents=True)
    (old / ".fake-tag").write_text("app-v2.5.5", encoding="utf-8")
    code.with_name("resumebot-new").mkdir()
    args = inst.parser().parse_args(["--data", str(data), "--code", str(code)])
    install = inst.Install(args, host, lambda text: None)
    install.recover()
    assert (code / ".fake-tag").read_text(encoding="utf-8") == "app-v2.5.5"


def test_a_half_made_new_folder_is_cleared_before_the_next_download(world):
    code, data, host, _ = world
    new = code.with_name("resumebot-new")
    (new / "junk").mkdir(parents=True)
    (new / "junk" / "half.txt").write_text("half", encoding="utf-8")
    assert run(world)[0] == 0
    assert not (code / "junk").exists()


def test_a_second_run_is_refused_while_the_first_is_alive_and_a_stale_lock_is_taken(world):
    code, data, host, _ = world
    (data / "run").mkdir()
    (data / "run" / "install.lock").write_text("4242", encoding="utf-8")
    host.live_pids = {4242}
    rc, lines = run(world)
    assert rc == inst.BUSY == 10
    assert "already running (pid 4242)" in "\n".join(lines)
    host.live_pids = set()
    host.missing = {"git"}  # stops at the prelude, which comes after the lock
    assert run(world)[0] == inst.PREREQUISITE
    assert not (data / "run" / "install.lock").exists(), "the lock is let go"


def test_the_data_folder_can_never_be_removed_or_overlap_the_code_folder(world, tmp_path):
    code, data, host, _ = world
    with pytest.raises(inst.Fail):
        inst.safe_remove(data, code, data)
    with pytest.raises(inst.Fail):
        inst.safe_remove(tmp_path, code, data)  # a folder holding the data folder
    with pytest.raises(inst.Fail):
        inst.safe_remove(code, code, data)  # only <code>-new is ever cleared
    inside_data = data / "app"
    assert run((inside_data, data, host, tmp_path))[0] == inst.USAGE
    assert run((tmp_path, data, host, tmp_path))[0] == inst.USAGE  # data lies inside the code folder
    assert run((data, data, host, tmp_path))[0] == inst.USAGE
    assert data.exists()


@pytest.mark.parametrize(("step", "fail", "code"), [
    ("prelude", "missing", 3), ("setup", "setup", 5), ("init", "init", 5), ("start", "start", 6), ("smoke", "smoke", 6),
    ("signin", "signin", 7), ("connect", "connect", 8), ("shortcut", "shortcut", 5), ("doctor", "doctor", 9)])
def test_each_failure_has_its_exit_code_and_one_sentence(world, step, fail, code, monkeypatch):
    monkeypatch.setattr(inst, "SMOKE_TRIES", 2)
    _, _, host, _ = world
    if fail == "missing":
        host.missing = {"uv"}
    else:
        host.fail[fail] = 1
    rc, lines = run(world)
    assert rc == code
    sentence = next(line for line in lines if line.startswith(f"{step}: "))
    assert sentence.count(": ") >= 2, "<step>: <what failed>: <what to do>"
    assert step_order(lines)[-1] == step or step == "prelude"


def test_a_missing_owner_name_is_a_usage_error_only_when_the_settings_are_missing(world):
    code, data, host, tmp = world
    args = ["--data", str(data), "--code", str(code), "--repo", "x", "--bootstrap", str(tmp / "b")]
    assert inst.main(args, host=host, out=lambda text: None) == inst.USAGE
    assert inst.main([*args, "--owner", "A B"], host=host, out=lambda text: None) == 0
    assert inst.main(args, host=host, out=lambda text: None) == 0  # complete: no owner needed


def test_an_init_that_a_stop_cut_short_is_finished_not_taken_for_complete(world):
    code, data, host, _ = world
    assert run(world)[0] == 0
    host.unfinished = True  # status now names a missing piece, though the settings file has its owner
    rc, lines = run(world)
    assert rc == 0 and not host.unfinished
    assert any(line.startswith("[init] wrote") for line in lines)


def test_sign_in_waits_for_the_user_in_the_status_file_with_one_sentence(world):
    code, data, host, _ = world
    seen = []
    original = host.app

    def watching(argv):
        if argv[1] == "signin":
            seen.append(json.loads((data / "install" / "install.json").read_text(encoding="utf-8")))
        return original(argv)

    host.app = watching
    rc, lines = run(world)
    assert rc == 0
    assert seen[0]["state"] == "waiting-for-user"
    assert seen[0]["action"] == "approve the sign-in in the browser tab that opened"
    assert sum("waiting for you" in line for line in lines) == 1
    assert "action" not in json.loads((data / "install" / "install.json").read_text(encoding="utf-8"))


def test_a_newer_copy_of_the_script_takes_over(world):
    code, data, host, tmp = world
    host.script_text = "# a newer install script\n"
    host.child_rc = 0
    rc, lines = run(world)
    assert rc == 0
    child = next(call for call in host.calls if len(call) > 1 and call[1].endswith("install.py"))
    assert "--no-reexec" in child and Path(child[1]).parent == tmp / "boot"
    assert Path(child[1]).read_text(encoding="utf-8") == "# a newer install script\n"
    assert not [call for call in host.calls if call[-1] == "setup"], "the older script does nothing more"
    assert not (data / "run" / "install.lock").exists(), "the lock is let go for the newer copy"
    host.child_rc = 6
    shutil_rc, _ = run(world)
    assert shutil_rc == 6, "the newer copy's exit code is ours"


def test_nothing_printed_or_logged_holds_a_token(world):
    code, data, host, _ = world
    host.fail["connect"] = 1
    rc, lines = run(world)
    assert rc == inst.NO_DOOR
    everything = "\n".join(lines) + "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in (data / "install").iterdir() if path.is_file())
    assert "Bearer" not in everything.replace("Bearer [token]", "")
    for text in (SECRET, LONG_RUN * 2, "A1b2C3d4" * 6):
        assert text not in everything
    host.fail = {"setup": 1}
    host.fail["setup"] = 1
    rc, lines = run((code.with_name("other"), data.with_name("data2"), host, data.parent))
    assert SECRET not in "\n".join(lines)
    assert inst.redact(f"x Bearer {SECRET} y") == "x Bearer [token] y"


def test_an_unmapped_exception_ends_in_the_steps_exit_code_and_one_sentence(world, monkeypatch):
    code, data, host, _ = world

    def boom(self):
        raise RuntimeError(f"surprise {SECRET}")

    monkeypatch.setattr(inst.Install, "signin", boom)
    rc, lines = run(world)
    assert rc == inst.SIGNIN_FAILED == 7
    assert any(re.match(r"signin: .+: .+", line) and "RuntimeError" in line for line in lines)
    log = (data / "install" / "install.log").read_text(encoding="utf-8")
    assert "Traceback" in log and "surprise" in log
    assert SECRET not in log and SECRET not in "\n".join(lines)
    assert json.loads((data / "install" / "install.json").read_text(encoding="utf-8"))["state"] == "failed"


def test_an_unmapped_exception_still_ends_in_the_exit_code_when_the_log_and_the_screen_fail_too(world, monkeypatch):
    code, data, host, tmp = world
    gone = []

    def broken(*_a, **_k):
        raise RuntimeError("the data folder is gone")

    def boom(self):
        monkeypatch.setattr(inst.Journal, "log", broken)  # the data folder fails at the moment of the failure
        gone.append(True)
        raise RuntimeError("surprise")

    def out(line):
        if gone:
            broken()

    monkeypatch.setattr(inst.Install, "signin", boom)
    args = ["--data", str(data), "--code", str(code), "--repo", "fake://repo", "--bootstrap", str(tmp / "boot"),
            "--owner", "Test Person"]
    assert inst.main(args, host=host, out=out) == inst.SIGNIN_FAILED == 7


def test_an_unmapped_exception_in_a_setup_step_is_exit_5(world, monkeypatch):
    monkeypatch.setattr(inst.Install, "replace", lambda self: 1 / 0)
    rc, lines = run(world)
    assert rc == inst.SETUP_FAILED == 5
    assert any(line.startswith("replace: ") for line in lines)


def test_the_new_folder_is_cleared_whole_read_only_files_and_a_link_included_and_the_data_stays(world):
    code, data, _host, _tmp = world
    new = code.with_name(code.name + "-new")
    held = new / ".git" / "objects" / "ab" / "cdef"
    held.parent.mkdir(parents=True)
    held.write_text("x", encoding="utf-8")
    held.chmod(0o444)  # git writes its objects read-only; Windows will not delete such a file as it is
    (new / "app" / "frontend" / "node_modules").mkdir(parents=True)
    (new / "app" / "frontend" / "node_modules" / "left.js").write_text("x", encoding="utf-8")
    link = new / "app" / "node_modules"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(new / "app" / "frontend" / "node_modules"), str(link))
    else:
        link.symlink_to(new / "app" / "frontend" / "node_modules", target_is_directory=True)
    (data / "resumebot.db").write_bytes(b"the user's work")
    inst.safe_remove(new, code, data)
    assert not new.exists()
    assert (data / "resumebot.db").read_bytes() == b"the user's work"


def test_a_half_made_environment_is_rebuilt_by_uvs_python_not_by_the_python_inside_it(world):
    """On Windows a python.exe cannot delete the venv it runs from (found by the real rehearsal, plan scenario 13)."""
    code, data, host, _tmp = world
    assert run(world)[0] == 0
    host.broken_env = True
    host.calls.clear()
    rc, lines = run(world)
    assert rc == 0
    (setup,) = [call for call in host.calls if call[-1] == "setup"]
    assert Path(setup[0]) == Path("C:/py/python.exe")
    assert Path(setup[0]) != inst.venv_python(code)
    assert any(line.startswith("[setup] set up") for line in lines)


def test_a_backup_that_fails_part_way_leaves_no_folder_that_looks_complete(world, monkeypatch):
    code, data, host, _ = world
    assert run(world)[0] == 0
    host.tags.append("app-v2.6.0")
    host.app_up, host.version = True, "2.5.5"
    (data / "resumebot.db").write_bytes(b"")
    real = inst.shutil.copytree

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(inst.shutil, "copytree", broken)
    rc, lines = run(world)
    monkeypatch.setattr(inst.shutil, "copytree", real)
    assert rc == inst.NO_BACKUP == 4
    assert not list((data / "backups").glob("install-*")), "no partial backup is left behind"
    assert not list(code.parent.glob("resumebot-old-*")), "nothing was replaced"


def test_a_good_backup_is_renamed_from_its_temporary_name(world):
    code, data, host, _ = world
    assert run(world)[0] == 0
    host.tags.append("app-v2.6.0")
    host.app_up, host.version = True, "2.5.5"
    assert run(world)[0] == 0
    names = [folder.name for folder in (data / "backups").iterdir()]
    assert len(names) == 1 and not names[0].endswith(".partial")


def test_the_data_folder_is_the_variable_then_a_registered_door_then_the_default(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.delenv(inst.DATA_VARIABLE, raising=False)
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "other": {"args": ["--data", str(tmp_path / "wrong")]},
        "resumebot-owner": {"args": ["x", "--data", str(tmp_path / "doored")]}}}), encoding="utf-8")

    class Plain(inst.Host):
        def registry_user_variable(self, name):
            return None

    def data_for(*extra):
        args = inst.parser().parse_args([*extra])
        return inst.Install(args, Plain(), lambda text: None).data

    assert data_for() == tmp_path / "doored"
    monkeypatch.setenv(inst.DATA_VARIABLE, str(tmp_path / "variable"))
    assert data_for() == tmp_path / "variable"
    monkeypatch.delenv(inst.DATA_VARIABLE)
    (home / ".claude.json").unlink()
    assert data_for() == inst.default_data_folder()
